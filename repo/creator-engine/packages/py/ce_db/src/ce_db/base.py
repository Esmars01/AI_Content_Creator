"""Declarative base, column helpers and conventions for every table (§29).

Conventions implemented here:
- UUIDv7 primary keys: Postgres 18's `uuidv7()` as the server default, `ce_core.ids.new_id()` in Python;
- `created_at` / `updated_at` on every table;
- tenant tables carry an indexed `org_id` and a `UNIQUE (org_id, id)` so children can reference
  them through composite foreign keys `(org_id, parent_id)` — a row can never point at another
  org's row (I12);
- closed value sets are `text` columns with CHECK constraints (no Postgres enums, so adding a
  value is a plain migration), named `ck_<table>_<column>`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

import sqlalchemy as sa
from ce_core.ids import new_id
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "EMBEDDING_DIM",
    "Base",
    "TenantMixin",
    "array_text",
    "array_uuid",
    "check_in",
    "embedding",
    "jsonb",
    "pk",
    "tenant_fk",
]

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Fixed when the schema is created (pgvector HNSW indexes need ≤ 2000 dimensions, §7).
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM") or 1024)
if not 1 <= EMBEDDING_DIM <= 2000:
    raise ValueError("EMBEDDING_DIM must be between 1 and 2000 (pgvector HNSW limit)")


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )


def pk() -> Mapped[UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=new_id, server_default=sa.text("uuidv7()"))


class TenantMixin:
    """Tenant-owned rows: `org_id` (indexed, FK to organizations) and UNIQUE (org_id, id) when there is an id."""

    org_id: Mapped[UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )


def tenant_fk(
    column: str, parent: str, *, ondelete: str | None = None, cycle_name: str | None = None
) -> sa.ForeignKeyConstraint:
    """Composite FK (org_id, column) → parent(org_id, id): references cannot cross tenants.

    `cycle_name` marks an FK that closes a dependency cycle between tables; it is created after
    all tables (`use_alter`) under that explicit name.
    """
    return sa.ForeignKeyConstraint(
        ["org_id", column],
        [f"{parent}.org_id", f"{parent}.id"],
        ondelete=ondelete,
        use_alter=cycle_name is not None,
        name=cycle_name,
    )


def org_id_unique() -> sa.UniqueConstraint:
    return sa.UniqueConstraint("org_id", "id")


def check_in(column: str, values: Iterable[str], *, nullable: bool = False) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=column)


def jsonb(*, nullable: bool = False, default: Any = None) -> Mapped[Any]:
    server_default = None
    if default is not None:
        server_default = sa.text("'[]'::jsonb") if default == [] else sa.text("'{}'::jsonb")
    return mapped_column(JSONB, nullable=nullable, server_default=server_default)


def array_uuid(*, nullable: bool = False) -> Mapped[list[UUID]]:
    return mapped_column(ARRAY(sa.Uuid), nullable=nullable, server_default=sa.text("'{}'"))


def array_text(*, nullable: bool = False) -> Mapped[list[str]]:
    return mapped_column(ARRAY(sa.Text), nullable=nullable, server_default=sa.text("'{}'"))


def embedding() -> Mapped[Sequence[float] | None]:
    return mapped_column(Vector(EMBEDDING_DIM), nullable=True)
