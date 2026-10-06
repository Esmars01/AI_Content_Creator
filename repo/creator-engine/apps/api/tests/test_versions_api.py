"""Videos and versions, read (§30): the spec comes back through `VideoSpec.to_api`."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness
from ce_db.models.videos import VideoVersion
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, example_spec
from ce_testing.seed import example_version_row

pytestmark = pytest.mark.infra


async def ensure_example_version(harness: ApiHarness) -> None:
    async with harness.services.db.transaction() as session:
        exists = await session.execute(sa.select(VideoVersion.id).where(VideoVersion.id == ALEX.VERSION_ID))
        if exists.scalar_one_or_none() is None:
            await session.execute(
                sa.text("INSERT INTO videos (id, org_id, project_id, title) VALUES (:id, :org, :project, 'Example')"),
                {"id": ALEX.VIDEO_ID, "org": ALEX.ORG_ID, "project": ALEX.PROJECT_ID},
            )
            await session.execute(sa.insert(VideoVersion), [example_version_row()])


async def test_read_videos_and_versions(harness: ApiHarness, seeded_db: TestDatabase) -> None:
    await ensure_example_version(harness)
    viewer = await harness.add_member(ALEX.ORG_ID, "viewer")
    videos = (await viewer.client.get(f"/v1/projects/{ALEX.PROJECT_ID}/videos")).json()["items"]
    assert [v["id"] for v in videos] == [str(ALEX.VIDEO_ID)]
    assert (await viewer.client.get(f"/v1/videos/{ALEX.VIDEO_ID}")).json()["project_id"] == str(ALEX.PROJECT_ID)
    versions = (await viewer.client.get(f"/v1/videos/{ALEX.VIDEO_ID}/versions")).json()["items"]
    assert [v["id"] for v in versions] == [str(ALEX.VERSION_ID)] and "spec" not in versions[0]
    version = (await viewer.client.get(f"/v1/versions/{ALEX.VERSION_ID}")).json()
    spec = example_spec()
    assert version["spec"]["script"]["wording_locked"] == spec.wording_locked
    assert version["spec_content_digest"] == spec.content_digest()
    assert version["spec"]["meta"] == spec.to_api()["meta"]
    assert version["claims_summary"] == {}
    snapshots = (await viewer.client.get(f"/v1/versions/{ALEX.VERSION_ID}/memory-snapshots")).json()
    assert [s["id"] for s in snapshots] == [str(ALEX.SNAPSHOT_ID)]
    seeded = (await viewer.client.get(f"/v1/creators/{ALEX.CREATOR_ID}/memory", params={"status": "active"})).json()[
        "items"
    ]
    assert len(seeded) == 4
    stranger = await harness.new_tenant()
    assert (await stranger.client.get(f"/v1/versions/{ALEX.VERSION_ID}")).status_code == 404
