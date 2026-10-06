"""CPU stand-in for SeedVR2 (manifest `test_backend`). Never used by a worker: frames resized by
area and cropped to multiples of 16 (the model's output geometry), lightly sharpened."""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image, ImageFilter

__all__ = ["FakeSeedVR2Backend", "fake_backend"]


class FakeSeedVR2Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float, int]] = []

    def restore(self, frames: list[np.ndarray], area: float, seed: int) -> list[np.ndarray]:
        self.calls.append((len(frames), area, seed))
        h, w = frames[0].shape[:2]
        scale = area / (h * w) ** 0.5
        nw, nh = max(16, int(w * scale) // 16 * 16), max(16, int(h * scale) // 16 * 16)
        out = []
        for frame in frames:
            image = Image.fromarray(frame).resize((nw, nh), Image.Resampling.BICUBIC).filter(ImageFilter.SHARPEN)
            out.append(np.asarray(image))
        return out


def fake_backend(manifest: Any) -> FakeSeedVR2Backend:
    return FakeSeedVR2Backend()
