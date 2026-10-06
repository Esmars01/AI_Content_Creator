"""Database access for retrieval: memory items, creator-version numbers, immutable snapshots and
the usage log. Org-scoped like every repository (I12)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.spec.videospec import VideoSpec
from ce_db.models.creators import CreatorVersion
from ce_db.models.memory import CreatorMemoryItem, CreatorUsageEvent, MemorySnapshot
from sqlalchemy.ext.asyncio import AsyncSession

from ce_memory.records import MemoryRecord
from ce_memory.repetition import UsageEntry
from ce_memory.retrieval import SnapshotDraft
from ce_memory.usage import usage_payload

__all__ = [
    "LIVE",
    "create_snapshot",
    "embedding_text",
    "items_without_embedding",
    "link_conflicts",
    "load_records",
    "propose_item",
    "recent_usage",
    "version_numbers",
    "write_usage_events",
]


async def load_records(session: AsyncSession, org_id: UUID, creator_id: UUID) -> list[MemoryRecord]:
    rows = (
        await session.execute(
            sa.select(CreatorMemoryItem).where(
                CreatorMemoryItem.org_id == org_id, CreatorMemoryItem.creator_id == creator_id
            )
        )
    ).scalars()
    return [
        MemoryRecord(
            id=r.id,
            category=r.category,
            kind=r.kind,
            key=r.key,
            value=dict(r.value or {}),
            text=r.text or "",
            confidence=float(r.confidence),
            last_seen_at=r.last_seen_at,
            status=r.status,
            pinned=bool(r.pinned),
            source_type=str((r.source or {}).get("type", "authored")),
            evidence_count=int(r.evidence_count),
            value_hash=r.value_hash,
            conflict_state=r.conflict_state,
            conflict_ids=tuple(r.conflict_ids or ()),
            creator_version_from=r.creator_version_from,
            creator_version_to=r.creator_version_to,
            deleted=r.deleted_at is not None or r.value is None,
            embedding=tuple(float(x) for x in r.embedding) if r.embedding is not None else None,
            embedding_model=r.embedding_model,
        )
        for r in rows
    ]


async def version_numbers(session: AsyncSession, org_id: UUID, creator_id: UUID) -> dict[UUID, int]:
    rows = (
        await session.execute(
            sa.select(CreatorVersion.id, CreatorVersion.number).where(
                CreatorVersion.org_id == org_id, CreatorVersion.creator_id == creator_id
            )
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


async def create_snapshot(
    session: AsyncSession,
    org_id: UUID,
    draft: SnapshotDraft,
    *,
    created_for_version_id: UUID | None = None,
    snapshot_id: UUID | None = None,
) -> UUID:
    """Inserts the immutable snapshot row and returns its id (I7). The Director allocates the id
    while planning (the spec pins it) and passes it here."""
    row = MemorySnapshot(
        **({"id": snapshot_id} if snapshot_id is not None else {}),
        org_id=org_id,
        creator_version_id=draft.creator_version_id,
        created_for_version_id=created_for_version_id,
        items=[i.model_dump(mode="json") for i in draft.items],
        params=draft.params,
        conflicts=draft.conflicts,
        digest=draft.digest(),
    )
    session.add(row)
    await session.flush()
    return row.id


async def recent_usage(
    session: AsyncSession, org_id: UUID, creator_id: UUID, *, exclude_video_id: UUID | None = None, limit: int = 20
) -> list[UsageEntry]:
    """The latest usage event of each distinct video of the creator, newest first (§18.3)."""
    rows = (
        await session.execute(
            sa.select(CreatorUsageEvent)
            .where(CreatorUsageEvent.org_id == org_id, CreatorUsageEvent.creator_id == creator_id)
            .order_by(CreatorUsageEvent.created_at.desc(), CreatorUsageEvent.id.desc())
        )
    ).scalars()
    seen: set[UUID] = set()
    out: list[UsageEntry] = []
    for row in rows:
        if row.video_id in seen or row.video_id == exclude_video_id:
            continue
        seen.add(row.video_id)
        out.append(
            UsageEntry(
                video_id=row.video_id,
                hooks=tuple(str(h.get("text", h)) if isinstance(h, dict) else str(h) for h in row.hooks or []),
                phrase_fingerprints=frozenset(str(p) for p in row.phrase_fingerprints or []),
                arc=tuple((row.arc_signature or {}).get("labels", [])),
                visual=dict(row.visual_signature or {}),
                hook_vector=tuple(float(x) for x in row.hook_embedding) if row.hook_embedding is not None else None,
                hook_model=row.hook_embedding_model,
            )
        )
        if len(out) >= limit:
            break
    return out


async def write_usage_events(
    session: AsyncSession,
    org_id: UUID,
    *,
    video_id: UUID,
    version_id: UUID,
    spec: VideoSpec,
    creators: Mapping[str, UUID],
    behavior_signatures: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    event: str = "ready",
    hook_embedding: Sequence[float] | None = None,
    hook_embedding_model: str | None = None,
) -> int:
    """One event per cast member (character → creator id); idempotent per (version, character, event)."""
    written = 0
    for member in spec.cast:
        creator_id = creators.get(member.key)
        if creator_id is None:
            continue
        exists = (
            await session.execute(
                sa.select(sa.func.count()).where(
                    CreatorUsageEvent.org_id == org_id,
                    CreatorUsageEvent.version_id == version_id,
                    CreatorUsageEvent.character_key == member.key,
                    CreatorUsageEvent.event == event,
                )
            )
        ).scalar_one()
        if exists:
            continue
        payload = usage_payload(spec, member.key, behavior_signatures=(behavior_signatures or {}).get(member.key, ()))
        session.add(
            CreatorUsageEvent(
                org_id=org_id,
                creator_id=creator_id,
                video_id=video_id,
                version_id=version_id,
                character_key=member.key,
                event=event,
                hooks=payload["hooks"],
                phrase_fingerprints=payload["phrase_fingerprints"],
                arc_signature=payload["arc_signature"],
                behavior_signatures=payload["behavior_signatures"],
                visual_signature=payload["visual_signature"],
                world_version_ids=payload["world_version_ids"],
                wardrobe_version_ids=payload["wardrobe_version_ids"],
                hook_embedding=list(hook_embedding) if hook_embedding is not None and payload["hooks"] else None,
                hook_embedding_model=hook_embedding_model if hook_embedding is not None and payload["hooks"] else None,
            )
        )
        written += 1
    await session.flush()
    return written


async def propose_item(
    session: AsyncSession,
    org_id: UUID,
    *,
    vocab: Any,
    creator_id: UUID,
    kind: str,
    value: Mapping[str, Any],
    text: str,
    source: Mapping[str, Any],
    confidence: float = 0.5,
    conflicts: list[CreatorMemoryItem] | None = None,
) -> UUID | None:
    """A `proposed` memory item (§18.4: confirmed or dismissed by the user), e.g. from an edit's
    `memory_feedback`. An identical live item is not duplicated (its id is returned). A live item
    with the same key and another value is marked conflicting with it (§18.7); the conflicting
    rows are appended to `conflicts` when given."""
    from ce_core.identity.memory import dedup_key, value_hash

    typed = vocab.validate_memory_value(kind, value).model_dump(mode="json")
    key = dedup_key(vocab, kind, typed)
    digest = value_hash(typed)
    existing = (
        await session.execute(
            sa.select(CreatorMemoryItem.id).where(
                CreatorMemoryItem.org_id == org_id,
                CreatorMemoryItem.creator_id == creator_id,
                CreatorMemoryItem.kind == kind,
                CreatorMemoryItem.key == key,
                CreatorMemoryItem.value_hash == digest,
                CreatorMemoryItem.status.in_(("proposed", "active")),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    item = CreatorMemoryItem(
        org_id=org_id,
        creator_id=creator_id,
        category=kind.split(".")[0],
        kind=kind,
        key=key,
        value_hash=digest,
        value=typed,
        text=text,
        vocab_version=vocab.version,
        source=dict(source),
        confidence=confidence,
        status="proposed",
    )
    session.add(item)
    await session.flush()
    linked = await link_conflicts(session, item)
    if conflicts is not None and linked:
        conflicts.extend([item, *linked])
    await session.flush()
    return item.id


LIVE = ("proposed", "active")


async def link_conflicts(session: AsyncSession, item: CreatorMemoryItem) -> list[CreatorMemoryItem]:
    """Marks live items with the same (creator, kind, key) and another value as conflicting with
    `item` (§18.7): both rows stay; the user keeps one, the other, or both with scopes. Shared by
    the API (authored items) and `MemoryUpdateWorkflow` (proposals)."""
    others = (
        (
            await session.execute(
                sa.select(CreatorMemoryItem)
                .where(
                    CreatorMemoryItem.org_id == item.org_id,
                    CreatorMemoryItem.creator_id == item.creator_id,
                    CreatorMemoryItem.kind == item.kind,
                    CreatorMemoryItem.key == item.key,
                    CreatorMemoryItem.value_hash != item.value_hash,
                    CreatorMemoryItem.status.in_(LIVE),
                    CreatorMemoryItem.id != item.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        other.conflict_ids = sorted({*other.conflict_ids, item.id})
        other.conflict_state = "unresolved"
    if others:
        item.conflict_ids = sorted({*item.conflict_ids, *(o.id for o in others)})
        item.conflict_state = "unresolved"
    return list(others)


async def items_without_embedding(
    session: AsyncSession, org_id: UUID, creator_id: UUID, *, model: str | None, kinds: Sequence[str], limit: int = 500
) -> list[CreatorMemoryItem]:
    """Live text-like items whose embedding is missing or was made by another model."""
    query = sa.select(CreatorMemoryItem).where(
        CreatorMemoryItem.org_id == org_id,
        CreatorMemoryItem.creator_id == creator_id,
        CreatorMemoryItem.status.in_(LIVE),
        CreatorMemoryItem.deleted_at.is_(None),
        CreatorMemoryItem.kind.in_(list(kinds)),
    )
    if model is not None:
        query = query.where(
            sa.or_(CreatorMemoryItem.embedding_model.is_(None), CreatorMemoryItem.embedding_model != model)
        )
    rows = (await session.execute(query.order_by(CreatorMemoryItem.id).limit(limit))).scalars().all()
    return list(rows)


def embedding_text(item: CreatorMemoryItem | MemoryRecord) -> str:
    """The text an item is embedded by: its summary plus its string values (stable order)."""
    value = dict(item.value or {})
    parts = [item.text or ""] + [str(value[k]) for k in sorted(value) if isinstance(value[k], str)]
    return " · ".join(p for p in parts if p)
