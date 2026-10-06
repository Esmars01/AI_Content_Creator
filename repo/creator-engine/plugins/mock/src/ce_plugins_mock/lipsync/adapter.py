"""Mock lip-sync patch: remux the new audio onto the clip and label it (§12.1 `lipsync.patch`)."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import LipSyncEngine
from ce_contracts.models import LipSyncRequest, VideoResult

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import probe_duration, run_ffmpeg

__all__ = ["MockLipSync"]


class MockLipSync(MockAdapter, LipSyncEngine):
    async def run_lipsync_dub(self, request: LipSyncRequest, ctx: RunContext) -> VideoResult:
        video = await ctx.read_artifact(request.video)
        audio = await ctx.read_artifact(request.audio)
        out = self.workdir(ctx, "dub") / "patched.mp4"
        await run_ffmpeg(
            [
                "-i",
                str(video),
                "-i",
                str(audio),
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                str(out),
            ]
        )
        duration = await probe_duration(out)
        ref = await self.write(ctx, out, "video", role="video", mime="video/mp4", patched=True)
        width, height = int(request.labels.get("width", 0) or 2), int(request.labels.get("height", 0) or 2)
        return VideoResult(
            video=ref, duration_s=duration, fps=float(request.labels.get("fps", 25)), width=width, height=height
        )
