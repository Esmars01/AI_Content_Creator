"""Screen recordings (§27): scene changes, keyframes, pixel and text diffs, dead time, speed
segments, animated zoom windows and the webcam bubble — on a synthetic recording drawn by FFmpeg."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from ce_core.boxes import fit_window, intersection, merge_boxes
from ce_render.ffmpeg import probe, run_ffmpeg
from ce_render.fonts import fonts_dir
from ce_render.screen import (
    Keyframe,
    ZoomPlan,
    dead_time,
    extract_frames,
    frame_size,
    keyframe_times,
    pixel_changes,
    recording_intervals,
    render_screen,
    scene_changes,
    text_diff,
    write_montage,
)
from ce_render.video import ComposeJob, Encode, Placement, compose

pytestmark = pytest.mark.slow

W, H = 1280, 720


def _text(text: str, x: int, y: int, start: float, end: float, size: int = 40) -> str:
    font = fonts_dir() / "NotoSans-Regular.ttf"
    return (
        f"drawtext=fontfile={font}:text='{text}':x={x}:y={y}:fontsize={size}:fontcolor=white"
        f":enable='between(t,{start},{end})'"
    )


async def _recording(path: Path) -> Path:
    """0–6 s a settings page; a cut to a blue billing page at 6 s; a line appears at 9 s
    (bottom right); nothing changes from 10 s to 18 s (dead time)."""
    vf = ",".join(
        [
            "drawbox=x=0:y=0:w=iw:h=60:color=0x333333:t=fill",
            _text("Settings", 20, 10, 0, 20),
            _text("Account preferences", 80, 160, 0, 20),
            _text("Notifications", 80, 240, 0, 20),
            "drawbox=x=0:y=0:w=iw:h=ih:color=0x2050a0:t=fill:enable='between(t,6,20)'",
            _text("Billing overview", 80, 160, 6, 20),
            _text("Invoice 2026-10 paid", 760, 520, 9, 20, 36),
        ]
    )
    await run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x101418:s={W}x{H}:r=30:d=18",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="module")
def recording(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return asyncio.run(_recording(tmp_path_factory.mktemp("screen") / "rec.mp4"))


def test_scene_changes_keyframes_and_diffs(recording: Path) -> None:
    async def run() -> tuple[list[float], list[float], list[np.ndarray]]:
        changes = await scene_changes(recording, threshold=10.0)
        times = keyframe_times(changes, 18.0, every_s=3.0)
        frames = await extract_frames(recording, times, frame_size(W, H, 1280))
        return changes, times, frames

    changes, times, frames = asyncio.run(run())
    assert changes == [pytest.approx(6.0, abs=0.05)]
    assert times[0] == 0.4 and any(abs(t - 6.4) < 0.06 for t in times) and any(5.7 < t < 6.0 for t in times)
    assert all(f.shape == (720, 1280, 3) for f in frames)
    by_t = dict(zip(times, frames, strict=True))
    # the cut changes most of the frame: no zoom target
    cut_before = max(t for t in times if t < 6)
    cut_after = min(t for t in times if t > 6)
    regions, fraction = pixel_changes(by_t[cut_before], by_t[cut_after])
    assert regions == [] and fraction > 0.5
    # the invoice line appears at 9 s in the bottom right: one region around it
    before = max(t for t in times if 6 < t < 9)
    after = min(t for t in times if t > 9)
    regions, fraction = pixel_changes(by_t[before], by_t[after])
    assert len(regions) == 1 and 0 < fraction < 0.1
    x, y, w, h = regions[0]
    assert 0.55 < x < 0.62 and 0.68 < y < 0.75 and 0.2 < w < 0.4 and h < 0.12
    # still frames: nothing changed
    assert pixel_changes(by_t[times[0]], by_t[times[1]]) == ([], 0.0)


def test_text_diff_and_dead_time() -> None:
    a = [
        {"text": "Settings", "bbox": [0.0, 0.0, 0.1, 0.05]},
        {"text": "Notifications", "bbox": [0.06, 0.33, 0.2, 0.05]},
    ]
    b = [
        {"text": "settings ", "bbox": [0.0, 0.0, 0.1, 0.05]},
        {"text": "Invoice paid", "bbox": [0.6, 0.72, 0.28, 0.06]},
    ]
    added, removed, regions = text_diff(a, b)
    assert added == ["Invoice paid"] and removed == ["notifications"] and regions == [[0.6, 0.72, 0.28, 0.06]]
    frames = [
        Keyframe(0.4, 0),
        Keyframe(6.4, 1, added=["Billing"]),
        Keyframe(9.4, 1, changed_regions=[[0.6, 0.7, 0.3, 0.1]]),
        Keyframe(12.4, 1),
    ]
    assert dead_time(frames, [6.0], 18.0, min_s=3.0) == [{"start_s": 9.9, "end_s": 11.9}]
    still = [Keyframe(0.4, 0), Keyframe(3.4, 0), Keyframe(5.8, 0), Keyframe(6.4, 1)]
    assert dead_time(still, [6.0], 18.0, min_s=3.0) == [{"start_s": 0.9, "end_s": 5.3}]
    assert keyframe_times([6.0], 18.0, every_s=3.0) == [0.4, 3.4, 5.8, 6.4, 9.4, 12.4, 15.4, 17.8]
    assert keyframe_times([], 0.5) == [0.4]


def test_boxes() -> None:
    merged = merge_boxes([[0.1, 0.1, 0.1, 0.05], [0.21, 0.1, 0.1, 0.05], [0.7, 0.7, 0.1, 0.1]])
    assert merged == [[0.1, 0.1, 0.21, 0.05], [0.7, 0.7, 0.1, 0.1]]
    window = fit_window([0.6, 0.72, 0.28, 0.06])
    assert window[2] == window[3] and window[0] + window[2] <= 1.0 and window[1] + window[3] <= 1.0
    assert intersection(window, [0.6, 0.72, 0.28, 0.06]) == pytest.approx(0.28 * 0.06)
    assert fit_window([0.0, 0.0, 1.0, 1.0]) == (0.0, 0.0, 1.0, 1.0)


def test_recording_intervals() -> None:
    assert recording_intervals([(2.0, 4.0, 3.0)], 10.0) == [(0.0, 2.0, 1.0), (2.0, 8.0, 3.0), (8.0, 14.0, 1.0)]
    assert recording_intervals([], 5.0) == [(0.0, 5.0, 1.0)]
    assert recording_intervals([(0.0, 1.0, 0.5)], 2.0) == [(0.0, 0.5, 0.5), (0.5, 1.5, 1.0)]


def _bright_box(frame: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(frame.mean(axis=2) > 200)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def test_render_screen_speed_and_zoom(recording: Path, tmp_path: Path) -> None:
    """Output 2–4 s plays recording 2–8 s at 3× (so output 5 s is recording 9 s); a snap zoom on
    the invoice line from output 5.5 s; letterboxed into 9:16."""
    zoom = ZoomPlan(5.5, 8.0, fit_window([0.59, 0.72, 0.3, 0.07]), "snap")

    async def run() -> list[np.ndarray]:
        out = await render_screen(
            recording, tmp_path / "shot.mp4", src_size=(W, H), width=540, height=960, fps=30, duration_s=9.0,
            zooms=[zoom], speed=[(2.0, 4.0, 3.0)],
        )  # fmt: skip
        info = await probe(out)
        assert (info.width, info.height) == (540, 960) and info.duration_s == pytest.approx(9.0, abs=0.05)
        return await extract_frames(out, [1.0, 3.5, 5.2, 6.5], (540, 960))

    early, sped, plain, zoomed = asyncio.run(run())
    # letterbox: the bars are dark, the band holds the 16:9 recording
    assert early[:200].mean() < 30 and early[400:560].mean() > 10
    # at output 3.5 s the recording is at 2 + 1.5 × 3 = 6.5 s: the blue page
    assert sped[480, 270, 2] > 120 and early[480, 270, 2] < 60
    # the zoom makes the invoice text larger
    x0, _, x1, _ = _bright_box(plain[500:640, 270:])
    zx0, _, zx1, _ = _bright_box(zoomed)
    assert (zx1 - zx0) > 1.8 * (x1 - x0)


def test_bubble_composites_the_base_in_a_circle(recording: Path, tmp_path: Path) -> None:
    async def run() -> np.ndarray:
        base = tmp_path / "base.mp4"
        await run_ffmpeg(
            ["-f", "lavfi", "-i", "color=c=red:s=360x640:r=30:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base)]
        )
        screen = tmp_path / "screen.mp4"
        await render_screen(recording, screen, src_size=(W, H), width=360, height=640, fps=30, duration_s=3.0)
        audio = tmp_path / "a.wav"
        await run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "4", str(audio)])
        job = ComposeJob(
            base=[Placement(base, 0.0, 0.0, 4.0)],
            overlays=[Placement(screen, 0.5, 0.5, 3.5, bubble=("bottom_right", 0.3))],
            titles=[],
            audio=audio,
            total_s=4.0,
            encode=Encode(
                width=360, height=640, fps=30.0, crf=18, max_bitrate_kbps=None, audio_bitrate_kbps=96, preset="veryfast"
            ),
        )
        out = await compose(job, tmp_path / "final.mp4")
        return (await extract_frames(out, [2.0], (360, 640)))[0]

    frame = asyncio.run(run())
    d = 108  # 0.3 × 360
    x, y = 360 - d - round(0.04 * 360), 640 - d - round(0.08 * 640)
    center = frame[y + d // 2, x + d // 2].astype(int)
    corner = frame[y + 2, x + 2].astype(int)  # inside the square, outside the circle: the screen shows
    assert center[0] > 150 and center[1] < 80  # the base track (red) in the bubble
    assert corner[0] < 60  # the dark letterbox of the screen overlay
    assert frame[320, 20].astype(int)[0] < 60  # the overlay covers the base elsewhere


def test_montage_is_one_frame_per_second(recording: Path, tmp_path: Path) -> None:
    async def run() -> float:
        frames = await extract_frames(recording, [0.4, 6.4, 9.4], (640, 360))
        out = await write_montage(frames, tmp_path / "keyframes.mp4")
        return (await probe(out)).duration_s

    assert asyncio.run(run()) == pytest.approx(3.0, abs=0.05)
