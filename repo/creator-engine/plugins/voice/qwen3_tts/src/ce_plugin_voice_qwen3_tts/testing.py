"""CPU stand-in for Qwen3-TTS's forward passes (manifest `test_backend`). Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from ce_plugin_kit.testing import fake_speech_samples

__all__ = ["FakeQwen3TTSBackend", "fake_backend"]


class FakeQwen3TTSBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.designs: list[tuple[str, str, int]] = []

    def synthesize(self, text: str, options: dict[str, Any]) -> np.ndarray:
        self.calls.append((text, dict(options)))
        return fake_speech_samples(len(text.split()), 24_000, seed=int(options["seed"]))

    def prepare(self, reference_wav: Path, transcript: str, workdir: Path) -> dict[str, Path]:
        out = workdir / "prompt.pt"
        out.write_bytes(b"fake-prompt:" + transcript.encode())
        return {"prompt.pt": out}

    def design(self, description: str, sample_text: str, language: str, seed: int) -> np.ndarray:
        self.designs.append((description, language, seed))
        return fake_speech_samples(len(sample_text.split()), 24_000, seed=seed)


def fake_backend(manifest: Any) -> FakeQwen3TTSBackend:
    return FakeQwen3TTSBackend()
