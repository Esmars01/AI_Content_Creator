"""Migrations up and down (§40 Phase 1 DoD), and no drift between models and the migrated schema."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from ce_db.rules import APPEND_ONLY_TABLES, VERSIONED_IDENTITY_TABLES
from ce_testing.database import TestDatabase, admin_dsn

pytestmark = pytest.mark.infra


async def _scalar(database: str, query: str) -> int:
    conn = await asyncpg.connect(admin_dsn(database))
    try:
        return int(await conn.fetchval(query))
    finally:
        await conn.close()


def test_upgrade_downgrade_upgrade_and_no_drift() -> None:
    database = TestDatabase("ce_migtest")
    asyncio.run(database.create(migrate=False))
    try:
        tables = "select count(*) from information_schema.tables where table_schema = 'public'"
        database.alembic("upgrade", "head")
        assert asyncio.run(_scalar(database.name, tables)) == 77  # 75 §29 tables + fleet_costs (0002) + alembic_version
        triggers = asyncio.run(_scalar(database.name, "select count(*) from pg_trigger where tgname like 'ce\\_%'"))
        assert triggers >= len(APPEND_ONLY_TABLES) + len(VERSIONED_IDENTITY_TABLES) + 4
        assert asyncio.run(_scalar(database.name, "select count(*) from pg_extension where extname = 'vector'")) == 1
        assert "No new upgrade operations detected" in database.alembic("check")
        database.alembic("downgrade", "0001")
        assert asyncio.run(_scalar(database.name, tables)) == 76  # 0002 removed cleanly
        database.alembic("upgrade", "head")
        database.alembic("downgrade", "base")
        assert asyncio.run(_scalar(database.name, tables)) == 1
        assert asyncio.run(_scalar(database.name, "select count(*) from pg_proc where proname like 'ce\\_%'")) == 0
        database.alembic("upgrade", "head")
        assert asyncio.run(_scalar(database.name, tables)) == 77
    finally:
        asyncio.run(database.drop())


def test_uuidv7_is_the_server_default(migrated_db: TestDatabase) -> None:
    value = asyncio.run(_scalar(migrated_db.name, "select uuid_extract_version(uuidv7())"))
    assert value == 7
