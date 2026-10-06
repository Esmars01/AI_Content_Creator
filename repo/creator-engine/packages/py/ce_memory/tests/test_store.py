"""Memory store against Postgres: records, retrieval into an immutable snapshot (I7), and the usage
log the repetition guard reads (§18.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from ce_config.loader import load_config
from ce_core.ids import new_id
from ce_db.models.videos import VideoVersion
from ce_db.session import Database
from ce_memory import MemoryRetriever, RetrievalContext
from ce_memory.store import create_snapshot, load_records, recent_usage, version_numbers, write_usage_events
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, example_spec
from ce_testing.seed import example_version_row
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.infra

NOW = datetime(2026, 10, 1, tzinfo=UTC)


async def _video(db: Database) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh video with one planned version, in its own project (the seeded project's video list
    is asserted elsewhere)."""
    project_id, video_id, version_id = new_id(), new_id(), new_id()
    row = example_version_row()
    spec = example_spec().model_copy(update={"video_id": video_id, "version_id": version_id})
    row.update(id=version_id, video_id=video_id, spec=spec.model_dump(mode="json"))
    async with db.transaction() as session:
        await session.execute(
            sa.text("INSERT INTO projects (id, org_id, name) VALUES (:id, :org, 'memory store tests')"),
            {"id": project_id, "org": ALEX.ORG_ID},
        )
        await session.execute(
            sa.text("INSERT INTO videos (id, org_id, project_id) VALUES (:id, :org, :project)"),
            {"id": video_id, "org": ALEX.ORG_ID, "project": project_id},
        )
        await session.execute(sa.insert(VideoVersion), [row])
    return video_id, version_id


async def test_records_and_version_numbers_load_for_the_creator(seeded_db: TestDatabase, db: Database) -> None:
    async with db.session() as session:
        records = await load_records(session, ALEX.ORG_ID, ALEX.CREATOR_ID)
        numbers = await version_numbers(session, ALEX.ORG_ID, ALEX.CREATOR_ID)
        other_org = await load_records(session, uuid.uuid4(), ALEX.CREATOR_ID)
    by_id = {r.id: r for r in records}
    assert {ALEX.MEMORY_GAZE_ID, ALEX.MEMORY_LAUGH_ID, ALEX.MEMORY_PHRASE_ID, ALEX.MEMORY_FACT_ID} <= set(by_id)
    fact = by_id[ALEX.MEMORY_FACT_ID]
    assert fact.pinned and fact.status == "active" and fact.source_type == "authored"
    assert fact.value["object"] == "Austin"
    assert numbers[ALEX.CREATOR_VERSION_ID] >= 1
    assert other_org == []  # org-scoped (I12)


async def test_retrieval_snapshot_is_persisted_and_immutable(seeded_db: TestDatabase, db: Database) -> None:
    memory = load_config(Path(__file__).resolve().parents[4] / "config", "test").memory
    assert memory is not None
    config = memory.retrieval
    async with db.session() as session:
        records = await load_records(session, ALEX.ORG_ID, ALEX.CREATOR_ID)
        numbers = await version_numbers(session, ALEX.ORG_ID, ALEX.CREATOR_ID)
    draft = MemoryRetriever(config).retrieve(
        records,
        RetrievalContext(
            creator_version_id=ALEX.CREATOR_VERSION_ID, now=NOW, brief="the reveal", version_numbers=numbers
        ),
    )
    assert ALEX.MEMORY_FACT_ID in draft.item_ids  # pinned items always enter
    async with db.transaction() as session:
        snapshot_id = await create_snapshot(session, ALEX.ORG_ID, draft)
    async with db.session() as session:
        stored = (
            await session.execute(
                sa.text("SELECT digest, items FROM memory_snapshots WHERE id = :id"), {"id": snapshot_id}
            )
        ).one()
    assert stored[0] == draft.digest()
    assert {i["item_id"] for i in stored[1]} == {str(i) for i in draft.item_ids}
    with pytest.raises(DBAPIError):
        async with db.transaction() as session:
            await session.execute(
                sa.text("UPDATE memory_snapshots SET items = '[]'::jsonb WHERE id = :id"), {"id": snapshot_id}
            )


async def test_usage_events_are_idempotent_and_recent_usage_is_per_video(seeded_db: TestDatabase, db: Database) -> None:
    creators = {"char_alex": ALEX.CREATOR_ID}
    first_video, first_version = await _video(db)
    second_video, second_version = await _video(db)
    spec = example_spec()
    async with db.transaction() as session:
        assert (
            await write_usage_events(
                session, ALEX.ORG_ID, video_id=first_video, version_id=first_version, spec=spec, creators=creators
            )
            == 1
        )
        assert (
            await write_usage_events(
                session, ALEX.ORG_ID, video_id=first_video, version_id=first_version, spec=spec, creators=creators
            )
            == 0
        )  # the same (version, character, event) is written once
        assert (
            await write_usage_events(
                session, ALEX.ORG_ID, video_id=second_video, version_id=second_version, spec=spec, creators=creators
            )
            == 1
        )
        assert (
            await write_usage_events(
                session,
                ALEX.ORG_ID,
                video_id=second_video,
                version_id=second_version,
                spec=spec,
                creators=creators,
                event="exported",
            )
            == 1
        )
    async with db.session() as session:
        usage = await recent_usage(session, ALEX.ORG_ID, ALEX.CREATOR_ID)
        without_second = await recent_usage(session, ALEX.ORG_ID, ALEX.CREATOR_ID, exclude_video_id=second_video)
    videos = [u.video_id for u in usage]
    assert len(videos) == len(set(videos))  # one entry per distinct video
    assert videos.index(second_video) < videos.index(first_video)  # newest first
    entry = usage[videos.index(first_video)]
    assert entry.hooks and entry.phrase_fingerprints and entry.arc
    assert second_video not in [u.video_id for u in without_second]
