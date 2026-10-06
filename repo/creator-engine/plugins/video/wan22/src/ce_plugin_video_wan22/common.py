"""Shared planning and adapter code of the Wan 2.2 engines.

Wan generates 4n+1 frames at its native rate (16 fps for A14B, 24 fps for TI2V-5B) in fixed size
buckets; the adapter plans the frame count covering the requested duration (capped at the manifest's
`max_frames`), picks the bucket in the request's orientation, then conforms the clip to the
requested frame size at the native rate. Frame-rate conversion is a separate node
(`video.interpolate`, §22)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ce_contracts import models as m
from ce_contracts.common import RunContext
from ce_plugin_kit import EngineAdapter
from ce_plugin_kit.media import conform_clip, probe

__all__ = ["WanParams", "WanVideoAdapter", "frames_for", "plan"]


@dataclass(frozen=True)
class WanParams:
    task: str  # t2v | i2v
    width: int
    height: int
    num_frames: int
    fps: int
    steps: int
    guidance_scale: float
    shift: float
    seed: int
    prompt: str
    negative: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def frames_for(seconds: float, fps: int) -> int:
    needed = max(1, math.ceil(seconds * fps))
    return ((needed - 1 + 3) // 4) * 4 + 1


def plan(request: m.VideoGenerateRequest, defaults: dict[str, Any], *, task: str, seed: int) -> WanParams:
    fps = int(defaults.get("fps", 16))
    frames = min(frames_for(request.duration_s, fps), int(defaults.get("max_frames", 81)))
    sizes = {str(k): v for k, v in dict(defaults.get("sizes") or {"480": [480, 832]}).items()}
    short = min(request.width, request.height)
    key = next((k for k in sorted(sizes, key=int) if short <= int(k)), max(sizes, key=int))
    small, large = (int(x) for x in sizes[key])
    portrait = request.height >= request.width
    return WanParams(
        task=task,
        width=small if portrait else large,
        height=large if portrait else small,
        num_frames=frames,
        fps=fps,
        steps=int(defaults.get("steps", 4)),
        guidance_scale=float(defaults.get("guidance_scale", 1.0)),
        shift=float(defaults.get("shift", 5.0)),
        seed=int(seed) % (2**31),
        prompt=request.prompt,
        negative=request.negative or str(defaults.get("negative", "")),
    )


class WanVideoAdapter(EngineAdapter):
    engine_name = "wan22"

    async def _generate(self, task: str, request: m.VideoGenerateRequest, ctx: RunContext) -> m.VideoResult:
        backend = self.require_backend()
        work = self.scratch(ctx, task)
        image: Path | None = None
        if task == "i2v":
            if request.first_frame is None:
                raise ValueError(f"{self.manifest.id}: video.i2v needs a first_frame")
            image = await ctx.read_artifact(request.first_frame)
        params = plan(request, self.defaults, task=task, seed=ctx.seed)
        await ctx.progress(0.05, f"{task} {params.num_frames} frames at {params.width}x{params.height}")
        raw = await self.call(ctx, backend.generate, params, image, work / "raw.mp4")
        duration = min(request.duration_s, params.num_frames / params.fps)
        out = conform_clip(
            Path(raw), work / "clip.mp4", width=request.width, height=request.height, duration_s=duration
        )
        info = probe(out)
        ref = await ctx.write_artifact(
            out, "video", {"engine": self.engine_name, "params": params.as_dict(), "duration_s": info.duration_s},
            role="video", mime="video/mp4",
        )  # fmt: skip
        return m.VideoResult(
            video=ref, duration_s=info.duration_s, fps=float(params.fps), width=info.width, height=info.height
        )

    async def run_video_t2v(self, request: m.VideoGenerateRequest, ctx: RunContext) -> m.VideoResult:
        return await self._generate("t2v", request, ctx)

    async def run_video_i2v(self, request: m.VideoGenerateRequest, ctx: RunContext) -> m.VideoResult:
        return await self._generate("i2v", request, ctx)
