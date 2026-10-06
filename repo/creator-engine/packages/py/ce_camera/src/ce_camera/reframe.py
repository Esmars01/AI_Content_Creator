"""Reframing (§22 `ce_camera.reframe`): subject tracks → smoothed crop path → per-aspect crop
windows, a crop-loss metric, and the composition fallback above the threshold.

1. Track: `face.detect` boxes (normalized x, y, w, h, score) per sample — the largest face.
2. Smooth: One-Euro filter on the box centre (low jitter at rest, low lag in motion).
3. Crop windows: for a target aspect inside a source frame, the window centred on the subject
   (clamped to the frame), with the subject's eye line kept near the profile's `eye_line`.
4. Crop loss: the mean fraction of the subject box (padded by `margin`) outside the window.
   Above `threshold` the composition falls back to a layout (blurred fill: the whole source fitted
   over a blurred, scaled-up copy); native regeneration in the target aspect is only offered with
   user approval (it costs GPU time).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CropPlan",
    "OneEuro",
    "SubjectTrack",
    "crop_loss",
    "piecewise_expression",
    "plan_crop",
    "smooth_track",
    "track_from_detections",
]


@dataclass(frozen=True)
class SubjectTrack:
    """Normalized subject boxes over time: (t_s, cx, cy, w, h); empty = no subject found."""

    samples: tuple[tuple[float, float, float, float, float], ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.samples)

    def as_list(self) -> list[list[float]]:
        return [[round(v, 4) for v in s] for s in self.samples]

    @classmethod
    def from_list(cls, data: Sequence[Sequence[float]] | None) -> SubjectTrack:
        return cls(tuple((float(a), float(b), float(c), float(d), float(e)) for a, b, c, d, e in (data or [])))


def track_from_detections(frames: Sequence[dict[str, object]], *, min_score: float = 0.5) -> SubjectTrack:
    out = []
    for frame in frames:
        found: Any = frame.get("boxes") or []
        boxes = [b for b in found if len(b) >= 5 and float(b[4]) >= min_score]
        if not boxes:
            continue
        x, y, w, h, _ = max(boxes, key=lambda b: float(b[2]) * float(b[3]))
        out.append((float(frame["t_s"]), float(x) + float(w) / 2, float(y) + float(h) / 2, float(w), float(h)))  # type: ignore[arg-type]
    return SubjectTrack(tuple(out))


class OneEuro:
    """The One-Euro filter (Casiez et al. 2012)."""

    def __init__(self, min_cutoff: float = 0.6, beta: float = 0.7, d_cutoff: float = 1.0) -> None:
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x: float | None = None
        self.dx = 0.0
        self.t: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, t: float, x: float) -> float:
        if self.x is None or self.t is None:
            self.x, self.t = x, t
            return x
        dt = max(1e-3, t - self.t)
        dx = (x - self.x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self.dx = a_d * dx + (1 - a_d) * self.dx
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        a = self._alpha(cutoff, dt)
        self.x = a * x + (1 - a) * self.x
        self.t = t
        return self.x


def smooth_track(track: SubjectTrack, *, min_cutoff: float = 0.6, beta: float = 0.7) -> SubjectTrack:
    fx, fy, fw, fh = (OneEuro(min_cutoff, beta) for _ in range(4))
    return SubjectTrack(tuple((t, fx(t, cx), fy(t, cy), fw(t, w), fh(t, h)) for t, cx, cy, w, h in track.samples))


@dataclass(frozen=True)
class CropPlan:
    """A crop window path in normalized source coordinates: (t_s, x0, y0) with a fixed size."""

    width: float  # window width / source width
    height: float
    path: tuple[tuple[float, float, float], ...]
    loss: float
    layout: str  # "crop" | "blurred_fill"

    def offsets_px(self, src_w: int, src_h: int) -> list[tuple[float, float, float]]:
        return [(t, x * src_w, y * src_h) for t, x, y in self.path]


def _window(src_aspect: float, dst_aspect: float, zoom: float = 1.0) -> tuple[float, float]:
    """Largest window of `dst_aspect` inside a source of `src_aspect` (normalized units)."""
    if dst_aspect <= src_aspect:  # narrower target: full height
        return dst_aspect / src_aspect / zoom, 1.0 / zoom
    return 1.0 / zoom, src_aspect / dst_aspect / zoom


def crop_loss(
    track: SubjectTrack, width: float, height: float, path: Sequence[tuple[float, float, float]], margin: float = 0.15
) -> float:
    if not track.samples or not path:
        return 0.0
    losses = []
    for (_t, cx, cy, w, h), (_, x0, y0) in zip(track.samples, path, strict=False):
        bw, bh = w * (1 + 2 * margin), h * (1 + 2 * margin)
        bx0, by0 = cx - bw / 2, cy - bh / 2
        ix = max(0.0, min(bx0 + bw, x0 + width) - max(bx0, x0))
        iy = max(0.0, min(by0 + bh, y0 + height) - max(by0, y0))
        area = bw * bh
        losses.append(1.0 - (ix * iy) / area if area > 0 else 0.0)
    return float(sum(losses) / len(losses))


def plan_crop(
    track: SubjectTrack,
    *,
    src_aspect: float,
    dst_aspect: float,
    eye_line: float = 0.38,
    threshold: float = 0.25,
    strategy: str = "subject_aware",
) -> CropPlan:
    """The crop window for `dst_aspect` following the smoothed subject (or centred)."""
    width, height = _window(src_aspect, dst_aspect)
    centred = ((0.0, (1.0 - width) / 2, (1.0 - height) / 2),)
    if strategy != "subject_aware" or not track.found:
        return CropPlan(width, height, centred, 0.0, "crop")
    smoothed = smooth_track(track)
    path = []
    for t, cx, cy, _w, h in smoothed.samples:
        eyes = cy - h * 0.15  # eyes sit above the box centre
        x0 = min(max(cx - width / 2, 0.0), 1.0 - width)
        y0 = min(max(eyes - eye_line * height, 0.0), 1.0 - height)
        path.append((round(t, 3), round(x0, 4), round(y0, 4)))
    loss = crop_loss(smoothed, width, height, path)
    return CropPlan(width, height, tuple(path), round(loss, 4), "blurred_fill" if loss > threshold else "crop")


def piecewise_expression(points: Sequence[tuple[float, float]], *, default: float) -> str:
    """An FFmpeg expression of `t` interpolating (t, v) points linearly (held at the ends)."""
    if not points:
        return f"{default:.3f}"
    if len(points) == 1:
        return f"{points[0][1]:.3f}"
    expr = f"{points[-1][1]:.3f}"
    for (t0, v0), (t1, v1) in reversed(list(itertools.pairwise(points))):
        if t1 <= t0:
            continue
        slope = (v1 - v0) / (t1 - t0)
        expr = f"if(lt(t\\,{t1:.3f})\\,{v0:.3f}+{slope:.4f}*(t-{t0:.3f})\\,{expr})"
    return f"if(lt(t\\,{points[0][0]:.3f})\\,{points[0][1]:.3f}\\,{expr})"
