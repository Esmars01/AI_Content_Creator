"""A stand-in tokenizer and encoder for the adapter's tests: a deterministic bag of hashed
character trigrams, so related texts get related vectors. Not a model; never routed."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

__all__ = ["StandInTokenizer", "standin_encoder"]


@dataclass
class _Encoding:
    ids: list[int]


class StandInTokenizer:
    def __init__(self, vocab: int = 4096) -> None:
        self.vocab = vocab

    def encode_batch(self, texts: list[str]) -> list[_Encoding]:
        out = []
        for text in texts:
            padded = f"  {text.lower()}  "
            grams = [padded[i : i + 3] for i in range(len(padded) - 2)]
            out.append(
                _Encoding([1 + int(hashlib.sha256(g.encode()).hexdigest()[:8], 16) % (self.vocab - 1) for g in grams])
            )
        return out


def standin_encoder(dim: int, vocab: int = 4096) -> object:
    table = np.random.default_rng(7).standard_normal((vocab, dim)).astype(np.float32)

    def run(ids: np.ndarray, mask: np.ndarray) -> np.ndarray:
        hidden = table[ids] * mask[..., None]
        cls = hidden.sum(axis=1, keepdims=True)  # position 0 carries the sentence sum
        return np.concatenate([cls, hidden[:, 1:, :]], axis=1)

    return run
