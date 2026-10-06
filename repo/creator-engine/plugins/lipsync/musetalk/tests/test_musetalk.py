"""MuseTalk adapter (Phase 8): frame cycling, box smoothing, the lower-face blend, passthrough of
faceless frames and the source frame rate, on the CPU stand-in. Nothing here runs a model
(`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import extract_frames, ffmpeg, probe
from ce_testing.engines import load_with_backend
from PIL import Image

from ce_plugin_lipsync_musetalk.backend import FACE_OVAL, musetalk_box
from ce_plugin_lipsync_musetalk.patch import Face, blend, crop_box, pingpong, select_face, smooth_boxes
from ce_plugin_lipsync_musetalk.testing import FakeMuseTalkBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_pingpong_cycles_forward_then_back() -> None:
    assert [pingpong(i, 3) for i in range(8)] == [0, 1, 2, 2, 1, 0, 0, 1]
    with pytest.raises(ValueError):
        pingpong(0, 0)


def test_smoothing_keeps_gaps_and_averages_neighbours() -> None:
    boxes = [(10, 10, 50, 50), (12, 10, 52, 50), None, (20, 10, 60, 50)]
    out = smooth_boxes(boxes, 3)
    assert out[2] is None and out[0] == (11, 10, 51, 50) and out[3] == (20, 10, 60, 50)


def test_select_face_prefers_largest_or_the_region() -> None:
    big, small = Face((100, 100, 300, 300)), Face((0, 400, 50, 450))
    assert select_face([small, big], None, (360, 640)) is big
    assert select_face([small, big], (0.0, 0.6, 0.2, 0.8), (360, 640)) is small
    assert select_face([big], (0.0, 0.0, 0.1, 0.1), (360, 640)) is None


def test_musetalk_box_mirrors_the_upper_edge_about_the_nose() -> None:
    points = np.zeros((478, 2))
    points[list(FACE_OVAL)] = [(100 + i, 200) for i in range(len(FACE_OVAL))]
    points[152] = (150, 400)  # chin
    points[195] = (150, 300)  # nose bridge
    assert musetalk_box(points) == (100, 200, 150, 400)


def test_blend_touches_only_the_lower_face() -> None:
    frame = np.full((200, 200, 3), 120, dtype=np.uint8)
    box = crop_box((60, 40, 140, 140), (200, 200), 10)
    generated = np.zeros((box[3] - box[1], box[2] - box[0], 3), dtype=np.uint8)
    out = blend(frame, generated, Face(box), box)
    diff = np.abs(out.astype(int) - frame.astype(int)).sum(axis=2)
    assert diff[:88].max() == 0  # above the middle of the expanded square (less the feather): untouched
    assert diff[130, 100] > 0  # the mouth area changed
    assert diff[:, :50].max() == 0 and diff[:, 150:].max() == 0  # outside the box: untouched


def _clip(tmp: Path, fps: int, seconds: float) -> Path:
    path = tmp / f"clip_{fps}.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", f"color=c=0x8090a0:s=320x480:r={fps}:d={seconds}",
        "-vf", "drawbox=x=96:y=96:w=128:h=144:color=0xd0b090:t=fill",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    return path


def _speech(tmp: Path, seconds: float) -> Path:
    path = tmp / "speech.wav"
    ffmpeg(
        "-f", "lavfi", "-i", f"sine=frequency=180:duration={seconds}:sample_rate=48000", "-af", "volume=0.6", str(path)
    )
    return path


async def _run(
    tmp: Path, backend: FakeMuseTalkBackend, *, fps: int, video_s: float, audio_s: float, **kw: Any
) -> tuple[m.VideoResult, LocalRunContext]:
    ctx = LocalRunContext(tmp / "ctx", seed=11)
    video = await ctx.put_file(_clip(tmp, fps, video_s), "video")
    audio = await ctx.put_file(_speech(tmp, audio_s), "audio")
    adapter = await load_with_backend(REGISTRY.get("musetalk_v15"), backend, tmp)
    result = await adapter.run(
        "lipsync.dub", m.LipSyncRequest(video=video, audio=audio, labels={"shot": "sht_a"}, **kw), ctx
    )
    return result, ctx  # type: ignore[return-value]


def test_dub_patches_every_frame_and_keeps_size(tmp_path: Path) -> None:
    backend = FakeMuseTalkBackend()
    result, ctx = asyncio.run(_run(tmp_path, backend, fps=25, video_s=1.0, audio_s=1.0))
    assert (result.width, result.height, result.fps) == (320, 480, 25.0)
    assert result.video.meta["patched_frames"] == 25 and result.video.meta["passthrough_frames"] == 0
    assert abs(result.duration_s - 1.0) < 0.1
    assert [c[0] for c in backend.generate_calls] == [8, 8, 8, 1]
    path = asyncio.run(ctx.read_artifact(result.video))
    assert probe(path).has_audio


def test_longer_audio_cycles_frames_and_faceless_frames_pass_through(tmp_path: Path) -> None:
    backend = FakeMuseTalkBackend(faceless={0, 1, 2})
    result, _ = asyncio.run(_run(tmp_path, backend, fps=25, video_s=0.6, audio_s=1.2))
    # 30 output frames over 15 source frames (forward then back); sources 0-2 have no face
    assert result.video.meta["patched_frames"] + result.video.meta["passthrough_frames"] == 30
    assert result.video.meta["passthrough_frames"] == 6
    assert abs(result.duration_s - 1.2) < 0.1


def test_a_30fps_clip_comes_back_at_30fps(tmp_path: Path) -> None:
    result, ctx = asyncio.run(_run(tmp_path, FakeMuseTalkBackend(), fps=30, video_s=1.0, audio_s=1.0))
    assert result.fps == 30.0
    info = probe(asyncio.run(ctx.read_artifact(result.video)))
    assert abs(info.fps - 30.0) < 0.01 and (info.width, info.height) == (320, 480)


def test_face_region_picks_the_face(tmp_path: Path) -> None:
    backend = FakeMuseTalkBackend(two_faces=True)
    result, ctx = asyncio.run(
        _run(tmp_path, backend, fps=25, video_s=0.4, audio_s=0.4, face_region=(0.0, 0.55, 0.3, 0.8))
    )
    assert result.video.meta["patched_frames"] == 10
    frames = extract_frames(asyncio.run(ctx.read_artifact(result.video)), tmp_path / "out")
    source = extract_frames(_clip(tmp_path, 25, 0.4), tmp_path / "src")
    a = np.asarray(Image.open(frames[5]).convert("RGB"), dtype=int)
    b = np.asarray(Image.open(source[5]).convert("RGB"), dtype=int)
    diff = np.abs(a - b).sum(axis=2)
    # the main face's mouth area is not repainted (beyond codec noise); the left face's is
    assert diff[200:240, 120:200].mean() < 6
