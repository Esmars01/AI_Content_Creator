"""CPU stand-in for VoxCPM2's forward passes (manifest `test_backend`). Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from ce_plugin_kit.testing import fake_speech_samples

from ce_plugin_voice_voxcpm2.backend import compose_text

__all__ = ["FakeVoxCPM2Backend", "fake_backend"]


class FakeVoxCPM2Backend:
    """Records the composed prompt text the real backend would send (the instruction syntax is the
    part worth testing) and returns speech-shaped tone bursts at 48 kHz."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.prompts: list[str] = []
        self.designs: list[tuple[str, str, int]] = []

    def synthesize(self, text: str, options: dict[str, Any]) -> np.ndarray:
        self.calls.append((text, dict(options)))
        phrases = (
            [options.get("instruction")]
            if options.get("reference")
            else [options.get("description"), options.get("instruction")]
        )
        self.prompts.append(compose_text(text, *phrases))
        return fake_speech_samples(len(text.split()), 48_000, seed=int(options["seed"]))

    def prepare(self, reference_wav: Path, transcript: str, workdir: Path) -> dict[str, Path]:
        return {}

    def design(self, description: str, sample_text: str, language: str, seed: int) -> np.ndarray:
        self.designs.append((description, language, seed))
        return fake_speech_samples(len(sample_text.split()), 48_000, seed=seed)


def fake_backend(manifest: Any) -> FakeVoxCPM2Backend:
    return FakeVoxCPM2Backend()
