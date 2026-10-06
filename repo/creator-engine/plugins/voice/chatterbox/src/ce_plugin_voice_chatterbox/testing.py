"""CPU stand-in for Chatterbox's forward pass (manifest `test_backend`): speech-shaped tone bursts
at 24 kHz, one per word, and a placeholder `conds.pt`. Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from ce_plugin_kit.testing import fake_speech_samples

__all__ = ["FakeChatterboxBackend", "fake_backend"]


class FakeChatterboxBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.prepared: list[tuple[Path, str]] = []

    def synthesize(self, text: str, options: dict[str, Any]) -> np.ndarray:
        self.calls.append((text, dict(options)))
        return fake_speech_samples(len(text.split()), 24_000, seed=int(options["seed"]))

    def prepare(self, reference_wav: Path, transcript: str, workdir: Path) -> dict[str, Path]:
        self.prepared.append((reference_wav, transcript))
        out = workdir / "conds.pt"
        out.write_bytes(b"fake-conditionals:" + reference_wav.read_bytes()[:64])
        return {"conds.pt": out}


def fake_backend(manifest: Any) -> FakeChatterboxBackend:
    return FakeChatterboxBackend()
