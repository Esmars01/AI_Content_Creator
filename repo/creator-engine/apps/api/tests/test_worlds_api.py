"""Worlds (§19, §30, §37 "World versioning"): World DNA validation, drafts editable, approved
immutable, plate choices, approval checks, promotion of overrides as a new version, diffs."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, upload_asset
from ce_db.models.assets import Artifact
from ce_db.models.worlds import WorldVersion
from ce_testing.fixtures import home_office_world
from ce_testing.placeholders import placeholder_png

pytestmark = pytest.mark.infra


def world_dna() -> dict[str, Any]:
    return home_office_world().model_dump(mode="json")


async def make_world(tenant: ApiTenant) -> dict[str, Any]:
    response = await tenant.client.post("/v1/worlds", json={"dna": world_dna()})
    assert response.status_code == 201, response.text
    return response.json()


async def record_fingerprints(harness: ApiHarness, tenant: ApiTenant, version_id: str) -> None:
    """What BuildWorldPlatesWorkflow records after plates are chosen (World Studio, Phase 10)."""
    async with harness.services.db.transaction() as session:
        artifact = Artifact(
            org_id=tenant.org_id,
            kind="world_fingerprints",
            storage_key=f"sha256/00/00/{uuid.uuid4().hex * 2}",
            mime="application/json",
            bytes=2,
            sha256="0" * 64,
        )
        session.add(artifact)
        await session.flush()
        await session.execute(
            sa.update(WorldVersion)
            .where(WorldVersion.id == uuid.UUID(version_id))
            .values(fingerprints_artifact_id=artifact.id)
        )


async def choose_default_plates(harness: ApiHarness, tenant: ApiTenant, version_id: str) -> None:
    dna = home_office_world()
    tod, weather = dna.time_and_weather.default_time_of_day, dna.time_and_weather.default_weather
    for position in dna.camera_positions:
        if position.status != "permitted":
            continue
        plate = await upload_asset(
            harness, tenant, placeholder_png(position.key, 64, 36), mime="image/png", kind="image"
        )
        chosen = await tenant.client.post(
            f"/v1/world-versions/{version_id}/plates:choose",
            json={"camera_position_key": position.key, "time_of_day": tod, "weather": weather, "asset_id": plate["id"]},
        )
        assert chosen.status_code == 200, chosen.text


async def test_world_versioning(harness: ApiHarness, owner: ApiTenant) -> None:
    world = await make_world(owner)
    assert (world["name"], world["kind"], world["current_version_id"]) == ("Alex's home office", "home_office", None)
    v1 = world["versions"][0]["id"]
    # drafts are editable (merge patch)
    patched = await owner.client.patch(f"/v1/world-versions/{v1}", json={"patch": {"style_tags": ["cozy", "warm"]}})
    assert patched.status_code == 200 and patched.json()["dna"]["style_tags"] == ["cozy", "warm"]
    # approval checks recorded results: plates for each permitted position at the default time/weather, fingerprints
    blocked = await owner.client.post(f"/v1/world-versions/{v1}:approve")
    assert blocked.status_code == 409
    codes = sorted({i["code"] for i in blocked.json()["issues"]})
    assert codes == ["fingerprints_missing", "plate_missing"]
    await choose_default_plates(harness, owner, v1)
    still = await owner.client.post(f"/v1/world-versions/{v1}:approve")
    assert [i["code"] for i in still.json()["issues"]] == ["fingerprints_missing"]
    await record_fingerprints(harness, owner, v1)
    approved = await owner.client.post(f"/v1/world-versions/{v1}:approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved" and approved.json()["approved_at"]
    assert (await owner.client.get(f"/v1/worlds/{world['id']}")).json()["current_version_id"] == v1
    # approved versions are immutable (I3)
    for attempt in (
        owner.client.patch(f"/v1/world-versions/{v1}", json={"patch": {"style_tags": []}}),
        owner.client.post(
            f"/v1/world-versions/{v1}/plates:choose",
            json={
                "camera_position_key": "cam_desk_front",
                "time_of_day": "late_afternoon",
                "weather": "clear",
                "asset_id": str(uuid.uuid4()),
            },
        ),
    ):
        response = await attempt
        assert response.status_code == 409 and response.json()["code"] == "immutable"
    # promoting a scene override creates a new draft version from the approved one (§19.3)
    promoted = await owner.client.post(
        f"/v1/worlds/{world['id']}/versions", json={"patch": {"time_and_weather": {"default_time_of_day": "evening"}}}
    )
    assert promoted.status_code == 201, promoted.text
    v2 = promoted.json()
    assert (v2["number"], v2["parent_version_id"], v2["status"]) == (2, v1, "draft")
    assert v2["dna"]["time_and_weather"]["default_time_of_day"] == "evening"
    assert v2["plates"] == approved.json()["plates"] and v2["fingerprints_artifact_id"] is None
    original = (await owner.client.get(f"/v1/world-versions/{v1}")).json()
    assert original["dna"]["time_and_weather"]["default_time_of_day"] == "late_afternoon"  # I6: never mutated
    diff = (await owner.client.get(f"/v1/world-versions/{v2['id']}/diff")).json()
    assert diff["against"] == v1
    assert {
        "op": "replace",
        "path": "/dna/time_and_weather/default_time_of_day",
        "old": "late_afternoon",
        "value": "evening",
    } in diff["changes"]
    # the evening default needs its own plates before v2 can be approved
    blocked_v2 = await owner.client.post(f"/v1/world-versions/{v2['id']}:approve")
    assert "plate_missing" in {i["code"] for i in blocked_v2.json()["issues"]}


async def test_world_dna_validation(owner: ApiTenant) -> None:
    dna = world_dna()
    dna["camera_positions"][0]["key"] = dna["elements"][0]["key"].replace("el_", "cam_")
    dna["background_layouts"] = {"cam_nowhere": {"visible_elements": [], "composition": "x"}}
    response = await owner.client.post("/v1/worlds", json={"dna": dna})
    assert response.status_code == 422
    unknown_kind = await owner.client.post("/v1/worlds", json={"dna": {**world_dna(), "kind": "space_station"}})
    assert unknown_kind.status_code == 422 and unknown_kind.json()["issues"][0]["path"] == "/kind"
    mismatch = await owner.client.post("/v1/worlds", json={"dna": world_dna(), "kind": "kitchen"})
    assert mismatch.status_code == 422


async def test_plate_choices_are_validated(harness: ApiHarness, owner: ApiTenant) -> None:
    world = await make_world(owner)
    v1 = world["versions"][0]["id"]
    audio = await upload_asset(
        harness, owner, b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28, mime="audio/wav", kind="audio"
    )
    response = await owner.client.post(
        f"/v1/world-versions/{v1}/plates:choose",
        json={"camera_position_key": "cam_ceiling", "time_of_day": "dawn", "weather": "snow", "asset_id": audio["id"]},
    )
    assert response.status_code == 422
    codes = {i["code"] for i in response.json()["issues"]}
    assert {"camera_position", "time_of_day", "weather"} <= codes and codes & {"asset_not_ready", "asset_media_type"}


async def test_world_kind_cannot_change(owner: ApiTenant) -> None:
    world = await make_world(owner)
    v1 = world["versions"][0]["id"]
    response = await owner.client.patch(f"/v1/world-versions/{v1}", json={"patch": {"kind": "kitchen"}})
    assert response.status_code == 422
