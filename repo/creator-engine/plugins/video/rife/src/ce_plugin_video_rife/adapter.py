"""Practical-RIFE 4.25 adapter (`video.interpolate`, §26: Wan's 16 fps clips to the timeline rate,
and other frame-rate conversions).

The plan (`plan.py`) maps every output frame to a source pair and timestep; pairs are sent to the
backend with all the timesteps they need in one call; copies and scene cuts (nearest source frame)
never reach the model. The result keeps the source size and audio."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import extract_frames, frames_dir_to_mp4, probe
from PIL import Image

from ce_plugin_video_rife.plan import is_cut, plan_steps

__all__ = ["RifeAdapter"]


class RifeAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_video_rife.backend import RifeBackend

        assert self.paths is not None
        return RifeBackend(train_log=self.paths.model("rife-4.25") / "train_log", defaults=self.defaults)

    async def run_video_interpolate(self, request: m.InterpolateRequest, ctx: RunContext) -> m.VideoResult:
        backend = self.require_backend()
        work = self.scratch(ctx, f"interpolate-{request.labels.get('shot', 'clip')}")
        source = await ctx.read_artifact(request.video)
        info = probe(source)
        paths = extract_frames(source, work / "frames")
        if not paths:
            raise ValueError("video.interpolate: the video has no frames")
        steps = plan_steps(len(paths), info.fps or float(request.target_fps), float(request.target_fps))
        threshold = float(self.defaults.get("cut_threshold", 40.0))
        cache: dict[int, np.ndarray] = {}

        def frame(i: int) -> np.ndarray:
            if i not in cache:
                cache[i] = np.asarray(Image.open(paths[i]).convert("RGB"))
            return cache[i]

        pairs: dict[tuple[int, int], list[float]] = defaultdict(list)
        cuts = 0
        for step in steps:
            if not step.copy:
                pairs[(step.a, step.b)].append(step.t)
        generated: dict[tuple[int, int, float], np.ndarray] = {}
        cut_pairs: set[tuple[int, int]] = set()
        for (a, b), times in sorted(pairs.items()):
            if is_cut(frame(a), frame(b), threshold):
                cut_pairs.add((a, b))
                continue
            outputs = await self.call(ctx, backend.interpolate, frame(a), frame(b), times)
            generated.update({(a, b, t): np.asarray(o, dtype=np.uint8) for t, o in zip(times, outputs, strict=True)})
            for i in [k for k in cache if k < a]:  # frames are visited in order: drop the ones behind
                del cache[i]
        out_dir = work / "out"
        out_dir.mkdir(exist_ok=True)
        for index, step in enumerate(steps):
            if step.copy:
                image = frame(step.a)
            elif (step.a, step.b) in cut_pairs:
                cuts += 1
                image = frame(step.a if step.t < 0.5 else step.b)
            else:
                image = generated[(step.a, step.b, step.t)]
            Image.fromarray(image).save(out_dir / f"{index:08d}.png")
        fps = float(request.target_fps)
        out = frames_dir_to_mp4(out_dir, work / "interpolated.mp4", fps, audio=source if info.has_audio else None)
        result = probe(out)
        ref = await ctx.write_artifact(
            out,
            "video",
            {
                "adapter_id": self.manifest.id,
                "source_fps": info.fps,
                "interpolated_frames": len(generated),
                "cut_frames": cuts,
            },
            role="video",
            mime="video/mp4",
        )
        return m.VideoResult(
            video=ref, duration_s=round(result.duration_s, 4), fps=fps, width=result.width, height=result.height
        )
