"""RIFE adapter (Phase 8): frame-rate plans, scene-cut handling and the output rate, on the CPU
stand-in. No model runs (rule 5)."""

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

from ce_plugin_video_rife.plan import Step, is_cut, plan_steps
from ce_plugin_video_rife.testing import FakeRifeBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_plan_16_to_24() -> None:
    steps = plan_steps(16, 16, 24)
    assert len(steps) == 24
    assert steps[:4] == [Step(0, 0, 0.0), Step(0, 1, 0.666667), Step(1, 2, 0.333333), Step(2, 2, 0.0)]
    assert all(s.copy for s in steps[::3])


def test_plan_same_rate_copies_and_rejects_bad_rates() -> None:
    assert all(s.copy for s in plan_steps(10, 25, 25))
    with pytest.raises(ValueError):
        plan_steps(10, 0, 25)


def test_cut_detection() -> None:
    dark, light = np.zeros((32, 32, 3), np.uint8), np.full((32, 32, 3), 200, np.uint8)
    assert is_cut(dark, light, 40.0) and not is_cut(dark, dark + 5, 40.0)


def _clip(tmp: Path, fps: int, seconds: float, *, cut: bool = False) -> Path:
    path = tmp / "src.mp4"
    if cut:
        half = seconds / 2
        black, white = (f"color=c={c}:s=64x64:r={fps}:d={half}" for c in ("black", "white"))
        ffmpeg(
            "-f", "lavfi", "-i", black, "-f", "lavfi", "-i", white, "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        )  # fmt: skip
    else:
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s=64x64:r={fps}:d={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        )
    return path


def _run(tmp: Path, clip: Path, target: float) -> tuple[m.VideoResult, FakeRifeBackend, LocalRunContext]:
    async def go() -> tuple[m.VideoResult, FakeRifeBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp / "ctx", seed=1)
        ref = await ctx.put_file(clip, "video")
        backend = FakeRifeBackend()
        adapter = await load_with_backend(REGISTRY.get("rife_425"), backend, tmp)
        return (
            await adapter.run("video.interpolate", m.InterpolateRequest(video=ref, target_fps=target), ctx),
            backend,
            ctx,
        )  # type: ignore[return-value]

    return asyncio.run(go())


def test_16fps_clip_to_24fps(tmp_path: Path) -> None:
    result, _, ctx = _run(tmp_path, _clip(tmp_path, 16, 1.0), 24)
    # 24 frames: every third falls on a source frame, and the last one holds the final source frame
    assert result.fps == 24.0 and result.video.meta["interpolated_frames"] == 15
    info = probe(asyncio.run(ctx.read_artifact(result.video)))
    assert abs(info.fps - 24.0) < 0.01 and abs(info.duration_s - 1.0) < 0.06 and (info.width, info.height) == (64, 64)


def test_scene_cut_repeats_frames(tmp_path: Path) -> None:
    result, backend, _ = _run(tmp_path, _clip(tmp_path, 16, 1.0, cut=True), 24)
    assert result.video.meta["cut_frames"] >= 1
    assert sum(len(c) for c in backend.calls) == result.video.meta["interpolated_frames"]
