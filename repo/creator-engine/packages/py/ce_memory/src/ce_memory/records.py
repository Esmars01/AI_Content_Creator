"""The retrieval view of a `creator_memory_items` row."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

__all__ = ["MemoryRecord"]


@dataclass(frozen=True)
class MemoryRecord:
    id: UUID
    category: str
    kind: str
    key: str
    value: dict[str, Any]
    text: str
    confidence: float
    last_seen_at: datetime
    status: str = "active"
    pinned: bool = False
    source_type: str = "authored"
    evidence_count: int = 1
    value_hash: str = ""
    conflict_state: str = "none"
    conflict_ids: tuple[UUID, ...] = field(default_factory=tuple)
    creator_version_from: UUID | None = None
    creator_version_to: UUID | None = None
    deleted: bool = False
    embedding: tuple[float, ...] | None = None  # an index (Phase 12), comparable only within `embedding_model`
    embedding_model: str | None = None
