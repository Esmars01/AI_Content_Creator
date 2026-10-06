"""Database rules (§29 conventions, §12.2, §12.3): append-only tables, frozen manifests, cache entries."""

from __future__ import annotations

from uuid import UUID

import pytest
import sqlalchemy as sa
from ce_core.errors import ImmutableRecordError
from ce_core.ids import new_id
from ce_db.errors import sqlstate, translate
from ce_db.rules import APPEND_ONLY_TABLES
from ce_db.session import Database
from ce_testing.rows import RowFactory
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.infra


async def new_org(db: Database) -> UUID:
    org = new_id()
    async with db.transaction() as session:
        await session.execute(sa.text("INSERT INTO organizations (id, name) VALUES (:id, 'test')"), {"id": org})
    return org


async def expect_sqlstate(db: Database, code: str, statement: str, params: dict[str, object]) -> None:
    with pytest.raises(DBAPIError) as info:
        async with db.transaction() as session:
            await session.execute(sa.text(statement), params)
    assert sqlstate(info.value) == code, str(info.value)


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_append_only_tables_reject_update_and_delete(db: Database, table: str) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        row = await RowFactory(session).create(table, org)
    pk_col = "id" if "id" in row else "version_id"
    params = {"pk": row[pk_col]}
    await expect_sqlstate(db, "CE002", f"UPDATE {table} SET updated_at = now() WHERE {pk_col} = :pk", params)
    await expect_sqlstate(db, "CE002", f"DELETE FROM {table} WHERE {pk_col} = :pk", params)


async def test_maintenance_mode_bypasses_append_only(db: Database) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        row = await RowFactory(session).create("audit_logs", org)
    async with db.transaction() as session:
        await session.execute(sa.text("SET LOCAL ce.maintenance = 'on'"))
        await session.execute(sa.text("DELETE FROM audit_logs WHERE id = :id"), {"id": row["id"]})
    async with db.session() as session:
        assert (
            await session.execute(sa.text("SELECT count(*) FROM audit_logs WHERE id = :id"), {"id": row["id"]})
        ).scalar() == 0


async def test_manifest_entries_are_insert_only_until_frozen(db: Database) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        factory = RowFactory(session)
        version = await factory.create("video_versions", org, state="ready", origin="plan")
        await factory.create("build_manifest_entries", org, version_id=version["id"], node_key="tts.segment:seg_1")
    # a second entry for another node is fine while the version is not frozen
    async with db.transaction() as session:
        await RowFactory(session).create(
            "build_manifest_entries", org, version_id=version["id"], node_key="audio.music:mc_1"
        )
    async with db.transaction() as session:
        await session.execute(
            sa.text("UPDATE video_versions SET frozen_at = now() WHERE id = :id"), {"id": version["id"]}
        )
    await expect_sqlstate(
        db,
        "CE001",
        "INSERT INTO build_manifest_entries (org_id, version_id, node_key) VALUES (:org, :v, 'render.final:p1')",
        {"org": org, "v": version["id"]},
    )


async def test_cache_entries_never_point_at_rejected_artifacts(db: Database) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        factory = RowFactory(session)
        good = await factory.create("artifacts", org, kind="video")
        bad = await factory.create("artifacts", org, kind="video", qc_state="qc_rejected")
        await factory.create("cache_entries", org, cache_key="key-good", artifact_id=good["id"])
    await expect_sqlstate(
        db,
        "CE003",
        "INSERT INTO cache_entries (org_id, cache_key, artifact_id) VALUES (:o, 'key-bad', :a)",
        {"o": org, "a": bad["id"]},
    )
    # rejecting an artifact later evicts its cache entries (§12.2)
    async with db.transaction() as session:
        await session.execute(
            sa.text("UPDATE artifacts SET qc_state = 'qc_rejected' WHERE id = :id"), {"id": good["id"]}
        )
    async with db.session() as session:
        remaining = await session.execute(sa.text("SELECT count(*) FROM cache_entries WHERE org_id = :o"), {"o": org})
        assert remaining.scalar() == 0


async def test_updated_at_is_touched_on_update(db: Database) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        project = await RowFactory(session).create("projects", org)
    async with db.session() as session:
        before = (
            await session.execute(sa.text("SELECT updated_at FROM projects WHERE id = :id"), {"id": project["id"]})
        ).scalar()
    async with db.transaction() as session:
        await session.execute(sa.text("UPDATE projects SET name = 'renamed' WHERE id = :id"), {"id": project["id"]})
    async with db.session() as session:
        after = (
            await session.execute(sa.text("SELECT updated_at FROM projects WHERE id = :id"), {"id": project["id"]})
        ).scalar()
    assert before is not None and after is not None and after > before


async def test_memory_snapshots_are_immutable(db: Database) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        snapshot = await RowFactory(session).create("memory_snapshots", org)
    await expect_sqlstate(
        db, "CE002", "UPDATE memory_snapshots SET digest = 'x' WHERE id = :id", {"id": snapshot["id"]}
    )


async def test_db_errors_translate_to_domain_errors(db: Database) -> None:
    org = await new_org(db)
    async with db.transaction() as session:
        row = await RowFactory(session).create("cost_ledger", org)
    with pytest.raises(DBAPIError) as info:
        async with db.transaction() as session:
            await session.execute(sa.text("DELETE FROM cost_ledger WHERE id = :id"), {"id": row["id"]})
    assert isinstance(translate(info.value), ImmutableRecordError)
