"""Phase 10 DoD, in one process on the Compose infrastructure, through the HTTP API (mock engines):

- **creator → test → approve**: a new creator's identity pack (candidates → canonical face →
  expansions with identity scores and the VLM age check → review → approve), a designed voice
  (candidates → select → test bench → approve), a wardrobe with generated references, then the
  Creator Test (the fixed test video, scorecard, baseline, ratings) and the creator version's approval;
- **world → plates → approve → used in a video**: a new version of a world, plate candidates, plate
  choices (each enqueues fingerprinting), approval, and a video whose scene is bound to the new
  version renders with the chosen canonical plate.

Every model call goes through the scheduler to the worker (studio jobs, ADR 0055)."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.services import build_services
from ce_api.testing import ApiHarness, ApiTenant
from ce_db.models.assets import Artifact, Asset, ExecutionNode, GenerationJob
from ce_db.models.videos import BuildManifestEntry
from ce_testing.fixtures import ALEX, alex_appearance_dna, alex_creator_dna, example_spec_dict, grey_hoodie
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]

TIMEOUT_S = 600


@pytest_asyncio.fixture
async def api(stack: Stack) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(build_services(stack.effective, pool_size=4))
    harness.services.workflows.before_start = None  # the stack runs the orchestrator worker
    yield harness
    await harness.aclose()


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def finish(api: ApiHarness, editor: ApiTenant, job_id: str) -> dict[str, Any]:
    """Waits for the job's workflow (the last one this API started) and returns the job."""
    handle = api.services.workflows.handles[-1]
    assert handle.id.endswith(job_id), (handle.id, job_id)
    result = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    job = (await editor.client.get(f"/v1/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (result, job.get("error"))
    return dict(result)


async def test_creator_identity_voice_wardrobe_then_test_then_approve(stack: Stack, api: ApiHarness) -> None:
    editor = await api.add_member(ALEX.ORG_ID, "editor")
    created = await editor.client.post(
        "/v1/creators", json={"name": "Robin", "kind": "synthetic", "dna": alex_creator_dna().model_dump(mode="json")}
    )
    assert created.status_code == 201, created.text
    creator = created.json()
    draft_version = creator["versions"][0]["id"]

    # ---- appearance: candidates → canonical → expansions, scores and the age check → review → approve
    appearance = (
        await editor.client.post(
            f"/v1/creators/{creator['id']}/appearances",
            json={"name": "Default", "dna": alex_appearance_dna().model_dump(mode="json")},
        )
    ).json()
    look = appearance["versions"][0]["id"]
    accepted = await editor.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:generate", json={"candidates": 3}
    )
    assert accepted.status_code == 202, accepted.text
    result = await finish(api, editor, accepted.json()["job_id"])
    assert len(result["candidates"]) == 3 and all(c["mock"] for c in result["candidates"])
    pack = (await editor.client.get(f"/v1/appearance-versions/{look}/identity-pack")).json()
    canonical = pack["identity_pack"]["candidates"][1]["asset_id"]
    chosen = await editor.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:choose", json={"asset_id": canonical}
    )
    assert chosen.status_code == 202
    result = await finish(api, editor, chosen.json()["job_id"])
    assert result["images"] == 11 and result["vlm_estimate"] >= 25  # 5 angles + 6 expressions; mock VLM: 25–45
    pack = (await editor.client.get(f"/v1/appearance-versions/{look}/identity-pack")).json()
    images = pack["identity_pack"]["images"]
    assert all(i["similarity"] is not None and i["decision"] == "pending" for i in images)
    assert pack["age_checks"]["vlm_mock"] is True and pack["age_checks"]["dna_age_appearance"] >= 18
    blocked = await editor.client.post(f"/v1/appearance-versions/{look}:approve")
    assert blocked.status_code == 409 and {i["code"] for i in blocked.json()["issues"]} == {"identity_pack"}
    reviewed = await editor.client.post(
        f"/v1/appearance-versions/{look}/identity-pack:review",
        json={"approve": [i["asset_id"] for i in images[:8]], "reject": [images[8]["asset_id"]]},
    )
    assert reviewed.status_code == 200
    assert (await editor.client.post(f"/v1/appearance-versions/{look}:approve")).status_code == 200

    # ---- voice: design → select a candidate → test bench → approve
    designed = await editor.client.post(
        "/v1/voices",
        json={"name": "Robin voice", "description": "warm, steady adult voice", "language": "en-US",
              "creator_id": creator["id"], "count": 2},
    )  # fmt: skip
    assert designed.status_code == 202, designed.text
    result = await finish(api, editor, designed.json()["job_id"])
    voice_id = designed.json()["voice_id"]
    candidates = (await editor.client.get(f"/v1/voices/{voice_id}/candidates")).json()
    assert len(candidates) == 2 and all(c["asset_id"] for c in candidates)
    selected = await editor.client.post(f"/v1/voices/{voice_id}/candidates/{candidates[0]['id']}:select")
    assert selected.status_code == 201, selected.text
    voice_version = selected.json()["id"]
    # the reference's transcript is what the candidate says (no sample text given: the studio default) —
    # it was stored empty, and every video with this voice then failed to plan (audit CR-VOICE-TRANSCRIPT)
    reference = selected.json()["references"][0]
    assert reference["transcript"] == api.services.config.studio.voice_test_text
    # a second Select of the same candidate (a double click) returns the open draft, not another one,
    # and the list marks that candidate alone as selected (audit D14)
    again = await editor.client.post(f"/v1/voices/{voice_id}/candidates/{candidates[0]['id']}:select")
    assert again.status_code == 201 and again.json()["id"] == voice_version
    marked = (await editor.client.get(f"/v1/voices/{voice_id}/candidates")).json()
    assert [c["selected"] for c in marked] == [True, False]
    assert len((await editor.client.get(f"/v1/voices/{voice_id}")).json()["versions"]) == 1
    patched = await editor.client.patch(
        f"/v1/voice-versions/{voice_version}", json={"lexicon": [{"term": "Robin", "respelling": "ROB-in"}]}
    )
    assert patched.status_code == 200
    tested = await editor.client.post(f"/v1/voice-versions/{voice_version}:test", json={"text": "Robin says hello."})
    bench = await finish(api, editor, tested.json()["job_id"])
    assert bench["audio_artifact_id"] and bench["wer"] is not None and bench["wpm"] > 0
    assert bench["speaker_similarity"] is not None and bench["mock"] is True
    assert "wpm_recorded" not in bench  # a mock engine never calibrates the WPM
    assert (await editor.client.post(f"/v1/voice-versions/{voice_version}:approve")).status_code == 200

    # ---- wardrobe: generated references conditioned on the canonical face
    wardrobe = (
        await editor.client.post(
            f"/v1/creators/{creator['id']}/wardrobes",
            json={"name": "grey hoodie", "spec": grey_hoodie().model_dump(mode="json")},
        )
    ).json()
    wardrobe_version = wardrobe["versions"][0]["id"]
    refs = await editor.client.post(f"/v1/wardrobe-versions/{wardrobe_version}/references:generate")
    result = await finish(api, editor, refs.json()["job_id"])
    assert len(result["references"]) == 3 and all(r["similarity"] is not None for r in result["references"])
    assert (await editor.client.post(f"/v1/wardrobe-versions/{wardrobe_version}:approve")).status_code == 200

    # ---- the draft creator version takes them; Creator Test; ratings; approve
    patched = await editor.client.patch(
        f"/v1/creator-versions/{draft_version}",
        json={"appearance_version_id": look, "voice_version_id": voice_version,
              "defaults": {"world_ids": [str(ALEX.WORLD_ID)], "wardrobe_version_ids": [wardrobe_version]}},
    )  # fmt: skip
    assert patched.status_code == 200, patched.text
    started = await editor.client.post(f"/v1/creator-versions/{draft_version}/tests", json={})
    assert started.status_code == 202, started.text
    result = await finish(api, editor, started.json()["job_id"])
    assert result["build_state"] == "ready"
    test = (await editor.client.get(f"/v1/creator-tests/{started.json()['creator_test_id']}")).json()
    card = test["scorecard"]
    assert card["status"] == "scored" and card["build_state"] == "ready"
    assert card["identity_similarity"]["status"] == "measured" and card["identity_similarity"]["mock"] is True
    assert card["wer"]["status"] == "measured" and card["wpm"]["status"] == "measured"
    assert card["accent"]["status"] == "not_measured"  # a human rating, never inferred
    assert card["requested_vs_observed"]["items"] > 0
    assert test["render_artifact_id"] and test["world_version_id"] == str(ALEX.WORLD_VERSION_ID)
    rated = await editor.client.post(
        f"/v1/creator-tests/{test['id']}/ratings", json={"human_rating": 4, "same_person_rating": 5, "accent_rating": 4}
    )
    assert rated.status_code == 200 and rated.json()["scorecard"]["accent"]["status"] == "rated"
    baselines = (await editor.client.get(f"/v1/creators/{creator['id']}/baselines")).json()
    assert baselines and baselines[0]["source"] == "creator_test" and "wpm" in baselines[0]["stats"]
    history = (await editor.client.get(f"/v1/creators/{creator['id']}/tests")).json()
    assert [t["id"] for t in history] == [test["id"]]
    approved = await editor.client.post(
        f"/v1/creator-versions/{draft_version}:approve", json={"attest_adult_presentation": True}
    )
    assert approved.status_code == 200, approved.text
    # the approved creator is cast in a video: planning and previz succeed with its designed voice
    project = (await editor.client.post("/v1/projects", json={"name": "Robin's first"})).json()["id"]
    body = {"input": "Short tip: keep your bedroom cool and dark.", "cast": [{"creator_id": creator["id"]}]}
    planned = await editor.client.post(f"/v1/projects/{project}/videos", json=body, headers=key())
    assert planned.status_code == 202, planned.text
    outcome = await asyncio.wait_for(api.services.workflows.handles[-1].result(), TIMEOUT_S)
    assert outcome["plan"]["status"] == "succeeded", outcome
    assert outcome["previz"] == {"state": "previz_ready", "failed": []}, outcome
    async with api.services.db.session() as session:  # the studio calls ran on the worker, as nodes of their jobs
        studio_nodes = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(ExecutionNode)
                .join(GenerationJob, GenerationJob.id == ExecutionNode.job_id)
                .where(ExecutionNode.version_id.is_(None), GenerationJob.kind == "identity_pack",
                       ExecutionNode.status == "succeeded")
            )
        ).scalar_one()  # fmt: skip
    assert studio_nodes >= 3 + 12 + 12


async def test_world_plates_approve_then_used_in_a_video(stack: Stack, api: ApiHarness) -> None:
    editor = await api.add_member(ALEX.ORG_ID, "editor")
    draft = await editor.client.post(f"/v1/worlds/{ALEX.WORLD_ID}/versions", json={})
    assert draft.status_code == 201, draft.text
    version = draft.json()
    generated = await editor.client.post(f"/v1/world-versions/{version['id']}/plates:generate", json={"candidates": 2})
    assert generated.status_code == 202, generated.text
    result = await finish(api, editor, generated.json()["job_id"])
    candidates = result["plate_candidates"]
    dna = version["dna"]
    tod, weather = dna["time_and_weather"]["default_time_of_day"], dna["time_and_weather"]["default_weather"]
    permitted = [c["key"] for c in dna["camera_positions"] if c.get("status", "permitted") == "permitted"]
    assert set(candidates) == set(permitted) and all(len(candidates[p][tod][weather]) == 2 for p in permitted)
    blocked = await editor.client.post(f"/v1/world-versions/{version['id']}:approve")
    assert blocked.status_code == 409
    chosen: dict[str, str] = {}
    for position in permitted:
        asset_id = candidates[position][tod][weather][0]
        response = await editor.client.post(
            f"/v1/world-versions/{version['id']}/plates:choose",
            json={"camera_position_key": position, "time_of_day": tod, "weather": weather, "asset_id": asset_id},
        )
        assert response.status_code == 200, response.text
        chosen[position] = asset_id
        fingerprint_job = response.json()["fingerprint_job_id"]
        await finish(api, editor, fingerprint_job)  # each choice fingerprints the chosen set
    current = (await editor.client.get(f"/v1/world-versions/{version['id']}")).json()
    assert current["fingerprints_artifact_id"]
    approved = await editor.client.post(f"/v1/world-versions/{version['id']}:approve")
    assert approved.status_code == 200, approved.text

    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    scene = spec["scenes"][0]
    scene["world"] = {**copy.deepcopy(scene["world"]), "world_version_id": version["id"], "time_of_day": tod,
                      "weather": weather,
                      "overrides": {"element_states": {}, "hide_elements": [], "add_elements": [], "move_elements": [],
                                    "lighting": None, "acoustics": None}}  # fmt: skip
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    built = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    assert built.state == "ready", (built.failed, built.statuses)
    position = scene["world"]["camera_position_key"]
    async with api.services.db.session() as session:
        sha = (
            await session.execute(
                sa.select(Artifact.sha256)
                .join(BuildManifestEntry, BuildManifestEntry.artifact_id == Artifact.id)
                .where(BuildManifestEntry.version_id == submitted.version_id,
                       BuildManifestEntry.node_key == "world.plate:scn_hook")
            )
        ).scalar_one()  # fmt: skip
        plate_sha = (
            await session.execute(sa.select(Asset.sha256).where(Asset.id == uuid.UUID(chosen[position])))
        ).scalar_one()
    output = await stack.exec.docs.output(sha)
    assert output.refs["image"].sha256 == plate_sha  # the scene renders on the chosen canonical plate
    continuity = (await editor.client.get(f"/v1/worlds/{ALEX.WORLD_ID}/continuity")).json()
    assert any(u["version_id"] == str(submitted.version_id) and u["world_version_id"] == version["id"]
               for u in continuity["uses"])  # fmt: skip
