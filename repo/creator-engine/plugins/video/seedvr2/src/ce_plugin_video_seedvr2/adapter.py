"""SeedVR2-3B adapter (`video.upscale`, §26 final-pass restoration and upscaling).

The clip is read as frames, restored in overlapping temporal chunks (`chunks.py`) at the target
area (SeedVR2 resizes by area and crops to multiples of 16, like `NaResize` + `DivisibleCrop` in
upstream inference), crossfaded across the overlaps, encoded at the source frame rate with the
source audio, and conformed to the exact target size (scale to cover, center crop)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import conform_clip, extract_frames, frames_to_mp4, probe
from PIL import Image

from ce_plugin_video_seedvr2.chunks import blend_overlap, plan_chunks

__all__ = ["SeedVR2Adapter"]


class SeedVR2Adapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_video_seedvr2.backend import SeedVR2Backend

        assert self.paths is not None
        # the SeedVR code is part of the image (the `seedvr2` variant of the post family), not the model cache
        code_dir = Path(str(self.defaults.get("code_dir", "/opt/upstream/SeedVR")))
        return SeedVR2Backend(weights_dir=self.paths.model("seedvr2-3b"), code_dir=code_dir, defaults=self.defaults)

    async def run_video_upscale(self, request: m.UpscaleRequest, ctx: RunContext) -> m.VideoResult:
        backend = self.require_backend()
        d = self.defaults
        work = self.scratch(ctx, f"upscale-{request.labels.get('shot', 'clip')}")
        source = await ctx.read_artifact(request.video)
        info = probe(source)
        paths = extract_frames(source, work / "frames")
        if not paths:
            raise ValueError("video.upscale: the video has no frames")
        plan = plan_chunks(len(paths), int(d.get("chunk_frames", 33)), int(d.get("chunk_overlap", 4)))
        area = (request.target_width * request.target_height) ** 0.5
        outputs: list[tuple[tuple[int, int], list[np.ndarray]]] = []
        for index, (start, end) in enumerate(plan):
            frames = [np.asarray(Image.open(p).convert("RGB")) for p in paths[start:end]]
            restored = await self.call(ctx, backend.restore, frames, area, (int(ctx.seed) + index) % (2**31))
            outputs.append(((start, end), [np.asarray(f, dtype=np.uint8) for f in restored]))
        joined = blend_overlap(outputs, len(paths))
        fps = info.fps or 25.0
        raw = frames_to_mp4(joined, work / "seedvr2_raw.mp4", fps, audio=source if info.has_audio else None)
        out = conform_clip(
            raw, work / "upscaled.mp4", width=request.target_width, height=request.target_height,
            audio=source if info.has_audio else None,
        )  # fmt: skip
        result = probe(out)
        ref = await ctx.write_artifact(
            out,
            "video",
            {
                "adapter_id": self.manifest.id,
                "chunks": len(plan),
                "model_size": [int(joined[0].shape[1]), int(joined[0].shape[0])],
            },
            role="video",
            mime="video/mp4",
        )
        return m.VideoResult(
            video=ref, duration_s=round(result.duration_s, 4), fps=fps, width=result.width, height=result.height
        )
