"""CPU stand-in for RIFE (manifest `test_backend`). Never used by a worker: a linear blend at the
timestep (what RIFE replaces with flow-based warping)."""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["FakeRifeBackend", "fake_backend"]


class FakeRifeBackend:
    def __init__(self) -> None:
        self.calls: list[list[float]] = []

    def interpolate(self, a: np.ndarray, b: np.ndarray, times: list[float]) -> list[np.ndarray]:
        self.calls.append(list(times))
        fa, fb = a.astype(np.float32), b.astype(np.float32)
        return [np.clip(fa * (1 - t) + fb * t, 0, 255).astype(np.uint8) for t in times]


def fake_backend(manifest: Any) -> FakeRifeBackend:
    return FakeRifeBackend()
