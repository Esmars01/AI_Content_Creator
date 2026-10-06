"""CPU stand-in for the CTC model (manifest `test_backend`). Never used by a worker.

It builds emissions in which each character of the given words is spoken in turn (evenly over
the audio, with blanks and a separator between words), so the Viterbi alignment and the word
mapping run for real on a known answer."""

from __future__ import annotations

from typing import Any

import numpy as np
from ce_plugin_kit.media import probe

__all__ = ["FakeCTCBackend", "fake_backend"]

FRAME_S = 0.02


class FakeCTCBackend:
    blank = 0
    separator = 1

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = script  # the words actually "spoken" (None: whatever encode_words saw first)
        self.vocab: dict[str, int] = {}
        self._encoded: list[list[int]] | None = None

    def _id(self, ch: str) -> int:
        if not ch.isalnum():
            return -1
        return self.vocab.setdefault(ch, len(self.vocab) + 2)

    def encode_words(self, words: list[str], language: str) -> list[list[int]]:
        encoded = [[i for i in (self._id(c) for c in w) if i >= 0] for w in words]
        if self._encoded is None:
            self._encoded = encoded
        return encoded

    def emissions(self, wav16k: str) -> tuple[np.ndarray, float]:
        duration = probe(wav16k).duration_s
        frames = int(duration / FRAME_S)
        spoken = [[self._id(c) for c in w if self._id(c) >= 0] for w in (self.script or [])]
        vocab = max(64, len(self.vocab) + 8)
        lp = np.full((frames, vocab), -8.0)
        lp[:, self.blank] = -0.05
        sequence: list[int] = []
        for i, word in enumerate(spoken):
            if i:
                sequence.append(self.separator)
            sequence += word
        if sequence:
            per = frames / (len(sequence) + 1)
            for k, token in enumerate(sequence):
                t = int((k + 0.5) * per)
                lp[t : t + max(1, int(per / 2)), token] = -0.02
                lp[t : t + max(1, int(per / 2)), self.blank] = -6.0
        return lp, FRAME_S


def fake_backend(manifest: Any) -> FakeCTCBackend:
    return FakeCTCBackend(script=["merhaba", "dünya"])
