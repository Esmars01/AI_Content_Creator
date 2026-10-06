"""Text similarity before embeddings (Phase 4): normalized words, keywords, character and word
n-grams. Deterministic and language-agnostic (Unicode word characters, NFKC + case folding)."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable

import regex

__all__ = ["cosine", "keywords", "ngram_fingerprints", "ngrams", "normalize", "similarity", "words"]

_WORD = regex.compile(r"[\p{L}\p{M}\p{N}]+(?:['’][\p{L}\p{M}]+)?")
# Function words of the shipped languages that carry no topic (keyword matching only).
_STOPWORD_TEXT = """
a an and are as at be but by do does for from has have he her his how i if in into is it its just like
me my no not of on or our she so than that the their them then there these they this to too was we were
what when where which who why will with you your yours about all also can could would should very really
ve ll re d s t der die das und ist nicht ein eine le la les et est un une el los las es que y bir ve bu
da de için mi
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def words(text: str) -> list[str]:
    return [m.group(0).replace("’", "'") for m in _WORD.finditer(normalize(text))]


def keywords(text: str, *, min_length: int = 3) -> set[str]:
    return {w for w in words(text) if len(w) >= min_length and w not in STOPWORDS}


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def ngram_fingerprints(text: str, n: int = 4) -> list[str]:
    """Stable string fingerprints of the word n-grams (stored in the usage log)."""
    return sorted(" ".join(g) for g in ngrams(words(text), n))


def _jaccard(a: Iterable[object], b: Iterable[object]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _char_trigrams(text: str) -> set[str]:
    flat = " ".join(words(text))
    return {flat[i : i + 3] for i in range(max(0, len(flat) - 2))}


def similarity(a: str, b: str) -> float:
    """Hook-level similarity in 0..1: the larger of keyword overlap and character-trigram overlap
    (catches reorderings and small rewordings)."""
    return round(max(_jaccard(keywords(a), keywords(b)), _jaccard(_char_trigrams(a), _char_trigrams(b))), 4)


def cosine(a: Iterable[float] | None, b: Iterable[float] | None) -> float:
    """Cosine similarity of two vectors (0 when either is missing, empty, zero or the sizes differ)."""
    if a is None or b is None:
        return 0.0
    va, vb = list(a), list(b)
    if not va or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb, strict=True))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    return dot / (na * nb) if na and nb else 0.0
