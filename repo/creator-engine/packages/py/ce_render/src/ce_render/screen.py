"""Screen recordings (§27 `ScreenAnalysisWorkflow`, Phase 7): scene-change detection, keyframes,
pixel and OCR text diffs between keyframes, dead time, zoom planning and the screen-shot render
(typed zoom windows with easing, speed segments).

Detection uses FFmpeg's `scdet` filter (a scene score per frame). OCR runs through the `vision.ocr`
route and the "what happens when" summary through `vision.video` — both in `ce_exec.screen`.
Everything here is deterministic media arithmetic over normalized `(x, y, w, h)` boxes (0..1);
planning zooms, speed segments and the webcam bubble from an analysis is `ce_director.screen`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from ce_core.boxes import merge_boxes

from ce_render.ffmpeg import run_ffmpeg

__all__ = [
    "ANALYSIS_VERSION",
    "Keyframe",
    "ZoomPlan",
    "dead_time",
    "extract_frames",
    "frame_size",
    "keyframe_times",
    "pixel_changes",
    "recording_intervals",
    "render_screen",
    "scene_changes",
    "text_diff",
    "write_montage",
    "zoom_expression",
]

ANALYSIS_VERSION = "1"
_TIME = re.compile(r"lavfi\.scd\.time=([\d.]+)")
Box = list[float]


# ---------------------------------------------------------------------- detection


async def scene_changes(path: Path, *, threshold: float = 10.0) -> list[float]:
    """Times (s) where FFmpeg's scene score (`scdet`, 0–100) exceeds `threshold`."""
    out = await run_ffmpeg(
        [
            "-i",
            str(path),
            "-an",
            "-vf",
            f"scdet=threshold={threshold:g}:sc_pass=1,metadata=print:file=-",
            "-f",
            "null",
            "-",
        ]
    )
    return sorted({round(float(m), 3) for m in _TIME.findall(out.decode("utf-8", "replace"))})


def keyframe_times(
    changes: Sequence[float], duration_s: float, *, settle_s: float = 0.4, every_s: float = 8.0, max_count: int = 60
) -> list[float]:
    """Per scene (between cuts): a keyframe once the screen settled after the cut, one every
    `every_s` (typing changes text without a cut) and one just before the scene ends, so every
    stretch of a scene is observed at both ends."""
    starts = [0.0, *[c for c in changes if 0 < c < duration_s]]
    times: list[float] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else duration_s
        first = len(times)
        t = min(start + settle_s, max(start, end - 0.05))
        while t < end - 1.0 or len(times) == first:
            if len(times) >= max_count or t >= end:
                break
            times.append(round(t, 3))
            t += every_s
        last = round(end - min(0.2, (end - start) / 4), 3)
        if len(times) < max_count and (len(times) == first or last > times[-1] + 0.5):
            times.append(last)
    return times


def frame_size(width: int, height: int, max_side: int) -> tuple[int, int]:
    """Even dimensions with the longer side at most `max_side`."""
    scale = min(1.0, max_side / max(width, height))
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


async def extract_frames(path: Path, times: Sequence[float], size: tuple[int, int]) -> list[np.ndarray]:
    """RGB frames at `times`, scaled to `size` (one accurate seek per frame)."""
    w, h = size
    frames: list[np.ndarray] = []
    for t in times:
        raw = await run_ffmpeg(
            ["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-"]
        )  # fmt: skip
        if len(raw) < w * h * 3:  # past the last frame: repeat the previous one
            frames.append(frames[-1] if frames else np.zeros((h, w, 3), np.uint8))
            continue
        frames.append(np.frombuffer(raw[: w * h * 3], np.uint8).reshape(h, w, 3))
    return frames


async def write_montage(frames: Sequence[np.ndarray], out: Path) -> Path:
    """The keyframes as a 1 fps clip (frame i at t = i s): one OCR call reads them all."""
    h, w = frames[0].shape[:2]
    data = b"".join(np.ascontiguousarray(f).tobytes() for f in frames)
    await run_ffmpeg(
        ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", "1", "-i", "-", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p", "-g", "1", str(out)],
        input_bytes=data,
    )  # fmt: skip
    return out


# ---------------------------------------------------------------------- diffs


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def text_diff(
    previous: Sequence[dict[str, Any]], current: Sequence[dict[str, Any]]
) -> tuple[list[str], list[str], list[Box]]:
    """OCR lines added and removed between two keyframes, and where the added lines are."""
    before = {_norm(b["text"]) for b in previous}
    after = {_norm(b["text"]) for b in current}
    added = [b for b in current if _norm(b["text"]) not in before]
    removed = sorted(before - after)
    return [b["text"] for b in added], removed, merge_boxes([b["bbox"] for b in added])


def pixel_changes(
    previous: np.ndarray,
    current: np.ndarray,
    *,
    threshold: int = 24,
    cell: int = 16,
    min_area: float = 0.0015,
    full: float = 0.5,
) -> tuple[list[Box], float]:
    """Regions whose pixels changed between two keyframes (luma difference above `threshold` in
    more than 2% of a `cell`×`cell` block), merged; and the changed fraction of the frame. A change
    over more than `full` of the frame (a cut, a page load) returns no regions — nothing to zoom on."""
    from scipy import ndimage

    def luma(rgb: np.ndarray) -> np.ndarray:
        return (
            rgb[..., 0].astype(np.int16) * 3 // 10
            + rgb[..., 1].astype(np.int16) * 59 // 100
            + rgb[..., 2].astype(np.int16) * 11 // 100
        )

    h, w = previous.shape[:2]
    gh, gw = max(1, h // cell), max(1, w // cell)
    diff = np.abs(luma(current) - luma(previous)) > threshold
    grid = diff[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).mean(axis=(1, 3)) > 0.02
    fraction = float(grid.mean())
    if fraction > full or not grid.any():
        return [], round(fraction, 4)
    labels, _ = ndimage.label(grid, structure=np.ones((3, 3), dtype=int))
    boxes = []
    for sl in ndimage.find_objects(labels):
        if sl is None:
            continue
        ys, xs = sl
        box = [xs.start / gw, ys.start / gh, (xs.stop - xs.start) / gw, (ys.stop - ys.start) / gh]
        if box[2] * box[3] >= min_area:
            boxes.append(box)
    return merge_boxes(boxes), round(fraction, 4)


@dataclass
class Keyframe:
    t_s: float
    scene: int
    ocr: list[dict[str, Any]] = field(default_factory=list)  # {text, bbox (x, y, w, h), confidence}
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed_regions: list[Box] = field(default_factory=list)
    changed_fraction: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "t_s": self.t_s,
            "scene": self.scene,
            "ocr": self.ocr,
            "added": self.added,
            "removed": self.removed,
            "changed_regions": self.changed_regions,
            "changed_fraction": self.changed_fraction,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Keyframe:
        return cls(
            **{
                k: data[k]
                for k in ("t_s", "scene", "ocr", "added", "removed", "changed_regions", "changed_fraction")
                if k in data
            }
        )


def dead_time(
    keyframes: Sequence[Keyframe],
    changes: Sequence[float],
    duration_s: float,
    *,
    min_s: float = 3.0,
    margin_s: float = 0.5,
) -> list[dict[str, float]]:
    """Stretches of a scene where consecutive keyframes show no text or pixel change, at least
    `min_s` long (speed-up candidates), each shrunk by `margin_s` so the action around them plays
    at 1×. A keyframe that changed makes the whole stretch since the previous one active: the
    change happened somewhere inside it."""
    del changes, duration_s  # the keyframes' scenes already split at cuts
    quiet: list[list[float]] = []
    for prev, cur in itertools.pairwise(keyframes):
        if prev.scene != cur.scene or cur.added or cur.removed or cur.changed_regions or cur.changed_fraction > 0:
            continue
        if quiet and quiet[-1][1] == prev.t_s:
            quiet[-1][1] = cur.t_s
        else:
            quiet.append([prev.t_s, cur.t_s])
    out = [{"start_s": round(a + margin_s, 3), "end_s": round(b - margin_s, 3)} for a, b in quiet if b - a >= min_s]
    return [d for d in out if d["end_s"] - d["start_s"] >= 1.0]


@dataclass(frozen=True)
class ZoomPlan:
    start_s: float  # output time, relative to the shot start
    end_s: float
    rect: tuple[float, float, float, float]
    ease: str = "ease_in_out"


def recording_intervals(
    speed: Sequence[tuple[float, float, float]], output_s: float
) -> list[tuple[float, float, float]]:
    """Speed segments in output time `(start_s, end_s, speed)` → contiguous recording intervals
    `(rec_start_s, rec_end_s, speed)` covering `output_s` of output (1× between segments)."""
    out: list[tuple[float, float, float]] = []
    t_out = rec = 0.0
    for a, b, s in sorted(speed):
        a, b = max(a, t_out), min(b, output_s)
        if b <= a:
            continue
        if a > t_out:
            out.append((rec, rec + (a - t_out), 1.0))
            rec += a - t_out
        out.append((rec, rec + (b - a) * s, s))
        rec += (b - a) * s
        t_out = b
    if output_s > t_out:
        out.append((rec, rec + (output_s - t_out), 1.0))
    return [(round(a, 6), round(b, 6), s) for a, b, s in out if b - a > 1e-3]


def _ease(ease: str, u: str) -> str:
    if ease == "snap":
        return f"gt({u}\\,0)"
    if ease == "linear":
        return u
    if ease == "ease_out":
        return f"(1-(1-{u})*(1-{u}))"
    return f"(3*{u}*{u}-2*{u}*{u}*{u})"  # smoothstep


def zoom_expression(zooms: Sequence[ZoomPlan], index: int, *, ramp_s: float = 0.35, var: str = "t") -> str:
    """An FFmpeg expression of time (`var`) for one rect component (0 x, 1 y, 2 w, 3 h): the full
    frame, easing into each zoom over `ramp_s` and back out before its end."""
    full = (0.0, 0.0, 1.0, 1.0)[index]
    expr = f"{full}"
    for z in sorted(zooms, key=lambda z: z.start_s, reverse=True):
        target = z.rect[index]
        a, b = z.start_s, z.end_s
        ramp = min(ramp_s, max(0.05, (b - a) / 3))
        u_in = f"min(1\\,max(0\\,({var}-{a:.3f})/{ramp:.3f}))"
        u_out = f"min(1\\,max(0\\,({b:.3f}-{var})/{ramp:.3f}))"
        k = f"min({_ease(z.ease, u_in)}\\,{_ease(z.ease, u_out)})"
        expr = f"if(between({var}\\,{a:.3f}\\,{b:.3f})\\,{full}+({target}-({full}))*{k}\\,{expr})"
    return expr


async def render_screen(
    src: Path,
    out: Path,
    *,
    src_size: tuple[int, int],
    width: int,
    height: int,
    fps: float,
    duration_s: float,
    zooms: Sequence[ZoomPlan] = (),
    speed: Sequence[tuple[float, float, float]] = (),
) -> Path:
    """The screen shot (§27): `duration_s` of output; speed segments (output time) pick and retime
    the recording, then animated zoom windows (output time), fitted into `width`×`height`
    (letterboxed on a dark background when the recording's shape differs). Past the recording's
    end the last frame holds.

    Zooms run through `zoompan` from the source resolution straight to the fitted size (sharp
    text, no intermediate upscale), centered on the window; a non-uniform window zooms by its
    larger side."""
    graph: list[str] = []
    parts = recording_intervals(speed, duration_s) if speed else [(0.0, duration_s, 1.0)]
    names = []
    for i, (a, b, s) in enumerate(parts):
        hold = f",tpad=stop_mode=clone:stop_duration={(b - a) / s + 1:.3f}" if i == len(parts) - 1 else ""
        graph.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{s:g},fps={fps:g}{hold}[p{i}]")
        names.append(f"[p{i}]")
    graph.append(
        "".join(names) + f"concat=n={len(names)}:v=1:a=0,trim=duration={duration_s:.3f},setpts=PTS-STARTPTS[timed]"
    )
    sw, sh = src_size
    scale = min(width / sw, height / sh)
    fw, fh = max(2, int(sw * scale) // 2 * 2), max(2, int(sh * scale) // 2 * 2)
    if zooms:
        # zoompan evaluates per frame at a fixed output size (`crop` cannot follow a changing input)
        x, y, w, h = (zoom_expression(zooms, i, var="it") for i in range(4))
        z = f"1/max({w}\\,{h})"
        cx, cy = f"({x}+({w})/2)", f"({y}+({h})/2)"
        fit = (
            f"zoompan=z='{z}':x='clip(iw*{cx}-iw/zoom/2\\,0\\,iw-iw/zoom)'"
            f":y='clip(ih*{cy}-ih/zoom/2\\,0\\,ih-ih/zoom)':d=1:s={fw}x{fh}:fps={fps:g}"
        )
    else:
        fit = f"scale={fw}:{fh}"
    graph.append(f"[timed]{fit},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x111111,setsar=1,format=yuv420p[out]")
    await run_ffmpeg(
        ["-i", str(src), "-filter_complex", ";".join(graph), "-map", "[out]", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "16", "-pix_fmt", "yuv420p", "-an", "-t", f"{duration_s:.3f}", str(out)]
    )  # fmt: skip
    return out
