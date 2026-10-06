"""I3 — Video versions are immutable (Phase 6 slice: the edit paths, §12.8).

Every way a version is derived — an edit, a regeneration, a re-route, a lock change, a take
selection, a restore, a branch, a duplicate — inserts a **new** version row and moves the video's
current-version pointer; the source row is never changed. Restoring an older version is a new
version with the same content, not a pointer move back. The trigger refuses identity changes of
derived versions like any other (`test_i03_immutability.py`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_core.canonical import content_digest
from ce_core.enums import VersionOrigin
from ce_core.ids import new_id
from ce_core.spec.videospec import VideoSpec
from ce_db.errors import sqlstate
from ce_db.models.assets import GenerationJob
from ce_db.models.videos import Project, Video, VideoVersion
from ce_db.session import Database
from ce_db.versions import insert_derived_version
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, two_scene_spec_dict
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.infra, pytest.mark.invariant]

IDENTITY = ("spec", "spec_hash", "spec_content_digest", "parent_version_id", "origin", "planned_routes", "number")


@pytest_asyncio.fixture
async def db(seeded_db: TestDatabase) -> AsyncIterator[Database]:
    database = Database(seeded_db.url, pool_size=2)
    yield database
    await database.dispose()


async def source(db: Database, state: str = "ready") -> VideoVersion:
    async with db.transaction() as session:
        project = Project(org_id=ALEX.ORG_ID, name="I3")  # its own project: other tests read the seeded one
        session.add(project)
        await session.flush()
        video = Video(org_id=ALEX.ORG_ID, project_id=project.id, title="I3", mode="explainer")
        session.add(video)
        await session.flush()
        version_id = new_id()
        model = VideoSpec.model_validate(
            two_scene_spec_dict() | {"video_id": str(video.id), "version_id": str(version_id)}
        )
        spec = model.model_dump(mode="json")
        row = VideoVersion(
            id=version_id,
            org_id=ALEX.ORG_ID,
            video_id=video.id,
            number=1,
            spec=spec,
            spec_hash=content_digest(spec),
            spec_content_digest=model.content_digest(),
            planned_routes={"tts.segment:seg_1": {"adapter_id": "mock_voice"}},
            state=state,
            origin="plan",
        )
        session.add(row)
        await session.flush()
        video.current_version_id = row.id
    return row


def columns(row: VideoVersion) -> dict[str, Any]:
    return {c: getattr(row, c) for c in (*IDENTITY, "state", "plan_report_artifact_id", "video_id")}


async def reload(db: Database, version_id: Any) -> VideoVersion:
    async with db.session() as session:
        row: VideoVersion = await session.get_one(VideoVersion, version_id)
        return row


@pytest.mark.parametrize("origin", [o for o in VersionOrigin if o not in (VersionOrigin.PLAN, VersionOrigin.REPLAN)])
async def test_every_derived_version_is_a_new_row_and_the_source_never_changes(
    db: Database, origin: VersionOrigin
) -> None:
    parent = await source(db)
    before = columns(await reload(db, parent.id))
    document = dict(parent.spec)
    if origin is VersionOrigin.EDIT:
        document = {**document, "meta": {**document["meta"], "title": "Edited"}}
    child_id = new_id()
    async with db.transaction() as session:
        target_video = None
        if origin is VersionOrigin.DUPLICATE:
            source_video = await session.get_one(Video, parent.video_id)
            copy = Video(org_id=ALEX.ORG_ID, project_id=source_video.project_id, title="copy")
            session.add(copy)
            await session.flush()
            target_video = copy.id
        child, job = await insert_derived_version(
            session,
            ALEX.ORG_ID,
            source_version_id=parent.id,
            new_version_id=child_id,
            document=document,
            origin=origin,
            requested_by=None,
            branch="alt" if origin is VersionOrigin.BRANCH else None,
            video_id=target_video,
        )
        assert job.kind == "generate"  # the source passed approval (it is ready)
    assert columns(await reload(db, parent.id)) == before  # untouched
    child = await reload(db, child_id)
    assert child.id != parent.id and child.origin == origin.value and child.parent_version_id == parent.id
    assert child.number == (1 if origin is VersionOrigin.DUPLICATE else 2)
    async with db.session() as session:
        video = await session.get_one(Video, child.video_id)
        assert video.current_version_id == child.id
    with pytest.raises(DBAPIError) as info:  # the derived version is just as immutable
        async with db.transaction() as session:
            await session.execute(
                sa.text("UPDATE video_versions SET spec = CAST('{}' AS jsonb) WHERE id = :id"), {"id": child.id}
            )
    assert sqlstate(info.value) == "CE001"


async def test_restore_is_a_new_version_with_the_old_content(db: Database) -> None:
    v1 = await source(db)
    async with db.transaction() as session:
        edited = {**v1.spec, "meta": {**v1.spec["meta"], "title": "Edited"}}
        v2, _ = await insert_derived_version(
            session,
            ALEX.ORG_ID,
            source_version_id=v1.id,
            new_version_id=new_id(),
            document=edited,
            origin=VersionOrigin.EDIT,
            requested_by=None,
        )
        v3, _ = await insert_derived_version(
            session,
            ALEX.ORG_ID,
            source_version_id=v1.id,
            new_version_id=new_id(),
            document=dict(v1.spec),
            origin=VersionOrigin.RESTORE,
            requested_by=None,
        )
    restored = await reload(db, v3.id)
    original = await reload(db, v1.id)
    assert [original.number, (await reload(db, v2.id)).number, restored.number] == [1, 2, 3]
    assert restored.id not in (v1.id, v2.id) and restored.parent_version_id == v1.id
    assert restored.spec_content_digest == original.spec_content_digest  # the same content …
    assert restored.spec["version_id"] == str(restored.id) != original.spec["version_id"]  # … in a new version
    async with db.session() as session:
        video = await session.get_one(Video, v1.video_id)
        assert video.current_version_id == v3.id
        count = (
            await session.execute(sa.select(sa.func.count()).where(VideoVersion.video_id == v1.video_id))
        ).scalar_one()
        assert count == 3  # nothing was deleted or overwritten


async def test_a_derived_version_of_an_unapproved_source_is_previzualized(db: Database) -> None:
    parent = await source(db, state="previz_ready")
    async with db.transaction() as session:
        child, job = await insert_derived_version(
            session,
            ALEX.ORG_ID,
            source_version_id=parent.id,
            new_version_id=new_id(),
            document=dict(parent.spec),
            origin=VersionOrigin.EDIT,
            requested_by=None,
        )
    assert (child.state, job.kind) == ("planned", "previz")  # approval applies to an up-to-date previz
    async with db.session() as session:
        stored = await session.get_one(GenerationJob, job.id)
    assert stored.temporal_workflow_id == f"previz-{job.id}"
