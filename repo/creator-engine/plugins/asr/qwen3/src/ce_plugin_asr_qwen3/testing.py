"""CPU stand-ins for Qwen3-ASR and the ForcedAligner (manifest `test_backend`). Never used by a worker.

The "ASR" returns a scripted transcript (`heard`, default: silence → ""), the "aligner" spreads
the given text's words evenly over the audio, so the adapters' mapping code runs for real."""

from __future__ import annotations

from typing import Any

from ce_plugin_kit.media import probe

__all__ = ["FakeQwen3ASRBackend", "fake_aligner_backend", "fake_asr_backend"]


class FakeQwen3ASRBackend:
    def __init__(self, heard: str = "", language: str = "English") -> None:
        self.heard = heard
        self.language = language
        self.calls: list[tuple[str, Any]] = []

    def transcribe(self, wav16k: str, language: str | None) -> tuple[str, str]:
        self.calls.append(("transcribe", language))
        return (language or self.language), self.heard

    def align(self, wav16k: str, text: str, language: str) -> list[tuple[str, float, float]]:
        self.calls.append(("align", (text, language)))
        words = text.split()
        if not words:
            return []
        duration = probe(wav16k).duration_s
        step = duration / len(words)
        return [(w, round(i * step + 0.02, 3), round((i + 1) * step - 0.02, 3)) for i, w in enumerate(words)]


def fake_asr_backend(manifest: Any) -> FakeQwen3ASRBackend:
    return FakeQwen3ASRBackend()


def fake_aligner_backend(manifest: Any) -> FakeQwen3ASRBackend:
    return FakeQwen3ASRBackend()
