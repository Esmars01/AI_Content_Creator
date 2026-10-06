"""vLLM VLM adapter (`vision.image`, `vision.video`; §39.7 VLM judge, §27 screen understanding).

Media are prepared on the worker before the model sees them: images converted to RGB PNG (longest
side capped), videos cut to the requested window and resampled to `sampling_fps` (longest side
capped), so what the model saw is exactly what the request asked for. Answers are structured
(vLLM structured outputs): the caller's JSON schema is wrapped in an envelope that also asks for
a self-reported `confidence` in [0, 1] — an uncalibrated number (VLM judges are calibrated per
proxy in Phase 11), recorded as such. Thinking is off for structured answers."""

from __future__ import annotations

import json
from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import ffmpeg, probe

__all__ = ["VLLMVisionAdapter", "envelope", "instructions"]

TEXT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def envelope(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema the model must fill: `{answer: <caller's schema>, confidence: number}`."""
    return {
        "type": "object",
        "properties": {
            "answer": dict(schema) if schema else TEXT_SCHEMA,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["answer", "confidence"],
        "additionalProperties": False,
    }


def instructions(question: str, kind: str, window: tuple[float, float] | None) -> str:
    where = "the image" if kind == "image" else "the video clip"
    if window is not None and kind == "video":
        where += f" (the source's {window[0]:.2f}–{window[1]:.2f} s window; times you give are relative to its start)"
    return (
        f"Look at {where} and answer the question from what is visible only; do not guess about anything you "
        "cannot see. Reply with JSON: `answer` follows the requested schema, `confidence` is how sure you are "
        f"(0 to 1).\n\nQuestion: {question.strip()}"
    )


class VLLMVisionAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_vision_vllm.backend import VLLMBackend

        assert self.paths is not None
        return VLLMBackend(model_dir=self.paths.model(), media_root=ctx.scratch_dir, defaults=self.defaults)

    async def _ask(self, request: m.VisionRequest, ctx: RunContext, kind: str) -> m.VisionResult:
        backend = self.require_backend()
        work = self.scratch(ctx, f"vision-{request.labels.get('item', kind)}")
        source = await ctx.read_artifact(request.media)
        side = int(self.defaults.get("max_side", 1280 if kind == "image" else 896))
        scale = f"scale='if(gt(iw,ih),min({side},iw),-2)':'if(gt(iw,ih),-2,min({side},ih))'"
        if kind == "image":
            media = work / "image.png"
            ffmpeg("-i", str(source), "-frames:v", "1", "-vf", scale, str(media))
        else:
            media = work / "clip.mp4"
            args = []
            if request.window_s is not None:
                start, end = request.window_s
                if end <= start:
                    raise ValueError(f"empty window {request.window_s}")
                args += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}"]
            ffmpeg(
                *args, "-i", str(source), "-an", "-vf", f"fps={request.sampling_fps:g},{scale}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(media),
            )  # fmt: skip
            if probe(media).duration_s <= 0:
                raise ValueError("the requested window has no frames")
        schema = envelope(request.json_schema)
        raw = await self.call(
            ctx, backend.ask, kind, str(media), instructions(request.question, kind, request.window_s), schema,
            int(ctx.seed) % (2**31),
        )  # fmt: skip
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"the VLM returned invalid JSON: {exc}") from exc
        answer = data.get("answer")
        if not isinstance(answer, dict):
            raise ValueError("the VLM answer is not an object")
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
        return m.VisionResult(answer=answer, confidence=confidence)

    async def run_vision_image(self, request: m.VisionRequest, ctx: RunContext) -> m.VisionResult:
        return await self._ask(request, ctx, "image")

    async def run_vision_video(self, request: m.VisionRequest, ctx: RunContext) -> m.VisionResult:
        return await self._ask(request, ctx, "video")
