"""SeedVR2 adapter (Phase 8): chunk planning, overlap crossfades, exact target geometry and audio
carried through, on the CPU stand-in. No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import ffmpeg, probe
from ce_testing.engines import load_with_backend

from ce_plugin_video_seedvr2.chunks import blend_overlap, plan_chunks
from ce_plugin_video_seedvr2.testing import FakeSeedVR2Backend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_plan_chunks() -> None:
    assert plan_chunks(20, 33, 4) == [(0, 20)]
    assert plan_chunks(70, 33, 4) == [(0, 33), (29, 62), (37, 70)]
    assert plan_chunks(70, 30, 4) == [(0, 29), (25, 54), (41, 70)]  # size forced to 4k+1
    assert plan_chunks(0, 33, 4) == []


def test_blend_overlap_crossfades_shared_frames() -> None:
    a = [np.full((2, 2, 3), 0, np.uint8)] * 5
    b = [np.full((2, 2, 3), 200, np.uint8)] * 5
    out = blend_overlap([((0, 5), a), ((3, 8), b)], 8)
    assert [int(f[0, 0, 0]) for f in out] == [0, 0, 0, 66, 133, 200, 200, 200]
    with pytest.raises(ValueError, match="cover"):
        blend_overlap([((0, 5), a)], 8)


def test_upscale_to_exact_target_with_audio(tmp_path: Path) -> None:
    async def go() -> tuple[m.VideoResult, FakeSeedVR2Backend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=3)
        clip = tmp_path / "small.mp4"
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=180x320:r=25:d=2", "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip),
        )  # fmt: skip
        ref = await ctx.put_file(clip, "video")
        backend = FakeSeedVR2Backend()
        adapter = await load_with_backend(REGISTRY.get("seedvr2_3b"), backend, tmp_path)
        result = await adapter.run(
            "video.upscale", m.UpscaleRequest(video=ref, target_width=360, target_height=640), ctx
        )
        return result, backend, ctx  # type: ignore[return-value]

    result, backend, ctx = asyncio.run(go())
    assert (result.width, result.height, result.fps) == (360, 640, 25.0)
    assert [c[0] for c in backend.calls] == [33, 33] and result.video.meta["chunks"] == 2  # 50 frames
    info = probe(asyncio.run(ctx.read_artifact(result.video)))
    assert info.has_audio and abs(info.duration_s - 2.0) < 0.1
