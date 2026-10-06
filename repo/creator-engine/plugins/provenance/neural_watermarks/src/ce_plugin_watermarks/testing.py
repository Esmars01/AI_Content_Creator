"""CPU stand-ins for VideoSeal and AudioSeal (manifest `test_backend`). Never used by a worker and
never a provenance layer: their `mode` is `mock_dev`.

The video stand-in passes frames through and remembers the bits it was asked to embed (it tests the
adapter's streaming, encoding and verification bookkeeping, not a watermark); the audio stand-in adds
a quiet tone per set bit plus a pilot tone and detects them in the spectrum, which survives AAC."""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["FakeAudioSealBackend", "FakeVideoSealBackend", "fake_audio_backend", "fake_video_backend"]


class FakeVideoSealBackend:
    mode = "mock_dev"

    def __init__(self) -> None:
        self.frames_embedded = 0
        self.bits: np.ndarray | None = None

    def embed(self, frames: np.ndarray, bits: np.ndarray) -> list[np.ndarray]:
        self.frames_embedded += len(frames)
        self.bits = bits.copy()
        return [np.array(f) for f in frames]

    def decode(self, frames: np.ndarray) -> np.ndarray:
        # A lossy codec destroys any pixel-level stand-in mark: the fake reports what was embedded
        # last (or zeros), which is enough to test verification's bookkeeping.
        return self.bits.copy() if self.bits is not None else np.zeros(96, dtype=np.uint8)


class FakeAudioSealBackend:
    mode = "mock_dev"
    base = 1000.0
    step = 150.0

    def watermark_signal(self, mono16k: np.ndarray, bits: np.ndarray) -> np.ndarray:
        t = np.arange(len(mono16k)) / 16_000
        tones = [np.sin(2 * np.pi * (self.base + i * self.step) * t) for i, b in enumerate(bits) if b]
        tones.append(np.sin(2 * np.pi * 800.0 * t))  # the presence pilot
        return (0.004 * np.sum(tones, axis=0)).astype(np.float32)

    def detect(self, mono16k: np.ndarray) -> tuple[float, np.ndarray]:
        spectrum = np.abs(np.fft.rfft(mono16k))
        freqs = np.fft.rfftfreq(len(mono16k), 1 / 16_000)

        def level(f: float) -> float:
            band = (freqs > f - 20) & (freqs < f + 20)
            return float(spectrum[band].max()) if band.any() else 0.0

        noise = float(np.median(spectrum)) + 1e-9
        pilot = level(800.0) / noise
        bits = np.array([1 if level(self.base + i * self.step) / noise > 50 else 0 for i in range(16)], dtype=np.uint8)
        return (1.0 if pilot > 50 else 0.0), bits


def fake_video_backend(manifest: Any) -> FakeVideoSealBackend:
    return FakeVideoSealBackend()


def fake_audio_backend(manifest: Any) -> FakeAudioSealBackend:
    return FakeAudioSealBackend()
