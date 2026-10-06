"""Org-scoped repositories (I12): every read and write is filtered by the caller's org.

A row of another org is indistinguishable from a missing row (`NotFoundError`), so tenant
existence never leaks. Approved versioned identity rows are immutable here as well as in the
database trigger (I3): the repository refuses before the database has to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import ImmutableRecordError, NotFoundError, PermissionDeniedError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from ce_db.errors import translate
from ce_db.rules import APPEND_ONLY_TABLES, IMMUTABLE_TABLES, VERSIONED_IDENTITY_TABLES

__all__ = ["GlobalRepository", "OrgContext", "TenantRepository"]


@dataclass(frozen=True)
class OrgContext:
    """Who is acting, and in which org. Required by every tenant repository method."""

    org_id: UUID
    user_id: UUID | None = None
    is_platform_admin: bool = False


def _pk_columns(model: type[DeclarativeBase]) -> Sequence[sa.ColumnElement[Any]]:
    return list(sa.inspect(model).primary_key)


class TenantRepository[T: DeclarativeBase]:
    def __init__(self, model: type[T], session: AsyncSession, ctx: OrgContext) -> None:
        if "org_id" not in model.__table__.c:  # type: ignore[attr-defined]
            raise TypeError(f"{model.__name__} is not a tenant table")
        self.model = model
        self.session = session
        self.ctx = ctx
        self.table: sa.Table = model.__table__  # type: ignore[attr-defined,assignment]

    # ------------------------------------------------------------------ helpers
    def _scoped(self) -> sa.Select[T]:
        return sa.select(self.model).where(self.table.c.org_id == self.ctx.org_id)

    def _pk_filter(self, key: Any) -> list[sa.ColumnElement[bool]]:
        columns = _pk_columns(self.model)
        values = key if isinstance(key, tuple) else (key,)
        if len(values) != len(columns):
            raise ValueError(f"{self.table.name} needs a primary key of {[c.name for c in columns]}")
        return [c == v for c, v in zip(columns, values, strict=True)]

    async def _flush(self) -> None:
        try:
            await self.session.flush()
        except DBAPIError as exc:
            raise translate(exc) from exc

    # ------------------------------------------------------------------ reads
    async def get(self, key: Any) -> T:
        row = (await self.session.execute(self._scoped().where(*self._pk_filter(key)))).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"{self.table.name} not found", table=self.table.name)
        return row

    async def find(self, *where: sa.ColumnElement[bool], limit: int = 500, **equals: Any) -> list[T]:
        query = self._scoped().where(*where, *(self.table.c[k] == v for k, v in equals.items())).limit(limit)
        return list((await self.session.execute(query)).scalars())

    # ------------------------------------------------------------------ writes
    async def add(self, obj: T) -> T:
        current = getattr(obj, "org_id", None)
        if current is not None and current != self.ctx.org_id:
            raise PermissionDeniedError("cannot create a record in another organization")
        obj.org_id = self.ctx.org_id  # type: ignore[attr-defined]
        self.session.add(obj)
        await self._flush()
        return obj

    async def update(self, key: Any, **values: Any) -> T:
        row = await self.get(key)
        if "org_id" in values and values["org_id"] != self.ctx.org_id:
            raise PermissionDeniedError("records cannot move between organizations")
        name = self.table.name
        if name in APPEND_ONLY_TABLES or name in IMMUTABLE_TABLES:
            raise ImmutableRecordError(f"{name} is append-only")
        status = getattr(row, "status", None)
        archiving = status == "approved" and set(values) == {"status"} and values["status"] == "archived"
        if name in VERSIONED_IDENTITY_TABLES and status in ("approved", "archived") and not archiving:
            raise ImmutableRecordError(f"{name}: approved versions are immutable (I3); create a new draft version")
        for column, value in values.items():
            setattr(row, column, value)
        await self._flush()
        return row

    async def delete(self, key: Any) -> None:
        row = await self.get(key)
        name = self.table.name
        if name in APPEND_ONLY_TABLES or name in IMMUTABLE_TABLES:
            raise ImmutableRecordError(f"{name} is append-only")
        if name in VERSIONED_IDENTITY_TABLES and getattr(row, "status", None) in ("approved", "archived"):
            raise ImmutableRecordError(f"{name}: approved versions cannot be deleted (I3)")
        await self.session.delete(row)
        await self._flush()


class GlobalRepository[T: DeclarativeBase]:
    """Platform tables (no org_id): readable by any authenticated caller, writable by platform admins only."""

    def __init__(self, model: type[T], session: AsyncSession, ctx: OrgContext) -> None:
        if "org_id" in model.__table__.c:  # type: ignore[attr-defined]
            raise TypeError(f"{model.__name__} is a tenant table; use TenantRepository")
        self.model = model
        self.session = session
        self.ctx = ctx

    def _require_admin(self) -> None:
        if not self.ctx.is_platform_admin:
            raise PermissionDeniedError("only platform admins can change global records")

    async def get(self, key: Any) -> T:
        row = await self.session.get(self.model, key)
        if row is None:
            raise NotFoundError(f"{self.model.__tablename__} not found")  # type: ignore[attr-defined]
        return row

    async def find(self, limit: int = 500, **equals: Any) -> list[T]:
        table = self.model.__table__  # type: ignore[attr-defined]
        query = sa.select(self.model).where(*(table.c[k] == v for k, v in equals.items())).limit(limit)
        return list((await self.session.execute(query)).scalars())

    async def add(self, obj: T) -> T:
        self._require_admin()
        self.session.add(obj)
        try:
            await self.session.flush()
        except DBAPIError as exc:
            raise translate(exc) from exc
        return obj

    async def update(self, key: Any, **values: Any) -> T:
        self._require_admin()
        row = await self.get(key)
        for column, value in values.items():
            setattr(row, column, value)
        try:
            await self.session.flush()
        except DBAPIError as exc:
            raise translate(exc) from exc
        return row

    async def delete(self, key: Any) -> None:
        self._require_admin()
        row = await self.get(key)
        await self.session.delete(row)
        await self.session.flush()
