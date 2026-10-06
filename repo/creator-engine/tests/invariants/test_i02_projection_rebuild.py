"""I2 — One creative source of truth: projection tables are rebuilt from the spec and manifest.

The scenes, scene_cast and shots rows of a version (and the `selected` flag of its takes) are
derived from `video_versions.spec`. Editing them directly is meaningless: a rebuild restores
exactly what the spec says, and rebuilding twice gives identical rows.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from ce_core.spec.videospec import VideoSpec
from ce_db.projections import projection_rows, rebuild_projections
from ce_db.repository import OrgContext
from ce_db.session import Database
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, example_spec
from ce_testing.seed import example_version_row

pytestmark = [pytest.mark.infra, pytest.mark.invariant]

CTX = OrgContext(org_id=ALEX.ORG_ID)


async def snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    out: dict[str, list[tuple[object, ...]]] = {}
    queries = {
        "scenes": 'SELECT scene_key, "order", purpose, world_version_id, camera_position_key, time_of_day, weather '
        "FROM scenes WHERE version_id = :v ORDER BY scene_key",
        "scene_cast": "SELECT scene_key, character_key, creator_version_id, wardrobe_version_id FROM scene_cast "
        "WHERE version_id = :v ORDER BY scene_key, character_key",
        "shots": "SELECT scene_key, shot_key, type, layer, selected_take_key FROM shots "
        "WHERE version_id = :v ORDER BY shot_key",
        "takes": "SELECT shot_key, take_key, selected FROM takes WHERE version_id = :v ORDER BY shot_key, take_key",
    }
    async with db.session() as session:
        for name, query in queries.items():
            out[name] = [tuple(r) for r in (await session.execute(sa.text(query), {"v": ALEX.VERSION_ID})).all()]
    return out


@pytest.fixture
async def version(seeded_db: TestDatabase, db: Database) -> None:
    async with db.transaction() as session:
        exists = await session.execute(sa.text("SELECT 1 FROM video_versions WHERE id = :id"), {"id": ALEX.VERSION_ID})
        if exists.scalar() is None:
            await session.execute(
                sa.text("INSERT INTO videos (id, org_id, project_id) VALUES (:id, :org, :project)"),
                {"id": ALEX.VIDEO_ID, "org": ALEX.ORG_ID, "project": ALEX.PROJECT_ID},
            )
            from ce_db.models.videos import VideoVersion

            await session.execute(sa.insert(VideoVersion), [example_version_row()])
            for key, index in (("tk_1", 1), ("tk_2", 2)):
                await session.execute(
                    sa.text(
                        "INSERT INTO takes (org_id, version_id, shot_key, take_key, take_index) "
                        "VALUES (:o, :v, 'sht_1', :k, :i)"
                    ),
                    {"o": ALEX.ORG_ID, "v": ALEX.VERSION_ID, "k": key, "i": index},
                )


async def test_rebuild_derives_projections_from_the_spec(db: Database, version: None) -> None:
    async with db.transaction() as session:
        await rebuild_projections(session, CTX, ALEX.VERSION_ID)
    rows = await snapshot(db)
    spec = example_spec()
    expected = projection_rows(spec)
    assert [r[0] for r in rows["scenes"]] == [s["scene_key"] for s in expected["scenes"]]
    assert rows["scenes"][0][2] == "hook" and rows["scenes"][0][4] == "cam_desk_front"
    assert rows["shots"] == [
        ("scn_hook", "sht_1", "talking_head", "base", None),
        ("scn_hook", "sht_2", "broll", "overlay", None),
    ]
    assert rows["scene_cast"] == [("scn_hook", "char_alex", ALEX.CREATOR_VERSION_ID, ALEX.WARDROBE_VERSION_ID)]
    assert rows["takes"] == [("sht_1", "tk_1", False), ("sht_1", "tk_2", False)]


async def test_edited_projections_are_restored_and_rebuild_is_idempotent(db: Database, version: None) -> None:
    async with db.transaction() as session:
        await rebuild_projections(session, CTX, ALEX.VERSION_ID)
    expected = await snapshot(db)
    async with db.transaction() as session:
        await session.execute(
            sa.text("UPDATE scenes SET purpose = 'outro' WHERE version_id = :v"), {"v": ALEX.VERSION_ID}
        )
        await session.execute(
            sa.text("DELETE FROM shots WHERE shot_key = 'sht_2' AND version_id = :v"), {"v": ALEX.VERSION_ID}
        )
        await session.execute(
            sa.text("UPDATE takes SET selected = true WHERE take_key = 'tk_2' AND version_id = :v"),
            {"v": ALEX.VERSION_ID},
        )
    assert await snapshot(db) != expected
    for _ in range(2):
        async with db.transaction() as session:
            await rebuild_projections(session, CTX, ALEX.VERSION_ID)
        assert await snapshot(db) == expected


def test_projection_follows_take_selection_in_the_spec() -> None:
    data = example_spec().model_dump(mode="json")
    data["scenes"][0]["shots"][0]["takes"]["selected_take_key"] = "tk_2"
    rows = projection_rows(VideoSpec.model_validate(data))
    assert rows["selected_takes"][0]["sht_1"] == "tk_2"
