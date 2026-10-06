"""Builds minimal valid rows for any table of §29, creating required parents in the same org.

Used by the cross-tenant suite (I12) and immutability tests to exercise every table without
hand-writing 75 factories. Values satisfy the schema's NOT NULL, CHECK and FK constraints.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import ce_db.models  # noqa: F401 - registers every table
import sqlalchemy as sa
from ce_core.ids import new_id
from ce_db.base import Base
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["RowFactory"]

_IN = re.compile(r"(\w+) IN \((.*?)\)")

# Columns whose CHECK constraints relate several columns: pick values that satisfy them.
OVERRIDES: dict[str, dict[str, Any]] = {
    "behavior_observations": {"level_scope": "viewer"},
    "users": {},
}


class RowFactory:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tables = Base.metadata.tables
        self._counter = 0

    def _unique(self, name: str) -> str:
        self._counter += 1
        return f"{name}-{self._counter}-{new_id().hex[-12:]}"

    def _allowed(self, table: sa.Table) -> dict[str, str]:
        allowed: dict[str, str] = {}
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                match = _IN.search(str(constraint.sqltext))
                if match:
                    allowed[match.group(1)] = match.group(2).split(",")[0].strip().strip("'")
        return allowed

    def _value(self, column: sa.Column[Any], allowed: dict[str, str]) -> Any:
        if column.name in allowed:
            return allowed[column.name]
        if column.name == "email":
            return f"{self._unique('user').lower()}@example.test"
        kind = column.type
        if isinstance(kind, sa.Uuid):
            return new_id()
        if isinstance(kind, sa.Boolean):
            return False
        if isinstance(kind, sa.Integer | sa.BigInteger):
            return 1
        if isinstance(kind, sa.Float):
            return 0.5
        if isinstance(kind, sa.Numeric):
            return Decimal("0")
        if isinstance(kind, sa.DateTime):
            return datetime.now(UTC) + timedelta(days=1)
        if isinstance(kind, JSONB):
            return {}
        if isinstance(kind, ARRAY):
            return []
        return self._unique(column.name)

    async def create(self, table_name: str, org_id: UUID | None, **values: Any) -> dict[str, Any]:
        """Inserts a row (and any required parents) and returns its column values."""
        table = self.tables[table_name]
        row: dict[str, Any] = dict(OVERRIDES.get(table_name, {}))
        row.update(values)
        if "org_id" in table.c and "org_id" not in row:
            row["org_id"] = org_id
        # Required foreign keys first: composite tenant FKs and simple ones.
        for fk in table.foreign_key_constraints:
            local = [c.name for c in fk.columns]
            data_cols = [c for c in local if c != "org_id"]
            if not data_cols or any(c in row for c in data_cols):
                continue
            if all(table.c[c].nullable for c in data_cols):
                continue
            parent = fk.referred_table.name
            if parent == "organizations":
                continue
            parent_row = await self.create(parent, row.get("org_id") or org_id)
            for local_col, element in zip(fk.column_keys, fk.elements, strict=True):
                if local_col != "org_id":
                    row[local_col] = parent_row[element.column.name]
        allowed = self._allowed(table)
        for column in table.c:
            if column.name in row or column.nullable or column.server_default is not None:
                continue
            if column.primary_key and column.default is not None:
                continue
            row[column.name] = self._value(column, allowed)
        for column in table.c:
            if column.primary_key and column.name not in row and isinstance(column.type, sa.Uuid):
                row[column.name] = new_id()
        await self.session.execute(sa.insert(table).values(**row))
        return row
