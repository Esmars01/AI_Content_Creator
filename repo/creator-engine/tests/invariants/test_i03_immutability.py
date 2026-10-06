"""I3 — Immutability (Phase 1 slice: identity rows; video versions' identity columns).

Approved versions of creators, appearances, voices, wardrobes, worlds and products are
immutable; only drafts change, and the only allowed change to an approved version is
archiving it. A video version's identity columns (§12.8) never change and versions are never
deleted. Enforced twice: in the repositories and by database triggers. The video-version API
paths arrive in Phase 6, which extends this test.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from ce_core.errors import ImmutableRecordError
from ce_core.ids import new_id
from ce_db.base import Base
from ce_db.errors import sqlstate
from ce_db.repository import OrgContext, TenantRepository
from ce_db.rules import VERSIONED_IDENTITY_TABLES
from ce_db.session import Database
from ce_testing.rows import RowFactory
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.infra, pytest.mark.invariant]

PAYLOAD_COLUMN = {
    "creator_versions": "dna",
    "appearance_versions": "dna",
    "voice_versions": "description",
    "wardrobe_versions": "spec",
    "world_versions": "dna",
    "product_versions": "description",
}
NEW_VALUE = {"dna": {"changed": True}, "spec": {"changed": True}, "description": "changed"}


def model_for(table: str) -> type:
    return next(m.class_ for m in Base.registry.mappers if m.class_.__tablename__ == table)


async def approved_row(db: Database, table: str) -> tuple[OrgContext, dict[str, object]]:
    ctx = OrgContext(org_id=new_id())
    async with db.transaction() as session:
        await session.execute(sa.text("INSERT INTO organizations (id, name) VALUES (:id, 't')"), {"id": ctx.org_id})
        row = await RowFactory(session).create(table, ctx.org_id, status="approved")
    return ctx, row


@pytest.mark.parametrize("table", VERSIONED_IDENTITY_TABLES)
async def test_repository_refuses_changes_to_approved_versions(db: Database, table: str) -> None:
    ctx, row = await approved_row(db, table)
    column = PAYLOAD_COLUMN[table]
    async with db.transaction() as session:
        repo: TenantRepository[Any] = TenantRepository(model_for(table), session, ctx)
        with pytest.raises(ImmutableRecordError):
            await repo.update(row["id"], **{column: NEW_VALUE[column]})
        with pytest.raises(ImmutableRecordError):
            await repo.delete(row["id"])
        archived = await repo.update(row["id"], status="archived")
        assert archived.status == "archived"  # type: ignore[attr-defined]


@pytest.mark.parametrize("table", VERSIONED_IDENTITY_TABLES)
async def test_trigger_refuses_changes_even_without_the_repository(db: Database, table: str) -> None:
    _, row = await approved_row(db, table)
    column = PAYLOAD_COLUMN[table]
    for statement, params in (
        (
            f"UPDATE {table} SET {column} = :v WHERE id = :id",
            {"v": '{"x": 1}' if column != "description" else "x", "id": row["id"]},
        ),
        (f"DELETE FROM {table} WHERE id = :id", {"id": row["id"]}),
        (f"UPDATE {table} SET status = 'draft' WHERE id = :id", {"id": row["id"]}),
    ):
        if column != "description" and "SET" in statement and ":v" in statement:
            statement = statement.replace(":v", "CAST(:v AS jsonb)")
        with pytest.raises(DBAPIError) as info:
            async with db.transaction() as session:
                await session.execute(sa.text(statement), params)
        assert sqlstate(info.value) == "CE001"
    # archiving is the one allowed change, and archived rows stay frozen
    async with db.transaction() as session:
        await session.execute(sa.text(f"UPDATE {table} SET status = 'archived' WHERE id = :id"), {"id": row["id"]})
    with pytest.raises(DBAPIError):
        async with db.transaction() as session:
            await session.execute(sa.text(f"UPDATE {table} SET status = 'approved' WHERE id = :id"), {"id": row["id"]})


@pytest.mark.parametrize("table", VERSIONED_IDENTITY_TABLES)
async def test_drafts_are_mutable(db: Database, table: str) -> None:
    ctx = OrgContext(org_id=new_id())
    async with db.transaction() as session:
        await session.execute(sa.text("INSERT INTO organizations (id, name) VALUES (:id, 't')"), {"id": ctx.org_id})
        row = await RowFactory(session).create(table, ctx.org_id, status="draft")
    column = PAYLOAD_COLUMN[table]
    async with db.transaction() as session:
        updated: Any = await TenantRepository(model_for(table), session, ctx).update(
            row["id"], **{column: NEW_VALUE[column]}
        )
        assert getattr(updated, column) == NEW_VALUE[column]
        approved: Any = await TenantRepository(model_for(table), session, ctx).update(row["id"], status="approved")
        assert approved.status == "approved"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("spec", "CAST('{\"changed\": true}' AS jsonb)"),
        ("spec_hash", "'sha256:changed'"),
        ("spec_content_digest", "'sha256:changed'"),
        ("origin", "'edit'"),
        ("planned_routes", "CAST('{\"x\": 1}' AS jsonb)"),
        ("parent_version_id", "uuidv7()"),
    ],
)
async def test_video_version_identity_columns_are_immutable(db: Database, column: str, value: str) -> None:
    """Erratum E1: the full §12.8 identity set, including spec_content_digest and planned_routes."""
    org = new_id()
    async with db.transaction() as session:
        await session.execute(sa.text("INSERT INTO organizations (id, name) VALUES (:id, 't')"), {"id": org})
        version = await RowFactory(session).create("video_versions", org, state="planned", origin="plan")
    with pytest.raises(DBAPIError) as info:
        async with db.transaction() as session:
            await session.execute(
                sa.text(f"UPDATE video_versions SET {column} = {value} WHERE id = :id"), {"id": version["id"]}
            )
    assert sqlstate(info.value) == "CE001"


async def test_video_version_status_columns_stay_mutable_and_versions_are_never_deleted(db: Database) -> None:
    org = new_id()
    async with db.transaction() as session:
        await session.execute(sa.text("INSERT INTO organizations (id, name) VALUES (:id, 't')"), {"id": org})
        version = await RowFactory(session).create("video_versions", org, state="planned", origin="plan")
    async with db.transaction() as session:
        await session.execute(
            sa.text(
                "UPDATE video_versions SET state = 'previz_running', flags = ARRAY['coverage_changed'], "
                "coverage_summary = CAST('{\"delivered\": 1}' AS jsonb), cost_actual_usd = 1.5 WHERE id = :id"
            ),
            {"id": version["id"]},
        )
    with pytest.raises(DBAPIError) as info:
        async with db.transaction() as session:
            await session.execute(sa.text("DELETE FROM video_versions WHERE id = :id"), {"id": version["id"]})
    assert sqlstate(info.value) == "CE001"
