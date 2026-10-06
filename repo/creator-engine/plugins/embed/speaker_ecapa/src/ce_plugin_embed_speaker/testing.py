"""CPU stand-in for ECAPA (manifest `test_backend`). Never used by a worker: a 192-dim vector of
coarse spectral band energies, so the same voice-like tone embeds close to itself and far from a
different one."""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["FakeEcapaBackend", "fake_backend"]


class FakeEcapaBackend:
    dim = 192

    def embed(self, samples16k: np.ndarray) -> np.ndarray:
        spectrum = np.abs(np.fft.rfft(samples16k[: 16_000 * 4]))
        bands = np.array_split(spectrum[: len(spectrum) // 2], self.dim)
        return np.log1p(np.array([b.mean() for b in bands]))


def fake_backend(manifest: Any) -> FakeEcapaBackend:
    return FakeEcapaBackend()
