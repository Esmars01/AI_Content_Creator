"""`MemoryRetriever.retrieve(records, context) -> SnapshotDraft` (§18.5).

1. Structured filters: status `active`, not deleted, inside the creator-version scope
   (`creator_version_from` ≤ current ≤ `creator_version_to`, by version number).
2. Conflicts: several values for one (kind, key) resolve as configured
   (`pinned > authored > confidence > recency`); the losers stay out and the conflict is listed.
3. Ranking per category: pinned first, then `w·confidence + (1 − w)·recency` (half-life per
   category) plus a keyword bonus for text-like kinds that share words with the brief.
4. Budgets per category; pinned items always enter.

The draft is copied into an immutable snapshot (I7). Since Phase 12, when the brief was embedded
(`brief_vector`), text-like items embedded by the **same** model rank by `retrieval_weight ·
cosine(brief, item)` instead of the keyword bonus; items without a comparable vector keep the
keyword bonus. The snapshot's params record the retriever and the embedding model, so the
snapshot stays reproducible from its own content.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ce_config.schemas import RetrievalConfig
from ce_core.canonical import content_digest
from ce_core.identity.memory import SnapshotItem

from ce_memory.records import MemoryRecord
from ce_memory.text import cosine, keywords

__all__ = ["TEXT_KINDS", "MemoryRetriever", "RetrievalContext", "SnapshotDraft"]

TEXT_KINDS = frozenset(
    {
        "speech_habit.recurring_phrase",
        "speech_habit.transition_phrase",
        "persona_fact",
        "stance",
        "avoidance.phrase",
        "avoidance.topic",
        "rejected_pattern",
        "preference",
    }
)
KEYWORD_BONUS = 0.5
RETRIEVER = "structured+keyword/v1"
RETRIEVER_EMBEDDING = "structured+embedding/v1"


@dataclass(frozen=True)
class RetrievalContext:
    creator_version_id: UUID
    now: datetime
    brief: str = ""
    version_numbers: Mapping[UUID, int] = field(default_factory=dict)
    categories: frozenset[str] | None = None
    brief_vector: tuple[float, ...] | None = None  # the brief embedded by `embedding_model` (Phase 12)
    embedding_model: str | None = None
    embedding_weight: float = 0.5


@dataclass
class SnapshotDraft:
    creator_version_id: UUID
    items: list[SnapshotItem]
    params: dict[str, Any]
    conflicts: list[dict[str, Any]]

    @property
    def item_ids(self) -> list[UUID]:
        return [i.item_id for i in self.items]

    def content(self) -> dict[str, Any]:
        return {
            "creator_version_id": str(self.creator_version_id),
            "items": [i.model_dump(mode="json") for i in self.items],
            "retrieval_params": self.params,
            "conflicts": self.conflicts,
        }

    def digest(self) -> str:
        return content_digest(self.content())


def _in_scope(record: MemoryRecord, ctx: RetrievalContext) -> bool:
    current = ctx.version_numbers.get(ctx.creator_version_id)
    if current is None:
        return record.creator_version_from is None and record.creator_version_to is None
    low = ctx.version_numbers.get(record.creator_version_from) if record.creator_version_from else None
    high = ctx.version_numbers.get(record.creator_version_to) if record.creator_version_to else None
    if record.creator_version_from is not None and low is None:
        return False  # scoped to a version this context does not know
    if record.creator_version_to is not None and high is None:
        return False
    return (low is None or low <= current) and (high is None or current <= high)


def _text_of(record: MemoryRecord) -> str:
    parts = [record.text] + [str(v) for v in record.value.values() if isinstance(v, str)]
    return " ".join(p for p in parts if p)


class MemoryRetriever:
    def __init__(self, config: RetrievalConfig) -> None:
        self.config = config

    def _precedence(self, record: MemoryRecord) -> tuple[Any, ...]:
        keys: list[Any] = []
        for rule in self.config.conflict_precedence:
            if rule == "pinned":
                keys.append(not record.pinned)
            elif rule == "authored":
                keys.append(record.source_type != "authored")
            elif rule == "confidence":
                keys.append(-record.confidence)
            elif rule == "recency":
                keys.append(-record.last_seen_at.timestamp())
        return (*keys, str(record.id))

    def _recency(self, record: MemoryRecord, ctx: RetrievalContext) -> float:
        half_life = self.config.recency_half_life_days.get(record.category, 180.0)
        age_days = max(0.0, (ctx.now - record.last_seen_at).total_seconds() / 86_400)
        return float(0.5 ** (age_days / half_life))

    def retrieve(self, records: Iterable[MemoryRecord], ctx: RetrievalContext) -> SnapshotDraft:
        live = [
            r
            for r in records
            if r.status == "active"
            and not r.deleted
            and _in_scope(r, ctx)
            and (ctx.categories is None or r.category in ctx.categories)
        ]
        by_key: dict[tuple[str, str], list[MemoryRecord]] = defaultdict(list)
        for record in live:
            by_key[(record.kind, record.key)].append(record)
        chosen: list[MemoryRecord] = []
        conflicts: list[dict[str, Any]] = []
        for (kind, key), group in sorted(by_key.items()):
            distinct = {r.value_hash or str(r.value) for r in group}
            ordered = sorted(group, key=self._precedence)
            if len(distinct) <= 1:
                chosen.append(ordered[0])
                continue
            winner, others = ordered[0], ordered[1:]
            chosen.append(winner)
            unresolved = any(r.conflict_state == "unresolved" for r in group)
            conflicts.append(
                {
                    "kind": kind,
                    "key": key,
                    "winner": str(winner.id),
                    "others": sorted(str(r.id) for r in others),
                    "state": "unresolved" if unresolved else "resolved",
                }
            )
        brief_words = keywords(ctx.brief)
        weight = self.config.confidence_weight
        semantic = ctx.brief_vector is not None and bool(ctx.embedding_model)

        def score(record: MemoryRecord) -> float:
            value = weight * record.confidence + (1 - weight) * self._recency(record, ctx)
            if record.kind not in TEXT_KINDS:
                return value
            if semantic and record.embedding is not None and record.embedding_model == ctx.embedding_model:
                return value + ctx.embedding_weight * max(0.0, cosine(ctx.brief_vector, record.embedding))
            if brief_words:
                own = keywords(_text_of(record))
                if own:
                    value += KEYWORD_BONUS * len(own & brief_words) / len(own)
            return value

        by_category: dict[str, list[MemoryRecord]] = defaultdict(list)
        for record in chosen:
            by_category[record.category].append(record)
        selected: list[MemoryRecord] = []
        for category in sorted(by_category):
            ranked = sorted(by_category[category], key=lambda r: (not r.pinned, -score(r), str(r.id)))
            budget = self.config.budgets.get(category, 0)
            pinned = [r for r in ranked if r.pinned]
            rest = [r for r in ranked if not r.pinned]
            selected += pinned + rest[: max(0, budget - len(pinned))]
        items = [
            SnapshotItem(
                item_id=r.id, kind=r.kind, value=dict(r.value), text=r.text, pinned=r.pinned, confidence=r.confidence
            )
            for r in selected
        ]
        params: dict[str, Any] = {
            "retriever": RETRIEVER_EMBEDDING if semantic else RETRIEVER,
            "budgets": dict(sorted(self.config.budgets.items())),
            "brief_keywords": sorted(brief_words)[:32],
            "categories": sorted(ctx.categories) if ctx.categories else None,
        }
        if semantic:
            params["embedding_model"] = ctx.embedding_model
            params["embedding_weight"] = ctx.embedding_weight
        return SnapshotDraft(ctx.creator_version_id, items, params, conflicts)
