"""Persistent research (Phase 12): facts from extracted text, deterministic fact ids, and hybrid
retrieval over stored facts (BM25 + embedding cosine) for the Director's fact check.

A **fact** is a chunk of a source's text with its character span (`quote_span`), so every piece of
evidence the fact check cites can be shown with its exact place in the source (traceability).
Fact ids are UUIDv5 of `(source id, chunk index)`: re-ingesting the same source yields the same ids,
and evidence ids stay valid across plans.

Embeddings are an index, never the record (§18 applies the same rule to memory): a fact without an
embedding, or embedded by another model than the query, is still found by keyword.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from ce_research.documents import Chunk, Source, chunk_source
from ce_research.retrieval import KeywordIndex

__all__ = ["FactDraft", "HybridIndex", "StoredFact", "cosine", "fact_id", "facts_from_text"]

_FACT_NAMESPACE = uuid.UUID("b7c0f1e4-2d3a-4c5b-8e6f-9a0b1c2d3e4f")


def fact_id(source_id: UUID, index: int) -> UUID:
    return uuid.uuid5(_FACT_NAMESPACE, f"{source_id}#c{index}")


@dataclass(frozen=True)
class FactDraft:
    id: UUID
    index: int
    text: str
    start: int
    end: int


def facts_from_text(source_id: UUID, text: str, *, words: int, overlap: int) -> list[FactDraft]:
    """Word windows over the extracted text (same rules as in-memory research)."""
    probe = Source(id=source_id, kind="pasted", text=text)
    return [
        FactDraft(fact_id(source_id, c.index), c.index, c.text, c.start, c.end)
        for c in chunk_source(probe, words=words, overlap=overlap)
    ]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class StoredFact:
    id: UUID
    source_id: UUID
    index: int
    text: str
    start: int
    end: int
    trust: str = "user_provided"
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    source_title: str = ""
    source_uri: str | None = None
    ref: str = ""  # the evidence id when it is not the fact id (in-memory chunks: "<source>#c<i>")

    @property
    def evidence_id(self) -> str:
        return self.ref or str(self.id)


@dataclass
class HybridIndex:
    """BM25 over every fact plus cosine similarity for facts embedded by the query's model.

    score = bm25 / max_bm25 + `vector_weight` · cosine (each term in 0..1). Deterministic: ties
    break by source id and fact index."""

    facts: list[StoredFact]
    vector_weight: float = 1.0
    _keyword: KeywordIndex | None = field(default=None, init=False)

    def _chunks(self) -> list[Chunk]:
        return [Chunk(f.source_id, f.index, f.text, f.start, f.end) for f in self.facts]

    @property
    def keyword(self) -> KeywordIndex:
        if self._keyword is None:
            self._keyword = KeywordIndex(self._chunks())
        return self._keyword

    def search(
        self,
        query: str,
        k: int,
        *,
        query_vector: Sequence[float] | None = None,
        model: str | None = None,
        min_cosine: float = 0.0,
    ) -> list[tuple[StoredFact, float]]:
        by_key = {(f.source_id, f.index): f for f in self.facts}
        scores: dict[tuple[UUID, int], float] = {}
        hits = self.keyword.search(query, max(k * 4, k))
        top = max((h.score for h in hits), default=0.0)
        for hit in hits:
            key = (hit.chunk.source_id, hit.chunk.index)
            scores[key] = scores.get(key, 0.0) + (hit.score / top if top else 0.0)
        if query_vector is not None and model:
            for fact in self.facts:
                if fact.embedding is None or fact.embedding_model != model:
                    continue
                sim = cosine(query_vector, fact.embedding)
                if sim > min_cosine:
                    key = (fact.source_id, fact.index)
                    scores[key] = scores.get(key, 0.0) + self.vector_weight * sim
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0][0]), kv[0][1]))
        return [(by_key[key], round(score, 6)) for key, score in ranked[:k] if key in by_key]
