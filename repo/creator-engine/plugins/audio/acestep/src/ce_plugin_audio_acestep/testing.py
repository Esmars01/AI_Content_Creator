"""CPU stand-in for ACE-Step (manifest `test_backend`). Never used by a worker: a seeded chord
pad of the requested length at 48 kHz stereo, written as WAV like the real engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from ce_plugin_kit.media import write_wav
from ce_plugin_kit.testing import seeded

__all__ = ["FakeAceStepBackend", "fake_backend"]


class FakeAceStepBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, options: dict[str, Any], workdir: str) -> str:
        self.calls.append(dict(options))
        rate, seconds = 48_000, float(options["duration_s"])
        t = np.arange(int(rate * seconds)) / rate
        rng = seeded("music", options["seed"])
        root = 110.0 * (1 + rng.integers(0, 4) / 12)
        pad = sum(0.12 * np.sin(2 * np.pi * root * ratio * t) for ratio in (1.0, 1.25, 1.5))
        stereo = np.stack([pad, pad * 0.9], axis=1).astype(np.float32)
        return str(write_wav(Path(workdir) / "acestep_raw.wav", stereo, rate))


def fake_backend(manifest: Any) -> FakeAceStepBackend:
    return FakeAceStepBackend()
