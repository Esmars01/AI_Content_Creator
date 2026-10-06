"""CPU stand-in for MOSS-SoundEffect (manifest `test_backend`). Never used by a worker: seeded
filtered noise of the requested length, mono at 48 kHz, shaped `(channels, samples)` like the pipeline."""

from __future__ import annotations

from typing import Any

import numpy as np
from ce_plugin_kit.testing import seeded

__all__ = ["FakeMossSfxBackend", "fake_backend"]


class FakeMossSfxBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, options: dict[str, Any]) -> np.ndarray:
        self.calls.append(dict(options))
        n = int(48_000 * float(options["seconds"]))
        noise = seeded("sfx", options["seed"], options["prompt"]).standard_normal(n).astype(np.float32)
        smooth = np.convolve(noise, np.ones(32, dtype=np.float32) / 32, mode="same") * 0.5
        return smooth[None, :]


def fake_backend(manifest: Any) -> FakeMossSfxBackend:
    return FakeMossSfxBackend()
