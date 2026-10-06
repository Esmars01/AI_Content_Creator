"""Shared endpoint helpers: audit entries, cursor pagination, idempotency keys."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import ConflictError, InvalidInputError, Issue
from ce_db.models.platform import AuditLog
from ce_db.models.tenancy import IdempotencyKey
from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.security.auth import Principal

__all__ = [
    "IDEMPOTENCY_HEADER",
    "Page",
    "StoredResponse",
    "audit",
    "begin_idempotent",
    "decode_cursor",
    "encode_cursor",
    "finish_idempotent",
    "page",
]

IDEMPOTENCY_HEADER = "Idempotency-Key"


# ---------------------------------------------------------------------- audit
async def audit(
    session: AsyncSession,
    principal: Principal | None,
    action: str,
    target_type: str,
    target_id: object | None,
    *,
    request: Request | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    org_id: UUID | None = None,
) -> None:
    """Append an audit entry (§33 list: role changes, memory forget/delete, approvals, …)."""
    session.add(
        AuditLog(
            org_id=org_id or (principal.org_id if principal else None),
            actor_user_id=principal.user_id if principal else None,
            actor_kind=principal.actor_kind if principal else "system",
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            before=before,
            after=after,
            ip=request.client.host if request is not None and request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:512] or None if request is not None else None,
        )
    )
    await session.flush()


# ---------------------------------------------------------------------- pagination
class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(default=None, description="pass as ?cursor= to get the next page")


def encode_cursor(last_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"v1:{last_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        version, _, value = raw.partition(":")
        if version != "v1":
            raise ValueError(version)
        return UUID(value)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidInputError("invalid cursor", issues=[Issue("cursor", "the cursor is malformed")]) from exc


async def page(
    session: AsyncSession,
    query: sa.Select[Any],
    id_column: Any,
    *,
    cursor: str | None,
    limit: int,
) -> tuple[Sequence[Any], str | None]:
    """Newest first (UUIDv7 ids are time-ordered). Returns the rows and the next cursor."""
    if cursor:
        query = query.where(id_column < decode_cursor(cursor))
    rows = (await session.execute(query.order_by(id_column.desc()).limit(limit + 1))).scalars().all()
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(rows[-1].id)
    return rows, None


# ---------------------------------------------------------------------- idempotency
@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: dict[str, Any]


def request_fingerprint(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(b"\n".join([method.upper().encode(), path.encode(), body])).hexdigest()


async def begin_idempotent(
    session: AsyncSession,
    principal: Principal,
    request: Request,
    *,
    ttl_s: int,
    now: datetime,
) -> tuple[str, StoredResponse | None]:
    """Claim `Idempotency-Key` for this request, or return the stored response of an earlier one.

    The claim is part of the request transaction: a failed request leaves no claim behind, and a
    concurrent duplicate waits on the row lock, then replays the committed response.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not key:
        raise InvalidInputError(
            f"this request starts work and needs an {IDEMPOTENCY_HEADER} header",
            issues=[Issue("idempotency_key_missing", f"send a unique {IDEMPOTENCY_HEADER} (e.g. a UUID)")],
        )
    if len(key) > 255 or not key.isprintable():
        raise InvalidInputError(f"{IDEMPOTENCY_HEADER} is at most 255 printable characters")
    fingerprint = request_fingerprint(request.method, request.url.path, await request.body())
    table: sa.Table = IdempotencyKey.__table__  # type: ignore[assignment]
    await session.execute(
        sa.delete(table).where(table.c.org_id == principal.org_id, table.c.key == key, table.c.expires_at <= now)
    )
    await session.execute(
        insert(table)
        .values(
            org_id=principal.org_id,
            key=key,
            request_hash=fingerprint,
            status="in_progress",
            expires_at=now + timedelta(seconds=ttl_s),
        )
        .on_conflict_do_nothing(index_elements=["org_id", "key"])
    )
    row = (
        await session.execute(
            sa.select(table.c.request_hash, table.c.status, table.c.response).where(
                table.c.org_id == principal.org_id, table.c.key == key
            )
        )
    ).one()
    if row.request_hash != fingerprint:
        raise InvalidInputError(
            f"{IDEMPOTENCY_HEADER} was already used for a different request",
            issues=[Issue("idempotency_key_reused", "use a new key for a different request")],
        )
    if row.status == "completed" and row.response is not None:
        return key, StoredResponse(int(row.response["status"]), dict(row.response["body"]))
    if row.status == "in_progress" and row.response is not None:  # pragma: no cover - defensive
        raise ConflictError("the original request is still in progress")
    return key, None


async def finish_idempotent(
    session: AsyncSession, principal: Principal, key: str, status: int, body: BaseModel | dict[str, Any]
) -> None:
    payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else json.loads(json.dumps(body, default=str))
    table: sa.Table = IdempotencyKey.__table__  # type: ignore[assignment]
    await session.execute(
        sa.update(table)
        .where(table.c.org_id == principal.org_id, table.c.key == key)
        .values(status="completed", response={"status": status, "body": payload})
    )
