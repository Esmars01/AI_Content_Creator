"""Creator Memory items and snapshots (§18.2, §18.5).

Memory is structured data first; embeddings are an index, never the record. A plan reads
memory only through an immutable, pinned MemorySnapshot (I7).
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, NonNegativeInt, model_validator

from ce_core.canonical import content_digest
from ce_core.enums import ConflictState, MemorySourceType, MemoryStatus
from ce_core.errors import Issue
from ce_core.keys import CharacterKey
from ce_core.scalars import Digest, NonEmptyStr, Unit
from ce_core.spec.base import SpecModel
from ce_core.vocab import Vocabulary

__all__ = [
    "MemoryItem",
    "MemorySnapshot",
    "MemorySource",
    "SnapshotItem",
    "dedup_key",
    "validate_memory_item",
    "value_hash",
]


class MemorySource(SpecModel):
    type: MemorySourceType
    video_id: UUID | None = None
    version_id: UUID | None = None
    element_key: str | None = None
    edit_proposal_id: UUID | None = None


class MemoryItem(SpecModel):
    """One `creator_memory_items` row as a domain object. `value` is typed by `kind` (see `validate_memory_item`)."""

    id: UUID
    org_id: UUID
    creator_id: UUID
    category: NonEmptyStr
    kind: NonEmptyStr
    key: str = Field(description="normalized dedup key from the kind's key fields")
    value_hash: Digest
    value: dict[str, Any]
    text: str = ""
    vocab_version: NonEmptyStr
    source: MemorySource
    confidence: Unit = 1.0
    evidence_count: NonNegativeInt = 1
    first_seen_at: datetime
    last_seen_at: datetime
    status: MemoryStatus = MemoryStatus.PROPOSED
    pinned: bool = False
    superseded_by_id: UUID | None = None
    conflict_ids: list[UUID] = Field(default_factory=list)
    conflict_state: ConflictState = ConflictState.NONE
    creator_version_from: UUID | None = None
    creator_version_to: UUID | None = None
    created_by: UUID | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def _consistent_state(self) -> MemoryItem:
        if self.status == MemoryStatus.SUPERSEDED and self.superseded_by_id is None:
            raise ValueError("a superseded item needs superseded_by_id")
        if self.kind.split(".")[0] != self.category:
            raise ValueError(f"kind {self.kind} does not belong to category {self.category}")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at is before first_seen_at")
        return self

    @property
    def influences_planning(self) -> bool:
        """Only active items (pinned or not) influence planning (§18.2)."""
        return self.status == MemoryStatus.ACTIVE


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(text.split())


def dedup_key(vocab: Vocabulary, kind: str, value: dict[str, Any]) -> str:
    """The normalized dedup key: the kind's key fields, NFKC + case-folded + whitespace-collapsed."""
    spec = vocab.memory_kinds[kind]
    return "|".join(_normalize_text(value.get(name) or "") for name in spec.key_fields)


def value_hash(value: dict[str, Any]) -> str:
    return content_digest(value)


def validate_memory_item(item: MemoryItem, vocab: Vocabulary) -> list[Issue]:
    issues: list[Issue] = []
    if item.kind not in vocab.memory_kinds:
        return [Issue("unknown_vocab", f"unknown memory kind {item.kind!r}", path="/kind")]
    try:
        typed = vocab.validate_memory_value(item.kind, item.value)
    except ValueError as exc:
        return [Issue("memory_value", f"{item.kind}: {exc}", path="/value")]
    normalized = typed.model_dump(mode="json")
    if item.key != dedup_key(vocab, item.kind, normalized):
        issues.append(Issue("memory_key", "key does not match the kind's normalized dedup key", path="/key"))
    if item.value_hash != value_hash(normalized):
        issues.append(Issue("memory_value_hash", "value_hash does not match the value", path="/value_hash"))
    return issues


class SnapshotItem(SpecModel):
    """A copied memory value: later edits to the item never change the snapshot (I7)."""

    item_id: UUID
    kind: NonEmptyStr
    value: dict[str, Any]
    text: str = ""
    pinned: bool = False
    confidence: Unit = 1.0


class MemorySnapshot(SpecModel):
    id: UUID
    creator_id: UUID
    creator_version_id: UUID
    character_key: CharacterKey | None = None
    items: list[SnapshotItem] = Field(default_factory=list)
    retrieval_params: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[tuple[UUID, UUID]] = Field(default_factory=list)
    created_at: datetime

    def content(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include={"creator_version_id", "items", "retrieval_params", "conflicts"})

    def digest(self) -> str:
        """Hash of the snapshot content (ids of the snapshot row and timestamps excluded)."""
        return content_digest(self.content())
