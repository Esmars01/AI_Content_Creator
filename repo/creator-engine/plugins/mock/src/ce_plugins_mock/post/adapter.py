"""Mock upscaler and frame interpolator: FFmpeg lanczos scaling and fps conversion (labelled mock)."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import FrameInterpolator, Upscaler
from ce_contracts.models import InterpolateRequest, UpscaleRequest, VideoResult

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import probe_duration, run_ffmpeg

__all__ = ["MockPost"]


class MockPost(MockAdapter, Upscaler, FrameInterpolator):
    async def run_video_upscale(self, request: UpscaleRequest, ctx: RunContext) -> VideoResult:
        source = await ctx.read_artifact(request.video)
        out = self.workdir(ctx, "upscale") / "upscaled.mp4"
        w, h = request.target_width - request.target_width % 2, request.target_height - request.target_height % 2
        await run_ffmpeg(
            ["-i", str(source), "-vf", f"scale={w}:{h}:flags=lanczos,setsar=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-an", str(out)]
        )  # fmt: skip
        ref = await self.write(ctx, out, "video", role="video", mime="video/mp4", upscaled_to=f"{w}x{h}")
        return VideoResult(
            video=ref, duration_s=await probe_duration(out), fps=float(request.labels.get("fps", 25)), width=w, height=h
        )

    async def run_video_interpolate(self, request: InterpolateRequest, ctx: RunContext) -> VideoResult:
        source = await ctx.read_artifact(request.video)
        out = self.workdir(ctx, "interpolate") / "interpolated.mp4"
        await run_ffmpeg(
            ["-i", str(source), "-vf", f"fps={request.target_fps:g}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-an", str(out)]
        )  # fmt: skip
        ref = await self.write(ctx, out, "video", role="video", mime="video/mp4", fps=request.target_fps)
        width, height = int(request.labels.get("width", 2)), int(request.labels.get("height", 2))
        return VideoResult(
            video=ref, duration_s=await probe_duration(out), fps=request.target_fps, width=width, height=height
        )
