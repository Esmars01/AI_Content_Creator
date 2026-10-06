"""CPU stand-ins for the QC metric models (manifest `test_backend`). Never used by a worker; their
numbers are synthetic and only exercise the adapters' bookkeeping (face tracks, crops, sampling)."""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image

__all__ = ["FakeSyncNet", "FakeUTMOS", "FakeUVQ", "fake_syncnet", "fake_utmos", "fake_uvq"]


class FakeSyncNet:
    def __init__(self, faceless: bool = False) -> None:
        self.faceless = faceless
        self.crops: np.ndarray | None = None

    def face_boxes(self, frame_paths: list[str]) -> list[tuple[float, float, float, float] | None]:
        out: list[tuple[float, float, float, float] | None] = []
        for path in frame_paths:
            with Image.open(path) as image:
                w, h = image.size
            out.append(None if self.faceless else (w * 0.3, h * 0.2, w * 0.7, h * 0.45))
        return out

    def evaluate(self, crops_rgb: np.ndarray, audio16k: np.ndarray, vshift: int) -> tuple[int, float, float]:
        self.crops = crops_rgb
        return 0, 6.5, 7.0


class FakeUVQ:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool, int]] = []

    def score(self, video: str, length_s: int, transpose: bool, fps: int) -> dict[str, Any]:
        self.calls.append((length_s, transpose, fps))
        frames = [3.5 + 0.1 * (i % 3) for i in range(length_s * fps)]
        return {
            "uvq1p5_score": float(np.mean(frames)),
            "per_frame_scores": frames,
            "frame_indices": list(range(len(frames))),
        }


class FakeUTMOS:
    def predict(self, samples16k: np.ndarray) -> float:
        return 3.9


def fake_syncnet(manifest: Any) -> FakeSyncNet:
    return FakeSyncNet()


def fake_uvq(manifest: Any) -> FakeUVQ:
    return FakeUVQ()


def fake_utmos(manifest: Any) -> FakeUTMOS:
    return FakeUTMOS()
