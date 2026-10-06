"""CPU stand-in for MuseTalk's face finding and UNet (manifest `test_backend`). Never used by a worker.

Faces are a fixed box in the upper middle of the frame (where `fake_image` draws its disc); the
"generated" mouth crop is the input crop darkened in its lower half in proportion to the audio
frame's loudness, so tests can see that the patch follows the audio and stays in the lower face."""

from __future__ import annotations

from typing import Any

import numpy as np
from ce_plugin_kit.media import read_audio
from PIL import Image

from ce_plugin_lipsync_musetalk.patch import Face

__all__ = ["FakeMuseTalkBackend", "fake_backend"]


class FakeMuseTalkBackend:
    def __init__(self, *, faceless: set[int] | None = None, two_faces: bool = False) -> None:
        self.faceless = faceless or set()
        self.two_faces = two_faces
        self.generate_calls: list[tuple[int, list[int], int]] = []

    def detect_faces(self, frame_paths: list[str]) -> list[list[Face]]:
        out: list[list[Face]] = []
        for i, path in enumerate(frame_paths):
            with Image.open(path) as image:
                width, height = image.size
            if i in self.faceless:
                out.append([])
                continue
            main = Face((int(width * 0.3), int(height * 0.2), int(width * 0.7), int(height * 0.5)))
            faces = [main]
            if self.two_faces:  # a smaller face on the left
                faces.append(Face((int(width * 0.02), int(height * 0.6), int(width * 0.22), int(height * 0.75))))
            out.append(faces)
        return out

    def audio_features(self, wav16k: str, fps: float, frames: int) -> np.ndarray:
        samples = read_audio(wav16k, 16_000)
        hop = max(1, int(16_000 / fps))
        rms = [float(np.sqrt(np.mean(samples[i * hop : (i + 1) * hop] ** 2) + 1e-12)) for i in range(frames)]
        return np.asarray(rms, dtype=np.float32)

    def generate(self, crops: list[np.ndarray], features: Any, indices: list[int], seed: int) -> list[np.ndarray]:
        self.generate_calls.append((len(crops), list(indices), seed))
        out = []
        for crop, index in zip(crops, indices, strict=True):
            mouth = np.asarray(crop, dtype=np.float32).copy()
            level = min(1.0, float(features[index]) * 4.0)
            mouth[mouth.shape[0] // 2 :] *= 1.0 - 0.6 * level
            out.append(mouth.clip(0, 255).astype(np.uint8))
        return out


def fake_backend(manifest: Any) -> FakeMuseTalkBackend:
    return FakeMuseTalkBackend()
