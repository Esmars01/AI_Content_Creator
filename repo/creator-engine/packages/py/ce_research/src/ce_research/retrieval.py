"""Keyword retrieval over chunks (BM25). Embedding retrieval over pgvector arrives with persistent
ingestion in Phase 12; this keeps Phase 4 research deterministic and dependency-free."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import regex

from ce_research.documents import Chunk

__all__ = ["KeywordIndex", "ScoredChunk", "terms"]

_TERM = regex.compile(r"[\p{L}\p{N}]+(?:['’][\p{L}]+)?")
_STOP_TEXT = (
    "a an and are as at be but by for from has have i in is it its of on or that the this to was were "
    "will with you your we our they their he she his her them not no do does did so if than then"
)
_STOP = frozenset(_STOP_TEXT.split())


def terms(text: str) -> list[str]:
    """Casefolded word terms without stopwords; a trailing plural `s` is dropped (cheap stemming)."""
    out = []
    for match in _TERM.finditer(text.casefold()):
        word = match.group(0).replace("’", "'")
        if word in _STOP or len(word) < 2:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float


class KeywordIndex:
    def __init__(self, chunks: Sequence[Chunk], *, k1: float = 1.4, b: float = 0.75) -> None:
        self.chunks = list(chunks)
        self.k1, self.b = k1, b
        self._tf = [Counter(terms(c.text)) for c in self.chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        wanted = Counter(terms(query))
        scored: list[ScoredChunk] = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            for term in wanted:
                f = tf.get(term, 0)
                if not f:
                    continue
                norm = 1 - self.b + self.b * (self._len[i] / self._avg if self._avg else 1.0)
                score += self._idf.get(term, 0.0) * f * (self.k1 + 1) / (f + self.k1 * norm)
            if score > 0:
                scored.append(ScoredChunk(self.chunks[i], score))
        scored.sort(key=lambda s: (-s.score, str(s.chunk.source_id), s.chunk.index))
        return scored[:k]
