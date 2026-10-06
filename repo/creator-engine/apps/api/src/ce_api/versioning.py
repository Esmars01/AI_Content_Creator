"""Helpers for versioned identity records (draft → approved → archived, §10.2, I3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import ConflictError, ImmutableRecordError, NotFoundError
from ce_db.repository import OrgContext
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["get_scoped", "lock_scoped", "next_number", "require_draft"]


async def get_scoped(session: AsyncSession, model: Any, ctx: OrgContext, row_id: UUID, what: str) -> Any:
    row = (
        await session.execute(sa.select(model).where(model.org_id == ctx.org_id, model.id == row_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"{what} not found")
    return row


async def lock_scoped(session: AsyncSession, model: Any, ctx: OrgContext, row_id: UUID, what: str) -> Any:
    """The row, locked for update: serializes version numbering and approvals per parent."""
    row = (
        await session.execute(sa.select(model).where(model.org_id == ctx.org_id, model.id == row_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"{what} not found")
    return row


async def next_number(session: AsyncSession, version_model: Any, parent_column: Any, parent_id: UUID) -> int:
    current = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.max(version_model.number), 0)).where(parent_column == parent_id)
        )
    ).scalar_one()
    return int(current) + 1


def require_draft(row: Any, what: str) -> None:
    if row.status == "draft":
        return
    if row.status in ("approved", "archived"):
        raise ImmutableRecordError(
            f"{what} {row.number} is {row.status} and immutable (I3); create a new draft version"
        )
    raise ConflictError(f"{what} {row.number} is {row.status}")
