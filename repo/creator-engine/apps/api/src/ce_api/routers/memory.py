"""Creator Memory (§18, §30): list, author, actions, history, delete; usage and snapshots (read).

Authored items are `active` with confidence 1 (§18.4). Inserting the same `(creator, kind, key)`
with a different value marks both live items `conflict_state=unresolved` (§18.7). Every action is
audited; `GET …/history` reads those entries. Hard delete (`DELETE`) is a `deletion` job that
leaves a tombstone (§18.9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_core.identity.memory import dedup_key, value_hash
from ce_db.models.creators import Creator, CreatorVersion
from ce_db.models.memory import CreatorMemoryItem, CreatorUsageEvent, MemorySnapshot
from ce_db.models.platform import AuditLog
from ce_db.models.videos import VideoVersion
from ce_memory.store import link_conflicts
from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.common import Page, audit, page
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.events import EventType
from ce_api.jobs import create_job, start_job, start_studio_job
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["memory"])

LIVE = ("proposed", "active")


class MemoryItemOut(Out):
    id: UUID
    creator_id: UUID
    category: str
    kind: str
    key: str
    value: dict[str, Any] | None
    text: str | None
    vocab_version: str
    source: dict[str, Any]
    confidence: float
    evidence_count: int
    status: str
    pinned: bool
    superseded_by_id: UUID | None
    conflict_ids: list[UUID]
    conflict_state: str
    creator_version_from: UUID | None
    creator_version_to: UUID | None
    embedding_model: str | None = Field(description="the embed.text route that indexed the item; null: not embedded")
    deleted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class MemoryCreate(Body):
    kind: str = Field(description="a memory kind from config/vocab/memory_kinds.yaml")
    value: dict[str, Any]
    text: Annotated[str, Field(max_length=2000)] = ""
    pinned: bool = False
    creator_version_from: UUID | None = None
    creator_version_to: UUID | None = None

    model_config = examples(
        [
            {
                "kind": "persona_fact",
                "value": {"subject": "Alex", "predicate": "owns", "object": "a grey cat"},
                "text": "Alex has a grey cat.",
            }
        ]
    )


class ScopeRange(Body):
    creator_version_from: UUID | None = None
    creator_version_to: UUID | None = None


class MemoryAction(Body):
    action: Literal["pin", "unpin", "forget", "activate", "dismiss", "supersede", "resolve_conflict", "edit"]
    by: UUID | None = Field(default=None, description="supersede: the item that replaces this one")
    keep: UUID | Literal["both"] | None = Field(default=None, description="resolve_conflict: the item to keep, or both")
    scopes: dict[UUID, ScopeRange] = Field(
        default_factory=dict,
        description="resolve_conflict with keep=both: creator-version scopes per item, so both stay true "
        "for different creator versions (§18.7 'keep both with scope')",
    )
    value: dict[str, Any] | None = Field(default=None, description="edit (authored items only)")
    text: Annotated[str | None, Field(max_length=2000)] = None

    model_config = examples([{"action": "pin"}, {"action": "resolve_conflict", "keep": "both"}])


class HistoryEntry(Out):
    action: str
    actor_user_id: UUID | None
    actor_kind: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime


class DeletionAccepted(Out):
    job_id: UUID
    memory_item_id: UUID


class UsageEventOut(Out):
    id: UUID
    creator_id: UUID
    video_id: UUID
    version_id: UUID
    character_key: str
    event: str
    hooks: list[Any]
    world_version_ids: list[UUID]
    wardrobe_version_ids: list[UUID]
    created_at: datetime


class SnapshotOut(Out):
    id: UUID
    creator_version_id: UUID
    created_for_version_id: UUID | None
    items: list[Any]
    params: dict[str, Any]
    conflicts: list[Any]
    digest: str
    created_at: datetime


def _snapshot(item: CreatorMemoryItem) -> dict[str, Any]:
    return {
        "status": item.status,
        "pinned": item.pinned,
        "conflict_state": item.conflict_state,
        "superseded_by_id": str(item.superseded_by_id) if item.superseded_by_id else None,
        "value": item.value,
        "text": item.text,
    }


def _typed(services: ServicesDep, kind: str, value: dict[str, Any]) -> dict[str, Any]:
    vocab = services.vocab
    if kind not in vocab.memory_kinds:
        raise InvalidInputError(
            "unknown memory kind", issues=[Issue("unknown_vocab", f"unknown memory kind {kind!r}", path="/kind")]
        )
    try:
        typed: dict[str, Any] = vocab.validate_memory_value(kind, value).model_dump(mode="json")
    except ValidationError as exc:
        issues = [
            Issue(str(e["type"]), str(e["msg"]), path="/value/" + "/".join(str(p) for p in e["loc"]))
            for e in exc.errors()
        ]
        raise InvalidInputError(f"invalid value for {kind}", issues=issues) from exc
    return typed


async def _check_scope(session: AsyncSession, org_id: UUID, creator_id: UUID, *version_ids: UUID | None) -> None:
    for version_id in version_ids:
        if version_id is None:
            continue
        owner = (
            await session.execute(
                sa.select(CreatorVersion.creator_id).where(
                    CreatorVersion.org_id == org_id, CreatorVersion.id == version_id
                )
            )
        ).scalar_one_or_none()
        if owner != creator_id:
            raise InvalidInputError(
                "version scope must be a version of this creator", issues=[Issue("creator_version", "not found")]
            )


async def _link_conflicts(session: AsyncSession, item: CreatorMemoryItem) -> list[CreatorMemoryItem]:
    """Mark live items with the same (creator, kind, key) and another value as conflicting (§18.7)."""
    return await link_conflicts(session, item)


async def _item(
    session: AsyncSession, principal: Any, memory_item_id: UUID, *, lock: bool = False
) -> CreatorMemoryItem:
    fn = lock_scoped if lock else get_scoped
    item: CreatorMemoryItem = await fn(session, CreatorMemoryItem, principal.ctx, memory_item_id, "memory item")
    return item


@router.get("/v1/creators/{creator_id}/memory", response_model=Page[MemoryItemOut])
async def list_memory(
    creator_id: UUID,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    category: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[MemoryItemOut]:
    await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    query = sa.select(CreatorMemoryItem).where(
        CreatorMemoryItem.org_id == principal.org_id, CreatorMemoryItem.creator_id == creator_id
    )
    if category:
        query = query.where(CreatorMemoryItem.category == category)
    if kind:
        query = query.where(CreatorMemoryItem.kind == kind)
    if status:
        query = query.where(CreatorMemoryItem.status == status)
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(CreatorMemoryItem.text.ilike(pattern, escape="\\"))
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, CreatorMemoryItem.id, cursor=cursor, limit=size)
    return Page[MemoryItemOut](items=[MemoryItemOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.post("/v1/creators/{creator_id}/memory", response_model=MemoryItemOut, status_code=201)
async def create_memory(
    creator_id: UUID,
    body: MemoryCreate,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> MemoryItemOut:
    await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    await _check_scope(session, principal.org_id, creator_id, body.creator_version_from, body.creator_version_to)
    typed = _typed(services, body.kind, body.value)
    key = dedup_key(services.vocab, body.kind, typed)
    digest = value_hash(typed)
    duplicate = (
        await session.execute(
            sa.select(CreatorMemoryItem.id).where(
                CreatorMemoryItem.org_id == principal.org_id,
                CreatorMemoryItem.creator_id == creator_id,
                CreatorMemoryItem.kind == body.kind,
                CreatorMemoryItem.key == key,
                CreatorMemoryItem.value_hash == digest,
                CreatorMemoryItem.status.in_(LIVE),
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError("the creator already remembers this", memory_item_id=str(duplicate))
    now = services.clock()
    item = CreatorMemoryItem(
        org_id=principal.org_id,
        creator_id=creator_id,
        category=body.kind.split(".")[0],
        kind=body.kind,
        key=key,
        value_hash=digest,
        value=typed,
        text=body.text,
        vocab_version=services.vocab.version,
        source={"type": "authored"},
        confidence=1.0,
        status="active",
        pinned=body.pinned,
        creator_version_from=body.creator_version_from,
        creator_version_to=body.creator_version_to,
        created_by=principal.user_id,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(item)
    await session.flush()
    conflicts = await _link_conflicts(session, item)
    await audit(
        session, principal, "memory_item.create", "memory_item", item.id, request=request, after=_snapshot(item)
    )
    await session.flush()
    await session.refresh(item)
    if conflicts:
        await services.events.publish(
            principal.org_id,
            EventType.MEMORY_CONFLICT,
            {"creator_id": str(creator_id), "memory_item_ids": [str(item.id), *(str(c.id) for c in conflicts)]},
        )
    await _index(session, services, background, principal, creator_id)
    return MemoryItemOut.model_validate(item)


async def _index(session: AsyncSession, services: Any, background: Any, principal: Any, creator_id: UUID) -> None:
    """A `memory_update` job (trigger `embed`) indexes the creator's new or edited items (Phase 12):
    the API never calls a model itself (§9)."""
    await start_studio_job(
        session,
        services,
        background,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kind=JobKind.MEMORY_UPDATE,
        target_type="creator",
        target_id=creator_id,
        args={"trigger": "embed"},
    )


@router.patch("/v1/memory-items/{memory_item_id}", response_model=MemoryItemOut)
async def act_on_memory(
    memory_item_id: UUID,
    body: MemoryAction,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> MemoryItemOut:
    item = await _item(session, principal, memory_item_id, lock=True)
    if item.deleted_at is not None:
        raise NotFoundError("memory item not found")
    before = _snapshot(item)
    action = body.action

    def need(condition: bool, message: str) -> None:
        if not condition:
            raise ConflictError(message, action=action, status=item.status)

    if action in ("pin", "unpin"):
        need(item.status in LIVE, "only live items can be pinned")
        item.pinned = action == "pin"
    elif action == "forget":
        need(item.status in LIVE, "the item is not live")
        item.status, item.pinned = "forgotten", False
    elif action == "activate":
        need(item.status == "proposed", "only proposed items can be activated")
        item.status = "active"
    elif action == "dismiss":
        need(item.status == "proposed", "only proposed items can be dismissed")
        item.status = "forgotten"
    elif action == "supersede":
        if body.by is None:
            raise InvalidInputError("supersede needs `by`", issues=[Issue("by", "required", path="/by")])
        by = await _item(session, principal, body.by, lock=True)
        need(item.status in LIVE and by.status in LIVE, "both items must be live")
        if (by.creator_id, by.kind, by.key) != (item.creator_id, item.kind, item.key) or by.id == item.id:
            raise InvalidInputError("supersede needs another item of the same creator, kind and key")
        item.status, item.superseded_by_id, item.pinned = "superseded", by.id, False
        await _resolve_links(session, item, by)
    elif action == "resolve_conflict":
        need(item.conflict_state == "unresolved", "the item has no unresolved conflict")
        if body.keep is None:
            raise InvalidInputError("resolve_conflict needs `keep`", issues=[Issue("keep", "required", path="/keep")])
        others = [await _item(session, principal, other_id, lock=True) for other_id in item.conflict_ids]
        group = [item, *others]
        if body.keep == "both":
            members = {m.id: m for m in group}
            for item_id, scope in body.scopes.items():
                if item_id not in members:
                    raise InvalidInputError(
                        "scopes name items outside the conflict", issues=[Issue("scopes", str(item_id), path="/scopes")]
                    )
                await _check_scope(
                    session, principal.org_id, item.creator_id, scope.creator_version_from, scope.creator_version_to
                )
                members[item_id].creator_version_from = scope.creator_version_from
                members[item_id].creator_version_to = scope.creator_version_to
            for member in group:
                member.conflict_state = "resolved"
        else:
            kept = next((m for m in group if m.id == body.keep), None)
            if kept is None:
                raise InvalidInputError(
                    "keep must be this item or one it conflicts with", issues=[Issue("keep", "not in the conflict")]
                )
            for member in group:
                member.conflict_state = "resolved"
                if member.id != kept.id and member.status in LIVE:
                    member.status, member.superseded_by_id, member.pinned = "superseded", kept.id, False
    elif action == "edit":
        need(item.source.get("type") == "authored", "only authored items can be edited")
        need(item.status in LIVE, "only live items can be edited")
        if body.value is None and body.text is None:
            raise InvalidInputError("edit needs value or text")
        if body.value is not None:
            typed = _typed(services, item.kind, body.value)
            item.value, item.value_hash = typed, value_hash(typed)
            item.key = dedup_key(services.vocab, item.kind, typed)
            item.conflict_ids, item.conflict_state = [], "none"
            await session.flush()
            await _link_conflicts(session, item)
        if body.text is not None:
            item.text = body.text
        item.embedding, item.embedding_model = None, None  # re-indexed by the embed job
        await session.flush()
        await _index(session, services, background, principal, item.creator_id)
    item.last_seen_at = services.clock() if action in ("activate", "edit") else item.last_seen_at
    await session.flush()
    await audit(
        session,
        principal,
        f"memory_item.{action}",
        "memory_item",
        item.id,
        request=request,
        before=before,
        after=_snapshot(item),
    )
    await session.refresh(item)
    return MemoryItemOut.model_validate(item)


async def _resolve_links(session: AsyncSession, item: CreatorMemoryItem, by: CreatorMemoryItem) -> None:
    if by.id in item.conflict_ids:
        item.conflict_state = "resolved"
        if all(c == item.id or c in item.conflict_ids for c in by.conflict_ids):
            by.conflict_state = "resolved"


@router.get("/v1/memory-items/{memory_item_id}/history", response_model=list[HistoryEntry])
async def memory_history(memory_item_id: UUID, principal: Reader, session: DbSession) -> list[HistoryEntry]:
    await _item(session, principal, memory_item_id)
    rows = (
        await session.execute(
            sa.select(AuditLog)
            .where(
                AuditLog.org_id == principal.org_id,
                AuditLog.target_type == "memory_item",
                AuditLog.target_id == str(memory_item_id),
            )
            .order_by(AuditLog.id)
        )
    ).scalars()
    return [HistoryEntry.model_validate(r) for r in rows]


@router.delete("/v1/memory-items/{memory_item_id}", response_model=DeletionAccepted, status_code=202)
async def delete_memory(
    memory_item_id: UUID,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> DeletionAccepted:
    item = await _item(session, principal, memory_item_id, lock=True)
    if item.deleted_at is not None:
        raise NotFoundError("memory item not found")
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.DELETION,
        target_type="memory_item",
        target_id=item.id,
        requested_by=principal.user_id,
    )
    await audit(
        session, principal, "memory_item.delete", "memory_item", item.id, request=request, before=_snapshot(item)
    )
    org_id, item_id = principal.org_id, item.id

    background.add_task(
        start_job,
        services,
        org_id,
        job.id,
        JobKind.DELETION,
        {"org_id": str(org_id), "job_id": str(job.id), "target_type": "memory_item", "target_id": str(item_id)},
    )
    return DeletionAccepted(job_id=job.id, memory_item_id=item.id)


@router.get("/v1/creators/{creator_id}/usage", response_model=list[UsageEventOut])
async def creator_usage(
    creator_id: UUID, principal: Reader, session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[UsageEventOut]:
    await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    rows = (
        await session.execute(
            sa.select(CreatorUsageEvent)
            .where(CreatorUsageEvent.org_id == principal.org_id, CreatorUsageEvent.creator_id == creator_id)
            .order_by(CreatorUsageEvent.id.desc())
            .limit(limit)
        )
    ).scalars()
    return [UsageEventOut.model_validate(r) for r in rows]


@router.get("/v1/versions/{version_id}/memory-snapshots", response_model=list[SnapshotOut])
async def version_snapshots(version_id: UUID, principal: Reader, session: DbSession) -> list[SnapshotOut]:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    pinned = [
        UUID(str(s["snapshot_id"]))
        for s in (version.spec.get("memory", {}) or {}).get("snapshots", [])
        if isinstance(s, dict) and s.get("snapshot_id")
    ]
    query = sa.select(MemorySnapshot).where(
        MemorySnapshot.org_id == principal.org_id,
        sa.or_(MemorySnapshot.created_for_version_id == version_id, MemorySnapshot.id.in_(pinned or [version_id])),
    )
    return [SnapshotOut.model_validate(r) for r in (await session.execute(query.order_by(MemorySnapshot.id))).scalars()]
