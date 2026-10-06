"""Frame-rate conversion plans (§26 `video.interpolate`): for every output frame, the source pair
and the arbitrary timestep RIFE 4.x interpolates at (`model.inference(I0, I1, timestep)`), or a
plain copy when the output instant falls on a source frame or across a scene cut."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

__all__ = ["Step", "is_cut", "plan_steps", "thumbnail"]

EPS = 1e-3


@dataclass(frozen=True)
class Step:
    a: int  # source frame before the output instant
    b: int  # source frame after it (== a for a copy)
    t: float  # position between a and b in [0, 1)

    @property
    def copy(self) -> bool:
        return self.a == self.b


def plan_steps(source_frames: int, source_fps: float, target_fps: float) -> list[Step]:
    """Output frame k sits at k / target_fps; its source position is that time × source_fps."""
    if source_frames <= 0 or source_fps <= 0 or target_fps <= 0:
        raise ValueError("frame counts and rates must be positive")
    duration = source_frames / source_fps
    count = max(1, round(duration * target_fps))
    steps: list[Step] = []
    for k in range(count):
        position = min(k * source_fps / target_fps, source_frames - 1)
        a = math.floor(position + EPS)
        t = position - a
        if t < EPS or a >= source_frames - 1:
            steps.append(Step(min(a, source_frames - 1), min(a, source_frames - 1), 0.0))
        elif t > 1 - EPS:
            steps.append(Step(a + 1, a + 1, 0.0))
        else:
            steps.append(Step(a, a + 1, round(t, 6)))
    return steps


def thumbnail(frame: np.ndarray, size: int = 64) -> np.ndarray:
    return np.asarray(
        Image.fromarray(frame).convert("L").resize((size, size), Image.Resampling.BILINEAR), dtype=np.float32
    )


def is_cut(a: np.ndarray, b: np.ndarray, threshold: float) -> bool:
    """A scene cut between two frames: the mean absolute difference of their grayscale thumbnails
    (0–255) above `threshold`. Interpolating across a cut produces ghosted blends, so the nearer
    source frame is repeated instead (upstream RIFE does the same below an SSIM threshold)."""
    return float(np.mean(np.abs(thumbnail(a) - thumbnail(b)))) > threshold
