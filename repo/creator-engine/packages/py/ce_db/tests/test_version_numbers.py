"""Version numbers under concurrency (audit A4): two jobs adding a version to the same video."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from ce_db.models.videos import Project, Video, VideoVersion
from ce_db.session import Database
from ce_db.versions import next_version_number
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX
from ce_testing.seed import example_version_row

pytestmark = pytest.mark.infra


async def test_concurrent_new_versions_of_one_video_get_distinct_numbers(seeded_db: TestDatabase) -> None:
    """Planning, a replan and an edit apply run in different jobs; MAX+1 without the video row's lock
    gave two of them the same number and one failed on the unique (video_id, number)."""
    db = Database(seeded_db.url, pool_size=4)
    video_id = uuid.uuid4()
    async with db.transaction() as session:  # a project of its own: the seeded one stays as seeded
        project = Project(org_id=ALEX.ORG_ID, name=f"version numbers {video_id.hex[:6]}")
        session.add(project)
        await session.flush()
        session.add(Video(id=video_id, org_id=ALEX.ORG_ID, project_id=project.id))

    async def add_version() -> int:
        async with db.transaction() as session:
            number = await next_version_number(session, ALEX.ORG_ID, video_id)
            await asyncio.sleep(0.3)  # the window in which the other job used to read the same MAX
            row = {**example_version_row(), "id": uuid.uuid4(), "video_id": video_id, "number": number}
            session.add(VideoVersion(**row))
        return number

    numbers = await asyncio.gather(add_version(), add_version())
    assert sorted(numbers) == [1, 2]
    await db.dispose()
