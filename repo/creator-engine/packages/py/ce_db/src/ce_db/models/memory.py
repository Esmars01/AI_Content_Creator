"""Creator Memory (§18, §29). Usage events are append-only (trigger)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import ConflictState, MemoryStatus
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_uuid, check_in, embedding, jsonb, pk, tenant_fk


class CreatorMemoryItem(TenantMixin, Base):
    __tablename__ = "creator_memory_items"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID] = mapped_column(index=True)
    category: Mapped[str]
    kind: Mapped[str]
    key: Mapped[str]
    value_hash: Mapped[str]
    value: Mapped[dict[str, Any] | None] = jsonb(nullable=True)  # null only for hard-deleted tombstones
    text: Mapped[str | None] = mapped_column(server_default="")
    embedding: Mapped[Sequence[float] | None] = embedding()
    embedding_model: Mapped[str | None]  # the embed.text route that made `embedding` (Phase 12)
    vocab_version: Mapped[str]
    source: Mapped[dict[str, Any]] = jsonb()
    confidence: Mapped[float] = mapped_column(server_default="1.0")
    evidence_count: Mapped[int] = mapped_column(server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    status: Mapped[str] = mapped_column(server_default=MemoryStatus.PROPOSED.value)
    pinned: Mapped[bool] = mapped_column(server_default=sa.false())
    superseded_by_id: Mapped[UUID | None]
    conflict_ids: Mapped[list[UUID]] = array_uuid()
    conflict_state: Mapped[str] = mapped_column(server_default=ConflictState.NONE.value)
    creator_version_from: Mapped[UUID | None]
    creator_version_to: Mapped[UUID | None]
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["org_id", "superseded_by_id"], ["creator_memory_items.org_id", "creator_memory_items.id"]
        ),
        tenant_fk("creator_version_from", "creator_versions"),
        tenant_fk("creator_version_to", "creator_versions"),
        check_in("status", [s.value for s in MemoryStatus]),
        check_in("conflict_state", [s.value for s in ConflictState]),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        sa.CheckConstraint("status <> 'superseded' OR superseded_by_id IS NOT NULL", name="superseded_by"),
        # Conflicting values can coexist (§18.7): uniqueness is per value, among live items.
        sa.Index(
            "uq_creator_memory_items_live_value",
            "creator_id",
            "kind",
            "key",
            "value_hash",
            unique=True,
            postgresql_where=sa.text("status IN ('proposed', 'active')"),
        ),
        sa.Index(
            "ix_creator_memory_items_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class MemorySnapshot(TenantMixin, Base):
    """Immutable (trigger): later memory changes never alter an existing plan (I7)."""

    __tablename__ = "memory_snapshots"
    id: Mapped[UUID] = pk()
    creator_version_id: Mapped[UUID] = mapped_column(index=True)
    created_for_version_id: Mapped[UUID | None]
    items: Mapped[list[Any]] = jsonb(default=[])
    params: Mapped[dict[str, Any]] = jsonb(default={})
    conflicts: Mapped[list[Any]] = jsonb(default=[])
    digest: Mapped[str]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_version_id", "creator_versions"),
        tenant_fk("created_for_version_id", "video_versions"),
    )


class CreatorUsageEvent(TenantMixin, Base):
    __tablename__ = "creator_usage_events"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID] = mapped_column(index=True)
    video_id: Mapped[UUID] = mapped_column(index=True)
    version_id: Mapped[UUID]
    character_key: Mapped[str]
    event: Mapped[str]
    hooks: Mapped[list[Any]] = jsonb(default=[])
    hook_embedding: Mapped[Sequence[float] | None] = embedding()
    hook_embedding_model: Mapped[str | None]  # the embed.text route that made `hook_embedding` (Phase 12)
    phrase_fingerprints: Mapped[list[Any]] = jsonb(default=[])
    arc_signature: Mapped[dict[str, Any]] = jsonb(default={})
    behavior_signatures: Mapped[list[Any]] = jsonb(default=[])
    visual_signature: Mapped[dict[str, Any]] = jsonb(default={})
    world_version_ids: Mapped[list[UUID]] = array_uuid()
    wardrobe_version_ids: Mapped[list[UUID]] = array_uuid()
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        tenant_fk("video_id", "videos", ondelete="CASCADE"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        check_in("event", ("ready", "exported")),
        sa.Index(
            "ix_creator_usage_events_hook_embedding_hnsw",
            "hook_embedding",
            postgresql_using="hnsw",
            postgresql_ops={"hook_embedding": "vector_cosine_ops"},
        ),
    )
