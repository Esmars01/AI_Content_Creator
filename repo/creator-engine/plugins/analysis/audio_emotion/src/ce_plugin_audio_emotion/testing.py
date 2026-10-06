"""CPU stand-in for emotion2vec+ (manifest `test_backend`). Never used by a worker: louder, brighter
windows lean `happy`/`angry`, quiet ones `sad`/`neutral` — enough to exercise the windowing and the
averaging, not a classifier."""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["FakeEmotionBackend", "fake_backend"]


class FakeEmotionBackend:
    def classify(self, samples16k: np.ndarray) -> dict[str, float]:
        rms = float(np.sqrt(np.mean(samples16k.astype(np.float64) ** 2))) if samples16k.size else 0.0
        loud = min(1.0, rms * 8.0)
        return {"happy": 0.5 * loud, "angry": 0.2 * loud, "neutral": 0.6 * (1 - loud), "sad": 0.2 * (1 - loud)}


def fake_backend(manifest: Any) -> FakeEmotionBackend:
    return FakeEmotionBackend()
