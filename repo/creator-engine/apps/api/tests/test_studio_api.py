"""Studio endpoints (Phase 10, §30): each model action is a studio job with its context; choices and
reviews only change drafts (I3); invalid input is refused before any job starts. Workflow starts are
recorded, not run (the studio workflows run end to end in tests/e2e/test_studio_mock.py)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, upload_asset
from ce_db.models.assets import GenerationJob
from ce_db.models.creators import AppearanceVersion, VoiceVersion
from ce_testing.fixtures import alex_appearance_dna, alex_creator_dna, home_office_world
from ce_testing.placeholders import placeholder_png

pytestmark = pytest.mark.infra


def _record_starts(harness: ApiHarness) -> list[tuple[str, Any]]:
    started: list[tuple[str, Any]] = []

    async def start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        started.append((workflow, arg))

    harness.services.workflows.start = start  # type: ignore[method-assign]
    return started


async def _appearance(tenant: ApiTenant) -> tuple[str, str]:
    creator = (
        await tenant.client.post("/v1/creators", json={"name": "R", "dna": alex_creator_dna().model_dump(mode="json")})
    ).json()
    appearance = (
        await tenant.client.post(
            f"/v1/creators/{creator['id']}/appearances",
            json={"name": "A", "dna": alex_appearance_dna().model_dump(mode="json")},
        )
    ).json()
    return creator["id"], appearance["versions"][0]["id"]


async def test_identity_pack_jobs_and_review(harness: ApiHarness, owner: ApiTenant) -> None:
    face = await upload_asset(harness, owner, placeholder_png("face", 256, 256), mime="image/png", kind="image")
    started = _record_starts(harness)  # after the upload: its validation job must run
    _, look = await _appearance(owner)
    accepted = await owner.client.post(f"/v1/appearance-versions/{look}/identity-pack:generate", json={"candidates": 9})
    assert accepted.status_code == 202
    workflow, ctx = started[-1]
    assert workflow == "BuildIdentityPackWorkflow" and ctx["kind"] == "identity_pack"
    assert ctx["args"] == {"mode": "candidates", "candidates": 9} and ctx["target_id"] == look
    assert ctx["model_timeout_s"] > 0 and ctx["user_id"] == str(owner.user_id)
    async with harness.services.db.session() as session:
        job = await session.get_one(GenerationJob, uuid.UUID(accepted.json()["job_id"]))
    assert (job.kind, job.target_type, job.status) == ("identity_pack", "appearance_version", "queued")

    audio_like = await owner.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:choose", json={"asset_id": str(uuid.uuid4())}
    )
    assert audio_like.status_code == 422
    unattested = await owner.client.post(  # an upload: the uploader must attest it is not a real person
        f"/v1/appearance-versions/{look}/identity-pack:choose", json={"asset_id": face["id"]}
    )
    assert unattested.status_code == 422 and unattested.json()["issues"][0]["code"] == "upload_attestation"
    chosen = await owner.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:choose",
        json={"asset_id": face["id"], "attestation": "not_a_real_person"},
    )
    assert chosen.status_code == 202 and started[-1][1]["args"] == {"mode": "expand"}
    async with harness.services.db.transaction() as session:  # what the expansion stage records
        await session.execute(
            sa.update(AppearanceVersion).where(AppearanceVersion.id == uuid.UUID(look)).values(
                identity_pack={"images": [{"asset_id": face["id"], "similarity": 0.9, "decision": "pending"}]},
                age_checks={"vlm_estimate": 21.0},
            )
        )  # fmt: skip
    unknown = await owner.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:review", json={"approve": [str(uuid.uuid4())]}
    )
    assert unknown.status_code == 422
    reviewed = await owner.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:review",
        json={"approve": [face["id"]], "age_review": "approved"},
    )
    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["identity_pack"]["images"][0]["decision"] == "approved"
    assert body["age_checks"]["review"]["decision"] == "approved"
    assert (await owner.client.post(f"/v1/appearance-versions/{look}:approve")).status_code == 200
    # approved: no more studio changes (I3)
    assert (
        await owner.client.post(f"/v1/appearance-versions/{look}/identity-pack:generate", json={})
    ).status_code == 409


async def test_viewers_cannot_review_or_start_jobs(harness: ApiHarness, owner: ApiTenant) -> None:
    _record_starts(harness)
    _, look = await _appearance(owner)
    viewer = await harness.add_member(owner.org_id, "viewer")
    assert (
        await viewer.client.post(f"/v1/appearance-versions/{look}/identity-pack:generate", json={})
    ).status_code == 403
    assert (await viewer.client.get(f"/v1/appearance-versions/{look}/identity-pack")).status_code == 200


async def test_voice_design_patch_test_and_approve(harness: ApiHarness, owner: ApiTenant) -> None:
    started = _record_starts(harness)
    accepted = await owner.client.post(
        "/v1/voices", json={"name": "V", "description": "calm adult voice", "language": "en-US", "count": 3}
    )
    assert accepted.status_code == 202
    assert started[-1][0] == "VoiceDesignWorkflow" and started[-1][1]["args"]["count"] == 3
    voice_id = accepted.json()["voice_id"]
    assert (await owner.client.get(f"/v1/voices/{voice_id}/candidates")).json() == []
    async with harness.services.db.transaction() as session:
        version = VoiceVersion(org_id=owner.org_id, voice_id=uuid.UUID(voice_id), number=1, status="draft")
        session.add(version)
        await session.flush()
        version_id = str(version.id)
    bad = await owner.client.patch(f"/v1/voice-versions/{version_id}", json={"lexicon": [{"term": "x"}]})
    assert bad.status_code == 422  # a lexicon entry needs exactly one of respelling or phonemes
    patched = await owner.client.patch(
        f"/v1/voice-versions/{version_id}", json={"lexicon": [{"term": "GIF", "respelling": "jif"}]}
    )
    assert patched.status_code == 200 and patched.json()["lexicon"] == [{"term": "GIF", "respelling": "jif"}]
    tested = await owner.client.post(
        f"/v1/voice-versions/{version_id}:test", json={"text": "hello", "tags": ["laughs"]}
    )
    assert tested.status_code == 202 and started[-1][0] == "VoiceTestWorkflow"
    assert (await owner.client.post(f"/v1/voice-versions/{version_id}:approve")).status_code == 200
    assert (await owner.client.patch(f"/v1/voice-versions/{version_id}", json={"description": "x"})).status_code == 409


async def test_creator_tests_ratings_and_world_plates(harness: ApiHarness, owner: ApiTenant) -> None:
    started = _record_starts(harness)
    creator_id, _ = await _appearance(owner)
    version_id = (await owner.client.get(f"/v1/creators/{creator_id}")).json()["versions"][0]["id"]
    world = (await owner.client.post("/v1/worlds", json={"dna": home_office_world().model_dump(mode="json")})).json()
    draft_world = world["versions"][0]["id"]
    refused = await owner.client.post(
        f"/v1/creator-versions/{version_id}/tests", json={"world_version_id": draft_world}
    )
    assert refused.status_code == 409  # tests run in approved worlds
    accepted = await owner.client.post(f"/v1/creator-versions/{version_id}/tests", json={})
    assert accepted.status_code == 202 and started[-1][0] == "CreatorTestWorkflow"
    test_id = accepted.json()["creator_test_id"]
    test = (await owner.client.get(f"/v1/creator-tests/{test_id}")).json()
    assert test["job_id"] == accepted.json()["job_id"] and test["scorecard"] == {"status": "queued"}
    assert (
        await owner.client.post(f"/v1/creator-tests/{test_id}/ratings", json={"human_rating": 6})
    ).status_code == 422
    rated = await owner.client.post(f"/v1/creator-tests/{test_id}/ratings", json={"same_person_rating": 2})
    assert rated.json()["same_person_rating"] == 2 and rated.json()["scorecard"]["same_person_rating"]["value"] == 2
    assert [t["id"] for t in (await owner.client.get(f"/v1/creators/{creator_id}/tests")).json()] == [test_id]
    assert (await owner.client.get(f"/v1/creators/{creator_id}/baselines")).json() == []
    assert (await owner.client.get(f"/v1/creators/{creator_id}/consistency")).json() == []

    plates = await owner.client.post(f"/v1/world-versions/{draft_world}/plates:generate", json={"candidates": 1})
    assert plates.status_code == 202 and started[-1][1]["args"] == {"mode": "generate", "candidates": 1,
                                                                      "camera_position_keys": [], "times_of_day": [],
                                                                      "weather": []}  # fmt: skip
    continuity = (await owner.client.get(f"/v1/worlds/{world['id']}/continuity")).json()
    assert continuity["uses"] == [] and "Phase 11" in continuity["note"]


async def test_a_voice_planning_cannot_read_is_refused_at_approval(harness: ApiHarness, owner: ApiTenant) -> None:
    """Audit CR-VOICE-TRANSCRIPT: a voice version whose reference has no transcript was approved,
    linked to a creator, and only failed later — every video with that creator failed to plan."""
    accepted = await owner.client.post("/v1/voices", json={"name": "V", "description": "calm", "language": "en-US"})
    voice_id = accepted.json()["voice_id"]
    async with harness.services.db.transaction() as session:
        version = VoiceVersion(
            org_id=owner.org_id,
            voice_id=uuid.UUID(voice_id),
            number=1,
            status="draft",
            references=[{"asset_id": str(uuid.uuid4()), "language": "en-US", "transcript": ""}],
        )
        session.add(version)
        await session.flush()
        version_id = str(version.id)
    refused = await owner.client.post(f"/v1/voice-versions/{version_id}:approve")
    assert refused.status_code == 422, refused.text
    assert any(i["path"] == "/references/0/transcript" for i in refused.json()["issues"]), refused.json()
    assert (await owner.client.get(f"/v1/voice-versions/{version_id}")).json()["status"] == "draft"
