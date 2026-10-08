"""I3 at the API layer: every endpoint that changes a versioned identity record refuses approved
versions with `409 immutable`, and the stored row is unchanged. Approval itself goes through the
API; the recorded results it verifies are written as the Phase 2+ workflows will write them."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services, upload_asset
from ce_db.models.assets import Artifact
from ce_db.models.creators import AppearanceVersion, Voice, VoiceVersion
from ce_db.models.worlds import WorldVersion
from ce_testing.database import TestDatabase
from ce_testing.fixtures import alex_appearance_dna, alex_creator_dna, grey_hoodie, home_office_world
from ce_testing.placeholders import placeholder_png

pytestmark = [pytest.mark.infra, pytest.mark.invariant]


@pytest_asyncio.fixture
async def harness(migrated_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(migrated_db.url, storage="local_fs", storage_root=tmp_path / "storage")
    await services.storage.ensure_bucket(services.settings.s3_bucket_assets)
    h = ApiHarness(services)
    yield h
    await h.aclose()


async def approved_everything(h: ApiHarness, t: ApiTenant) -> dict[str, Any]:
    c = t.client
    image = await upload_asset(h, t, placeholder_png("i3", 640, 360), mime="image/png", kind="image")
    creator = (
        await c.post("/v1/creators", json={"name": "I3", "dna": alex_creator_dna().model_dump(mode="json")})
    ).json()
    appearance = (
        await c.post(
            f"/v1/creators/{creator['id']}/appearances",
            json={
                "name": "I3",
                "dna": alex_appearance_dna().model_dump(mode="json"),
                "canonical_face_asset_id": image["id"],
                "face_attestation": "not_a_real_person",
            },
        )
    ).json()
    appearance_version = appearance["versions"][0]["id"]
    spec = grey_hoodie().model_dump(mode="json") | {"reference_asset_ids": [image["id"]]}
    wardrobe = (await c.post(f"/v1/creators/{creator['id']}/wardrobes", json={"name": "I3", "spec": spec})).json()
    world = (await c.post("/v1/worlds", json={"dna": home_office_world().model_dump(mode="json")})).json()
    world_version = world["versions"][0]["id"]
    dna = home_office_world()
    for position in dna.camera_positions:
        if position.status == "permitted":
            await c.post(
                f"/v1/world-versions/{world_version}/plates:choose",
                json={
                    "camera_position_key": position.key,
                    "time_of_day": dna.time_and_weather.default_time_of_day,
                    "weather": dna.time_and_weather.default_weather,
                    "asset_id": image["id"],
                    "attestation": "no_identifiable_people_rights_held",
                },
            )
    async with h.services.db.transaction() as session:  # recorded results (identity pack, age check, fingerprints)
        await session.execute(
            sa.update(AppearanceVersion)
            .where(AppearanceVersion.id == uuid.UUID(appearance_version))
            .values(
                age_checks={"vlm_estimate": 30},
                identity_pack={"images": [{"asset_id": image["id"], "similarity": 0.9, "decision": "approved"}]},
            )
        )
        artifact = Artifact(
            org_id=t.org_id,
            kind="world_fingerprints",
            storage_key="sha256/aa/bb/x",
            mime="application/json",
            bytes=1,
            sha256="0" * 64,
        )
        session.add(artifact)
        await session.flush()
        await session.execute(
            sa.update(WorldVersion)
            .where(WorldVersion.id == uuid.UUID(world_version))
            .values(fingerprints_artifact_id=artifact.id)
        )
        voice = Voice(org_id=t.org_id, creator_id=uuid.UUID(creator["id"]), name="I3", kind="designed")
        session.add(voice)
        await session.flush()
        voice_version = VoiceVersion(org_id=t.org_id, voice_id=voice.id, number=1, status="approved")
        session.add(voice_version)
        await session.flush()
        voice_version_id = str(voice_version.id)
    creator_version = creator["versions"][0]["id"]
    approvals = [
        (f"/v1/appearance-versions/{appearance_version}:approve", None),
        (f"/v1/wardrobe-versions/{wardrobe['versions'][0]['id']}:approve", None),
        (f"/v1/world-versions/{world_version}:approve", None),
    ]
    for path, body in approvals:
        response = await c.post(path, json=body)
        assert response.status_code == 200, (path, response.text)
    await c.patch(
        f"/v1/creator-versions/{creator_version}",
        json={"appearance_version_id": appearance_version, "voice_version_id": voice_version_id},
    )
    approved = await c.post(f"/v1/creator-versions/{creator_version}:approve", json={"attest_adult_presentation": True})
    assert approved.status_code == 200, approved.text
    return {
        "creator_version": creator_version,
        "appearance_version": appearance_version,
        "wardrobe_version": wardrobe["versions"][0]["id"],
        "world_version": world_version,
        "image": image["id"],
    }


async def test_approved_identity_versions_refuse_every_change(harness: ApiHarness) -> None:
    tenant = await harness.new_tenant()
    ids = await approved_everything(harness, tenant)
    c = tenant.client
    reads = {
        "creator_version": f"/v1/creator-versions/{ids['creator_version']}",
        "appearance_version": f"/v1/appearance-versions/{ids['appearance_version']}",
        "wardrobe_version": f"/v1/wardrobe-versions/{ids['wardrobe_version']}",
        "world_version": f"/v1/world-versions/{ids['world_version']}",
    }
    before = {k: (await c.get(path)).json() for k, path in reads.items()}
    assert {v["status"] for v in before.values()} == {"approved"}
    attempts = [
        ("PATCH", reads["creator_version"], {"dna": alex_creator_dna().model_dump(mode="json")}),
        ("POST", f"{reads['creator_version']}:approve", {"attest_adult_presentation": True}),
        ("PATCH", reads["appearance_version"], {"dna": alex_appearance_dna().model_dump(mode="json")}),
        ("POST", f"{reads['appearance_version']}:approve", None),
        ("PATCH", reads["wardrobe_version"], {"spec": grey_hoodie().model_dump(mode="json")}),
        ("POST", f"{reads['wardrobe_version']}:approve", None),
        ("PATCH", reads["world_version"], {"patch": {"style_tags": ["changed"]}}),
        (
            "POST",
            f"{reads['world_version']}/plates:choose",
            {
                "camera_position_key": "cam_desk_front",
                "time_of_day": "late_afternoon",
                "weather": "clear",
                "asset_id": ids["image"],
            },
        ),
        ("POST", f"{reads['world_version']}:approve", None),
    ]
    for method, path, body in attempts:
        response = await c.request(method, path, json=body)
        assert (response.status_code, response.json()["code"]) == (409, "immutable"), (method, path, response.text)
    after = {k: (await c.get(path)).json() for k, path in reads.items()}
    assert after == before
