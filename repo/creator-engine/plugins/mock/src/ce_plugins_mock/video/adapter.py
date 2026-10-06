"""Mock video engine: a labelled moving gradient for the requested duration (§37 B-roll mock)."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import VideoGenerator
from ce_contracts.models import VideoGenerateRequest, VideoResult

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import render_gradient_clip, seed_int

__all__ = ["MockVideo"]


class MockVideo(MockAdapter, VideoGenerator):
    async def _clip(self, capability: str, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        work = self.workdir(ctx, capability.replace(".", "_"))
        out = work / "clip.mp4"
        first = await ctx.read_artifact(request.first_frame) if request.first_frame else None
        lines = [f"MOCK {capability.upper()}", request.labels.get("shot", ""), request.prompt[:60]]
        if request.labels.get("take"):
            lines.append(f"take {request.labels['take']}")
        await render_gradient_clip(
            out,
            width=request.width,
            height=request.height,
            fps=request.fps,
            duration_s=request.duration_s,
            seed=seed_int(ctx.seed, request.prompt),
            lines=[line for line in lines if line],
            first_frame=first,
            workdir=work,
        )
        ref = await self.write(ctx, out, "video", role="video", mime="video/mp4", duration_s=request.duration_s)
        return VideoResult(
            video=ref, duration_s=request.duration_s, fps=request.fps, width=request.width, height=request.height
        )

    async def run_video_t2v(self, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        return await self._clip("video.t2v", request, ctx)

    async def run_video_i2v(self, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        return await self._clip("video.i2v", request, ctx)

    async def run_video_r2v(self, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        return await self._clip("video.r2v", request, ctx)

    async def run_video_edit(self, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        return await self._clip("video.edit", request, ctx)

    async def run_video_extend(self, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        return await self._clip("video.extend", request, ctx)

    async def run_video_joint_av(self, request: VideoGenerateRequest, ctx: RunContext) -> VideoResult:
        return await self._clip("video.joint_av", request, ctx)
