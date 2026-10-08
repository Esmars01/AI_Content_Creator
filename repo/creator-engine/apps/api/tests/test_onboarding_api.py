"""Uploaded media as identity, world and voice references (cutover §4).

An uploaded face or voice is accepted only with the attestation that it is not a real person (real
people need the digital-twin consent path, V1, which is off), an uploaded plate with the attestation
that it shows no identifiable people. Each upload must be usable (size, duration, sample rate). The
attestation is recorded on the asset, and approvals refuse an uploaded reference without it.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, upload_asset
from ce_db.models.assets import Asset
from ce_db.models.platform import AuditLog
from ce_testing.fixtures import alex_appearance_dna, alex_creator_dna
from ce_testing.placeholders import placeholder_png, placeholder_wav

pytestmark = pytest.mark.infra


async def _creator(tenant: ApiTenant) -> str:
    created = await tenant.client.post(
        "/v1/creators", json={"name": "U", "dna": alex_creator_dna().model_dump(mode="json")}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _rights(harness: ApiHarness, asset_id: str) -> dict[str, object]:
    async with harness.services.db.session() as session:
        return dict((await session.get_one(Asset, uuid.UUID(asset_id))).rights)


async def test_an_uploaded_face_needs_the_attestation_and_a_usable_size(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await _creator(owner)
    small = await upload_asset(harness, owner, placeholder_png("tiny", 64, 64), mime="image/png", kind="image")
    body = {
        "name": "Look",
        "dna": alex_appearance_dna().model_dump(mode="json"),
        "canonical_face_asset_id": small["id"],
    }
    refused = await owner.client.post(
        f"/v1/creators/{creator}/appearances", json=body | {"face_attestation": "not_a_real_person"}
    )
    assert refused.status_code == 422
    assert {i["code"] for i in refused.json()["issues"]} == {"upload_too_small"}

    face = await upload_asset(harness, owner, placeholder_png("face-ok", 512, 384), mime="image/png", kind="image")
    body["canonical_face_asset_id"] = face["id"]
    unattested = await owner.client.post(f"/v1/creators/{creator}/appearances", json=body)
    assert unattested.status_code == 422
    assert [i["code"] for i in unattested.json()["issues"]] == ["upload_attestation"]
    assert "digital-twin consent" in unattested.json()["issues"][0]["message"]

    created = await owner.client.post(
        f"/v1/creators/{creator}/appearances", json=body | {"face_attestation": "not_a_real_person"}
    )
    assert created.status_code == 201, created.text
    version = (await owner.client.get(f"/v1/appearance-versions/{created.json()['versions'][0]['id']}")).json()
    source = version["identity_pack"]["face_source"]
    assert source["attestation"] == "not_a_real_person" and source["attested_by"] == str(owner.user_id)
    rights = await _rights(harness, face["id"])
    assert rights["attestations"]["face"]["attestation"] == "not_a_real_person"  # type: ignore[index]

    # An approval refuses an uploaded face whose attestation is gone.
    async with harness.services.db.transaction() as session:
        await session.execute(
            sa.update(Asset).where(Asset.id == uuid.UUID(face["id"])).values(rights={"owner": str(owner.user_id)})
        )
    blocked = await owner.client.post(f"/v1/appearance-versions/{version['id']}:approve")
    assert blocked.status_code == 409
    assert "upload_attestation" in {i["code"] for i in blocked.json()["issues"]}


async def test_a_voice_from_an_upload(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await _creator(owner)
    short = await upload_asset(harness, owner, placeholder_wav(1.0), mime="audio/wav", kind="audio")
    body = {
        "name": "Imported narrator",
        "asset_id": short["id"],
        "language": "en-US",
        "transcript": "Here is the thing: it actually works.",
        "creator_id": creator,
        "attestation": "synthetic_voice_not_a_person",
    }
    refused = await owner.client.post("/v1/voices:from-upload", json=body)
    assert refused.status_code == 422 and {i["code"] for i in refused.json()["issues"]} == {"upload_duration"}
    low = await upload_asset(harness, owner, placeholder_wav(6.0, sample_rate=8_000), mime="audio/wav", kind="audio")
    refused = await owner.client.post("/v1/voices:from-upload", json=body | {"asset_id": low["id"]})
    assert refused.status_code == 422 and {i["code"] for i in refused.json()["issues"]} == {"upload_sample_rate"}

    clip = await upload_asset(harness, owner, placeholder_wav(6.0), mime="audio/wav", kind="audio")
    unattested = await owner.client.post(
        "/v1/voices:from-upload", json=body | {"asset_id": clip["id"], "attestation": None}
    )
    assert unattested.status_code == 422
    assert [i["code"] for i in unattested.json()["issues"]] == ["upload_attestation"]
    image = await upload_asset(harness, owner, placeholder_png("not-audio", 256, 256), mime="image/png", kind="image")
    wrong = await owner.client.post("/v1/voices:from-upload", json=body | {"asset_id": image["id"]})
    assert wrong.status_code == 422

    created = await owner.client.post("/v1/voices:from-upload", json=body | {"asset_id": clip["id"]})
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["status"] == "draft" and version["number"] == 1
    assert version["references"] == [
        {"asset_id": clip["id"], "language": "en-US", "transcript": "Here is the thing: it actually works."}
    ]
    voice = (await owner.client.get(f"/v1/voices/{version['voice_id']}")).json()
    assert voice["kind"] == "designed" and voice["creator_id"] == creator
    rights = await _rights(harness, clip["id"])
    assert rights["attestations"]["voice"]["attestation"] == "synthetic_voice_not_a_person"  # type: ignore[index]
    async with harness.services.db.session() as session:
        actions = (
            (await session.execute(sa.select(AuditLog.action).where(AuditLog.target_id == version["voice_id"])))
            .scalars()
            .all()
        )
    assert "voice.from_upload" in actions

    async with harness.services.db.transaction() as session:  # the approval re-checks the attestation
        await session.execute(
            sa.update(Asset).where(Asset.id == uuid.UUID(clip["id"])).values(rights={"owner": str(owner.user_id)})
        )
    blocked = await owner.client.post(f"/v1/voice-versions/{version['id']}:approve")
    assert blocked.status_code == 409 and blocked.json()["issues"][0]["code"] == "upload_attestation"


async def test_an_uploaded_plate_needs_the_attestation_and_a_usable_size(harness: ApiHarness, owner: ApiTenant) -> None:
    from ce_testing.fixtures import home_office_world

    dna = home_office_world()
    world = (await owner.client.post("/v1/worlds", json={"dna": dna.model_dump(mode="json")})).json()
    version = world["versions"][0]["id"]
    position = next(p.key for p in dna.camera_positions if p.status == "permitted")
    choice = {
        "camera_position_key": position,
        "time_of_day": dna.time_and_weather.default_time_of_day,
        "weather": dna.time_and_weather.default_weather,
    }
    small = await upload_asset(harness, owner, placeholder_png("plate-small", 320, 180), mime="image/png", kind="image")
    refused = await owner.client.post(
        f"/v1/world-versions/{version}/plates:choose",
        json=choice | {"asset_id": small["id"], "attestation": "no_identifiable_people_rights_held"},
    )
    assert refused.status_code == 422 and {i["code"] for i in refused.json()["issues"]} == {"upload_too_small"}
    plate = await upload_asset(harness, owner, placeholder_png("plate-ok", 1280, 720), mime="image/png", kind="image")
    unattested = await owner.client.post(
        f"/v1/world-versions/{version}/plates:choose", json=choice | {"asset_id": plate["id"]}
    )
    assert unattested.status_code == 422 and unattested.json()["issues"][0]["code"] == "upload_attestation"
    chosen = await owner.client.post(
        f"/v1/world-versions/{version}/plates:choose",
        json=choice | {"asset_id": plate["id"], "attestation": "no_identifiable_people_rights_held"},
    )
    assert chosen.status_code == 200, chosen.text
    rights = await _rights(harness, plate["id"])
    assert rights["attestations"]["plate"]["attested_by"] == str(owner.user_id)  # type: ignore[index]
