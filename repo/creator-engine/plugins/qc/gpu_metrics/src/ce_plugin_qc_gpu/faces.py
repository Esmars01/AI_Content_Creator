"""Face tracks for SyncNet crops: MuseTalk-style landmark boxes come from the backend; this module
smooths them and cuts SyncNet's 224×224 crops exactly as `run_pipeline.py::crop_video` (commit
907c0b5): median-filtered box size and center (kernel 13), `crop_scale` 0.4, padding value 110."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from PIL import Image

__all__ = ["crop_face", "fill_gaps", "median_filter"]

Box = tuple[float, float, float, float]


def median_filter(values: Sequence[float], kernel: int) -> np.ndarray:
    """`scipy.signal.medfilt` (zero-padded edges, odd kernel) without SciPy."""
    k = max(1, kernel | 1)
    data = np.asarray(values, dtype=np.float64)
    padded = np.pad(data, k // 2, mode="constant")
    return np.array([np.median(padded[i : i + k]) for i in range(len(data))])


def fill_gaps(boxes: Sequence[Box | None]) -> list[Box] | None:
    """Frames without a face take the nearest detected box; None when no frame has one."""
    known = [i for i, b in enumerate(boxes) if b is not None]
    if not known:
        return None
    out: list[Box] = []
    for i in range(len(boxes)):
        nearest = min(known, key=lambda k: abs(k - i))
        out.append(boxes[nearest])  # type: ignore[arg-type]
    return out


def crop_face(frame: np.ndarray, center_x: float, center_y: float, size: float, crop_scale: float = 0.4) -> np.ndarray:
    """One 224×224 SyncNet crop: the box half-size `size`, extended downward and sideways by
    `crop_scale`, from the frame padded with gray 110."""
    bsi = int(size * (1 + 2 * crop_scale))
    padded = np.pad(frame, ((bsi, bsi), (bsi, bsi), (0, 0)), mode="constant", constant_values=110)
    my, mx = center_y + bsi, center_x + bsi
    face = padded[
        int(my - size) : int(my + size * (1 + 2 * crop_scale)),
        int(mx - size * (1 + crop_scale)) : int(mx + size * (1 + crop_scale)),
    ]
    return np.asarray(Image.fromarray(face).resize((224, 224), Image.Resampling.BILINEAR))
