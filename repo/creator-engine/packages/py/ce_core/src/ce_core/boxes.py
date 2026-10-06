"""Normalized boxes `(x, y, w, h)` in 0..1 of a frame: merging and zoom windows (§27 screen
recordings). Pure geometry, shared by media analysis (`ce_render.screen`) and planning
(`ce_director.screen`)."""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["area", "fit_window", "intersection", "merge_boxes"]

Box = list[float]


def area(box: Sequence[float]) -> float:
    return max(0.0, float(box[2])) * max(0.0, float(box[3]))


def intersection(a: Sequence[float], b: Sequence[float]) -> float:
    """The area two boxes share."""
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def merge_boxes(boxes: Sequence[Sequence[float]], gap: float = 0.03) -> list[Box]:
    """Unions of boxes closer than `gap`, largest first (repeated until no two touch)."""
    merged: list[Box] = []
    for start in sorted((list(map(float, b)) for b in boxes), key=lambda b: (b[1], b[0])):
        box = start
        while True:
            x, y, w, h = box
            hit = next(
                (
                    o
                    for o in merged
                    if x <= o[0] + o[2] + gap and o[0] <= x + w + gap and y <= o[1] + o[3] + gap and o[1] <= y + h + gap
                ),
                None,
            )
            if hit is None:
                merged.append(box)
                break
            merged.remove(hit)
            nx, ny = min(x, hit[0]), min(y, hit[1])
            box = [nx, ny, max(x + w, hit[0] + hit[2]) - nx, max(y + h, hit[1] + hit[3]) - ny]
    return sorted(([round(v, 4) for v in b] for b in merged), key=lambda b: -area(b))


def fit_window(
    region: Sequence[float], aspect: float = 1.0, *, pad: float = 0.15, min_size: float = 0.4
) -> tuple[float, float, float, float]:
    """A zoom window around `region`: padded, at least `min_size` of the frame, shaped like the
    frame times `aspect` (1.0 keeps the frame's shape), clamped inside the frame."""
    x, y, w, h = (float(v) for v in region)
    cx, cy = x + w / 2, y + h / 2
    w, h = max(w * (1 + 2 * pad), min_size), max(h * (1 + 2 * pad), min_size)
    if w / h > aspect:
        h = w / aspect
    else:
        w = h * aspect
    if w > 1.0 or h > 1.0:
        s = max(w, h)
        w, h = w / s, h / s
    x0, y0 = min(max(cx - w / 2, 0.0), 1.0 - w), min(max(cy - h / 2, 0.0), 1.0 - h)
    return (round(x0, 4), round(y0, 4), round(w, 4), round(h, 4))
