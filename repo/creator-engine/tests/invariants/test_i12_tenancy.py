"""I12 — Tenant isolation: every tenant-owned row carries org_id; repositories require an org context.

For **every** tenant table of §29 (derived from the schema, not a hand-written list), a row
created in org A is invisible to org B through the repository: get, find, update and delete
all fail as "not found", while org A can read it. Cross-org references are rejected by the
composite (org_id, parent_id) foreign keys in the database itself. Phase 13 extends this
suite to the API layer for every table.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from ce_core.errors import NotFoundError, PermissionDeniedError
from ce_core.ids import new_id
from ce_db.base import Base
from ce_db.errors import sqlstate
from ce_db.repository import GlobalRepository, OrgContext, TenantRepository
from ce_db.session import Database
from ce_testing.rows import RowFactory
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.infra, pytest.mark.invariant]

import ce_db.models  # noqa: E402,F401 - registers every table

TENANT_TABLES = sorted(name for name, table in Base.metadata.tables.items() if "org_id" in table.c)
GLOBAL_TABLES = sorted(name for name, table in Base.metadata.tables.items() if "org_id" not in table.c)


def model_for(table: str) -> type:
    return next(m.class_ for m in Base.registry.mappers if m.class_.__tablename__ == table)


def pk_of(table: str, row: dict[str, Any]) -> Any:
    columns = [c.name for c in Base.metadata.tables[table].primary_key.columns]
    values = tuple(row[c] for c in columns)
    return values[0] if len(values) == 1 else values


async def two_orgs(db: Database) -> tuple[OrgContext, OrgContext]:
    a, b = OrgContext(org_id=new_id()), OrgContext(org_id=new_id())
    async with db.transaction() as session:
        for ctx in (a, b):
            await session.execute(sa.text("INSERT INTO organizations (id, name) VALUES (:id, 't')"), {"id": ctx.org_id})
    return a, b


def test_every_table_is_classified() -> None:
    assert len(TENANT_TABLES) + len(GLOBAL_TABLES) == 76  # §29 + fleet_costs (Phase 9)
    assert set(GLOBAL_TABLES) == {
        "organizations",
        "users",
        "plugins",
        "models",
        "model_benchmarks",
        "benchmark_pairs",
        "model_behavior_profiles",
        "gpu_providers",
        "gpu_workers",
        "worker_enrollment_tokens",
        "fleet_costs",
        "feature_flags",
    }


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_other_orgs_cannot_read_or_write(db: Database, table: str) -> None:
    a, b = await two_orgs(db)
    async with db.transaction() as session:
        row = await RowFactory(session).create(table, a.org_id)
    key = pk_of(table, row)
    model = model_for(table)
    async with db.session() as session:
        theirs: TenantRepository[Any] = TenantRepository(model, session, b)
        with pytest.raises(NotFoundError):
            await theirs.get(key)
        assert await theirs.find() == []
        with pytest.raises(NotFoundError):
            await theirs.update(key, updated_at=sa.func.now())
        with pytest.raises(NotFoundError):
            await theirs.delete(key)
        mine: TenantRepository[Any] = TenantRepository(model, session, a)
        assert await mine.get(key) is not None
        assert len(await mine.find()) >= 1


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_repository_never_creates_rows_in_another_org(db: Database, table: str) -> None:
    a, b = await two_orgs(db)
    model = model_for(table)
    async with db.session() as session:
        obj = model()
        obj.org_id = a.org_id  # type: ignore[attr-defined]
        with pytest.raises(PermissionDeniedError):
            await TenantRepository(model, session, b).add(obj)


CROSS_REFERENCES = sorted(
    (name, fk.referred_table.name, next(c.name for c in fk.columns if c.name != "org_id"))
    for name, table in Base.metadata.tables.items()
    for fk in table.foreign_key_constraints
    if "org_id" in [c.name for c in fk.columns]
    and fk.referred_table.name != "organizations"
    and len([c for c in fk.columns if c.name != "org_id"]) == 1
)


@pytest.mark.parametrize(
    ("table", "parent", "column"), CROSS_REFERENCES, ids=[f"{t}.{c}" for t, _, c in CROSS_REFERENCES]
)
async def test_database_rejects_references_to_another_orgs_rows(
    db: Database, table: str, parent: str, column: str
) -> None:
    a, b = await two_orgs(db)
    async with db.transaction() as session:
        foreign_parent = await RowFactory(session).create(parent, a.org_id)
    with pytest.raises(DBAPIError) as info:
        async with db.transaction() as session:
            await RowFactory(session).create(table, b.org_id, **{column: foreign_parent["id"]})
    assert sqlstate(info.value) == "23503", str(info.value)  # foreign_key_violation


@pytest.mark.parametrize("table", ["plugins", "gpu_providers", "feature_flags"])
async def test_global_tables_are_writable_only_by_platform_admins(db: Database, table: str) -> None:
    model = model_for(table)
    async with db.session() as session:
        with pytest.raises(PermissionDeniedError):
            await GlobalRepository(model, session, OrgContext(org_id=new_id())).add(model())
