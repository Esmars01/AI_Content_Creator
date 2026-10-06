"""Creators, appearances, wardrobes, voices (§30, §17): drafts editable, approved immutable (I3),
approvals verify recorded results only (Phase 1), and every approval is audited."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, upload_asset
from ce_db.models.creators import AppearanceVersion
from ce_db.models.platform import AuditLog
from ce_testing.fixtures import alex_appearance_dna, alex_creator_dna, grey_hoodie
from ce_testing.placeholders import placeholder_png

pytestmark = pytest.mark.infra


def creator_dna() -> dict[str, Any]:
    return alex_creator_dna().model_dump(mode="json")


async def make_creator(tenant: ApiTenant) -> dict[str, Any]:
    response = await tenant.client.post(
        "/v1/creators", json={"name": "Robin", "kind": "synthetic", "dna": creator_dna()}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def approved_appearance(harness: ApiHarness, tenant: ApiTenant, creator_id: str) -> str:
    """An appearance version approved through the API, with recorded results written as the
    identity-pack workflow will write them (Phase 2 mock, Phase 7 real)."""
    face = await upload_asset(harness, tenant, placeholder_png("face"), mime="image/png", kind="image")
    created = await tenant.client.post(
        f"/v1/creators/{creator_id}/appearances",
        json={
            "name": "Default",
            "dna": alex_appearance_dna().model_dump(mode="json"),
            "canonical_face_asset_id": face["id"],
        },
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["versions"][0]["id"]
    blocked = await tenant.client.post(f"/v1/appearance-versions/{version_id}:approve")
    assert blocked.status_code == 409 and blocked.json()["code"] == "approval_blocked"
    assert {i["code"] for i in blocked.json()["issues"]} == {"age_check_missing", "identity_pack"}
    async with harness.services.db.transaction() as session:
        await session.execute(
            sa.update(AppearanceVersion)
            .where(AppearanceVersion.id == uuid.UUID(version_id))
            .values(
                age_checks={"age_appearance": 31, "vlm_estimate": 33},
                identity_pack={"images": [{"asset_id": face["id"], "similarity": 0.91, "decision": "approved"}]},
            )
        )
    approved = await tenant.client.post(f"/v1/appearance-versions/{version_id}:approve")
    assert approved.status_code == 200, approved.text
    return version_id


async def seeded_voice_version(harness: ApiHarness, tenant: ApiTenant, creator_id: str) -> str:
    from ce_db.models.creators import Voice, VoiceVersion

    async with harness.services.db.transaction() as session:
        voice = Voice(org_id=tenant.org_id, creator_id=uuid.UUID(creator_id), name="Robin voice", kind="designed")
        session.add(voice)
        await session.flush()
        version = VoiceVersion(org_id=tenant.org_id, voice_id=voice.id, number=1, status="approved", wpm={"en": 150})
        session.add(version)
        await session.flush()
        return str(version.id)


async def test_creator_lifecycle(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await make_creator(owner)
    draft = creator["versions"][0]
    assert (draft["number"], draft["status"]) == (1, "draft") and creator["current_version_id"] is None
    # drafts are editable
    dna = creator_dna()
    dna["identity"]["bio"] = "Explains things."
    patched = await owner.client.patch(f"/v1/creator-versions/{draft['id']}", json={"dna": dna})
    assert patched.status_code == 200 and patched.json()["dna"]["identity"]["bio"] == "Explains things."
    # approval needs approved appearance and voice versions and the adult attestation
    blocked = await owner.client.post(
        f"/v1/creator-versions/{draft['id']}:approve", json={"attest_adult_presentation": False}
    )
    assert blocked.status_code == 409
    assert {i["code"] for i in blocked.json()["issues"]} == {"reference_missing", "age_attestation"}
    appearance = await approved_appearance(harness, owner, creator["id"])
    voice = await seeded_voice_version(harness, owner, creator["id"])
    linked = await owner.client.patch(
        f"/v1/creator-versions/{draft['id']}", json={"appearance_version_id": appearance, "voice_version_id": voice}
    )
    assert linked.status_code == 200, linked.text
    approved = await owner.client.post(
        f"/v1/creator-versions/{draft['id']}:approve", json={"attest_adult_presentation": True}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved" and approved.json()["approved_at"]
    detail = (await owner.client.get(f"/v1/creators/{creator['id']}")).json()
    assert detail["current_version_id"] == draft["id"] and detail["current_version"]["status"] == "approved"
    # approved versions are immutable (I3): the API refuses before the trigger has to
    refused = await owner.client.patch(f"/v1/creator-versions/{draft['id']}", json={"dna": creator_dna()})
    assert refused.status_code == 409 and refused.json()["code"] == "immutable"
    again = await owner.client.post(
        f"/v1/creator-versions/{draft['id']}:approve", json={"attest_adult_presentation": True}
    )
    assert again.json()["code"] == "immutable"
    # a new draft derives from the current version and keeps its references
    v2 = await owner.client.post(f"/v1/creators/{creator['id']}/versions", json={})
    assert v2.status_code == 201
    assert (v2.json()["number"], v2.json()["parent_version_id"], v2.json()["appearance_version_id"]) == (
        2,
        draft["id"],
        appearance,
    )
    async with harness.services.db.session() as session:
        audit_row = (
            await session.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == "creator_version.approve", AuditLog.target_id == draft["id"]
                )
            )
        ).scalar_one()
    assert audit_row.after is not None
    assert audit_row.after["attest_adult_presentation"] is True and audit_row.after["age_checks"]["vlm_estimate"] == 33


async def test_creator_dna_is_validated_against_the_vocabulary(owner: ApiTenant) -> None:
    dna = creator_dna()
    dna["camera"]["framing_preference"] = "extreme_dutch_angle"
    response = await owner.client.post("/v1/creators", json={"name": "X", "dna": dna})
    assert response.status_code == 422
    assert any(i["path"] == "/camera/framing_preference" for i in response.json()["issues"])
    unknown = await owner.client.post("/v1/creators", json={"name": "X", "dna": {**creator_dna(), "mood": "sunny"}})
    assert unknown.status_code == 422  # extra fields are refused


async def test_digital_twins_are_disabled(owner: ApiTenant) -> None:
    response = await owner.client.post(
        "/v1/creators", json={"name": "Twin", "kind": "digital_twin", "dna": creator_dna()}
    )
    assert response.status_code == 403 and response.json()["code"] == "policy_denied"


async def test_references_must_belong_to_the_creator(harness: ApiHarness, owner: ApiTenant) -> None:
    first = await make_creator(owner)
    second = await make_creator(owner)
    appearance = await approved_appearance(harness, owner, first["id"])
    response = await owner.client.patch(
        f"/v1/creator-versions/{second['versions'][0]['id']}", json={"appearance_version_id": appearance}
    )
    assert response.status_code == 422 and response.json()["issues"][0]["path"] == "/appearance_version_id"


async def test_low_age_estimates_block_appearance_approval(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await make_creator(owner)
    face = await upload_asset(harness, owner, placeholder_png("young"), mime="image/png", kind="image")
    created = (
        await owner.client.post(
            f"/v1/creators/{creator['id']}/appearances",
            json={
                "name": "Look",
                "dna": alex_appearance_dna().model_dump(mode="json"),
                "canonical_face_asset_id": face["id"],
            },
        )
    ).json()
    version_id = created["versions"][0]["id"]
    async with harness.services.db.transaction() as session:
        await session.execute(
            sa.update(AppearanceVersion)
            .where(AppearanceVersion.id == uuid.UUID(version_id))
            .values(
                age_checks={"vlm_estimate": 21},
                identity_pack={"images": [{"asset_id": face["id"], "similarity": 0.9, "decision": "approved"}]},
            )
        )
    blocked = await owner.client.post(f"/v1/appearance-versions/{version_id}:approve")
    assert [i["code"] for i in blocked.json()["issues"]] == ["age_check_review"]
    young = alex_appearance_dna().model_dump(mode="json") | {"age_appearance": 17}
    under = await owner.client.post(f"/v1/creators/{creator['id']}/appearances", json={"name": "Teen", "dna": young})
    assert under.status_code == 422  # age_appearance >= 18 is part of the model (§17.3)


async def test_wardrobe_versions(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await make_creator(owner)
    spec = grey_hoodie().model_dump(mode="json")
    created = await owner.client.post(
        f"/v1/creators/{creator['id']}/wardrobes", json={"name": "grey hoodie", "spec": spec}
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["versions"][0]["id"]
    blocked = await owner.client.post(f"/v1/wardrobe-versions/{version_id}:approve")
    assert blocked.status_code == 409 and blocked.json()["issues"][0]["code"] == "wardrobe_references"
    reference = await upload_asset(harness, owner, placeholder_png("hoodie"), mime="image/png", kind="image")
    spec["reference_asset_ids"] = [reference["id"]]
    patched = await owner.client.patch(f"/v1/wardrobe-versions/{version_id}", json={"spec": spec})
    assert patched.status_code == 200 and patched.json()["reference_asset_ids"] == [reference["id"]]
    assert (await owner.client.post(f"/v1/wardrobe-versions/{version_id}:approve")).status_code == 200
    assert (await owner.client.patch(f"/v1/wardrobe-versions/{version_id}", json={"spec": spec})).status_code == 409
    v2 = await owner.client.post(f"/v1/wardrobes/{created.json()['id']}/versions", json={})
    assert (v2.status_code, v2.json()["number"], v2.json()["status"]) == (201, 2, "draft")
    listed = (await owner.client.get(f"/v1/creators/{creator['id']}/wardrobes")).json()
    assert [w["current_version_id"] for w in listed] == [version_id]
    bad = {**spec, "reference_asset_ids": [str(uuid.uuid4())]}
    assert (await owner.client.patch(f"/v1/wardrobe-versions/{v2.json()['id']}", json={"spec": bad})).status_code == 422


async def test_voices_are_readable(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await make_creator(owner)
    version_id = await seeded_voice_version(harness, owner, creator["id"])
    voices = (await owner.client.get("/v1/voices", params={"creator_id": creator["id"]})).json()
    assert len(voices) == 1
    detail = (await owner.client.get(f"/v1/voices/{voices[0]['id']}")).json()
    assert detail["versions"][0]["id"] == version_id
    assert (await owner.client.get(f"/v1/voice-versions/{version_id}")).json()["wpm"] == {"en": 150}


async def test_viewers_cannot_approve(harness: ApiHarness, owner: ApiTenant) -> None:
    creator = await make_creator(owner)
    viewer = await harness.add_member(owner.org_id, "viewer")
    response = await viewer.client.post(
        f"/v1/creator-versions/{creator['versions'][0]['id']}:approve", json={"attest_adult_presentation": True}
    )
    assert response.status_code == 403
