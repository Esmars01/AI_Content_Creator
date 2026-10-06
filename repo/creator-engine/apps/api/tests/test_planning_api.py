"""Planning, previz and approval over HTTP (§30, §13): `POST /v1/projects/{id}/videos` writes the
plan job and starts `PlanVideoWorkflow`; the Director runs in the job (here: its activity body,
in-process, with the fixture LLM); previz, intent, replan and approval read and act on the result.

Workflow starts are recorded instead of sent to Temporal, so each step runs exactly once; the
end-to-end flow through Temporal and the workers is `tests/e2e/test_plan_mock.py`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services
from ce_core.behavior.plan_report import Finding, PlanReport
from ce_core.canonical import canonical_json
from ce_core.enums import ArtifactKind, VersionFlag, VersionState
from ce_db import execution as rec
from ce_db.models.assets import Artifact, GenerationJob
from ce_db.models.platform import AuditLog
from ce_db.models.videos import DirectorRun, Video, VideoVersion
from ce_exec.context import build_services as build_exec_services
from ce_exec.planning import run_plan
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX

pytestmark = [pytest.mark.infra]

AI_AGENTS = "Create a 30-second TikTok explaining why most people misunderstand AI agents."


@pytest_asyncio.fixture
async def harness(seeded_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(
        seeded_db.url, storage="local_fs", storage_root=tmp_path / "storage", extra_env={"MOCK_GPU": "true"}
    )
    for bucket in (services.settings.s3_bucket_assets, services.settings.s3_bucket_artifacts):
        await services.storage.ensure_bucket(bucket)
    h = ApiHarness(services)
    h.started = []  # type: ignore[attr-defined]

    async def record_start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        h.started.append((workflow, arg, workflow_id))  # type: ignore[attr-defined]

    services.workflows.start = record_start  # type: ignore[method-assign]
    h.exec = build_exec_services(services.effective, pool_size=2)  # type: ignore[attr-defined]
    yield h
    await h.exec.close()  # type: ignore[attr-defined]
    await h.aclose()


@pytest_asyncio.fixture
async def editor(harness: ApiHarness) -> ApiTenant:
    return await harness.add_member(ALEX.ORG_ID, "editor")


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def new_project(tenant: ApiTenant) -> str:
    response = await tenant.client.post("/v1/projects", json={"name": f"P {uuid.uuid4().hex[:6]}"})
    response.raise_for_status()
    return str(response.json()["id"])


async def plan(harness: ApiHarness, tenant: ApiTenant, body: dict[str, Any]) -> dict[str, Any]:
    """Create → run the plan job (the `plan_video` activity body) → a `planned` version."""
    project = await new_project(tenant)
    response = await tenant.client.post(f"/v1/projects/{project}/videos", json=body, headers=key())
    assert response.status_code == 202, response.text
    await harness.services.drain()
    ids: dict[str, Any] = response.json()
    result = await run_plan(harness.exec, tenant.org_id, UUID(ids["job_id"]))  # type: ignore[attr-defined]
    assert result["status"] == "succeeded", result
    return ids | result | {"project_id": project}


async def previz_ready(harness: ApiHarness, version_id: str, **values: Any) -> None:
    """Stands in for PrevizWorkflow (its GPU nodes run in the e2e test)."""
    async with harness.services.db.transaction() as session:
        await rec.set_version_state(session, ALEX.ORG_ID, UUID(version_id), VersionState.PREVIZ_RUNNING)
        await rec.set_version_state(session, ALEX.ORG_ID, UUID(version_id), VersionState.PREVIZ_READY, **values)


async def replace_report(harness: ApiHarness, version_id: str, findings: list[Finding]) -> None:
    exec_svc = harness.exec  # type: ignore[attr-defined]
    async with harness.services.db.session() as session:
        version = await session.get_one(VideoVersion, UUID(version_id))
        artifact = await session.get_one(Artifact, version.plan_report_artifact_id)
    report = PlanReport.model_validate_json(await exec_svc.content.read_bytes(artifact.sha256))
    report = report.model_copy(update={"findings": [*report.findings, *findings]})
    raw = canonical_json(report.model_dump(mode="json")).encode()
    sha = await exec_svc.content.put_bytes(raw, mime="application/json")
    async with harness.services.db.transaction() as session:
        artifact_id = await rec.register_artifact(
            session,
            ALEX.ORG_ID,
            sha256=sha,
            kind=ArtifactKind.PLAN_REPORT.value,
            mime="application/json",
            size=len(raw),
            storage_key=exec_svc.content.key(sha),
        )
        version = await session.get_one(VideoVersion, UUID(version_id))
        version.plan_report_artifact_id = artifact_id


async def test_create_video_validates_and_starts_the_plan_workflow(harness: ApiHarness, editor: ApiTenant) -> None:
    project = await new_project(editor)
    path = f"/v1/projects/{project}/videos"
    body = {
        "input": AI_AGENTS,
        "style": {"caption_style_id": "bold_pop_highlight"},
        "advanced": {"takes": 2, "acting_hints": ["deadpan, then a crack of a smile"]},
        "budget_usd": 5,
    }
    missing = await editor.client.post(path, json=body)
    assert missing.status_code == 422 and missing.json()["issues"][0]["code"] == "idempotency_key_missing"
    bad = await editor.client.post(
        path, json={"input": "x", "mode": "nope", "style": {"caption_style_id": "nope"}}, headers=key()
    )
    assert bad.status_code == 422
    assert {i["code"] for i in bad.json()["issues"]} == {"mode", "caption_style"}
    sources = await editor.client.post(path, json={"input": "x", "sources": [str(uuid.uuid4())]}, headers=key())
    # Phase 12: persistent sources are accepted when they are ingested sources of this project
    assert sources.status_code == 422 and sources.json()["issues"][0]["code"] == "unknown_source"
    cast = await editor.client.post(
        path, json={"input": "x", "cast": [{"creator_id": str(uuid.uuid4())}]}, headers=key()
    )
    assert cast.status_code == 404
    viewer = await harness.add_member(ALEX.ORG_ID, "viewer")
    assert (await viewer.client.post(path, json=body, headers=key())).status_code == 403

    headers = key()
    accepted = await editor.client.post(path, json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    ids = accepted.json()
    replay = await editor.client.post(path, json=body, headers=headers)
    assert replay.status_code == 202 and replay.json() == ids  # one video, one job
    await harness.services.drain()
    ((workflow, arg, workflow_id),) = harness.started  # type: ignore[attr-defined]
    assert (workflow, workflow_id) == ("PlanVideoWorkflow", f"plan-{ids['job_id']}")
    assert arg["job_id"] == ids["job_id"] and arg["version_id"] == ids["version_id"]
    async with harness.services.db.session() as session:
        job = await session.get_one(GenerationJob, UUID(ids["job_id"]))
        video = await session.get_one(Video, UUID(ids["video_id"]))
        count = (
            await session.execute(sa.select(sa.func.count()).where(Video.project_id == UUID(project)))
        ).scalar_one()
    assert job.kind == "plan" and job.status == "queued" and job.temporal_workflow_id == f"plan-{job.id}"
    request = job.input["request"]
    assert request["caption_style_id"] == "bold_pop_highlight" and request["takes"] == 2
    assert request["acting_hints"] == ["deadpan, then a crack of a smile"]
    assert job.input["version_id"] == ids["version_id"] and job.input["origin"] == "plan"
    assert count == 1 and float(video.budget_usd or 0) == 5
    # the version row appears once planning is done; the video says planning is under way
    assert (await editor.client.get(f"/v1/versions/{ids['version_id']}")).status_code == 404
    pending = (await editor.client.get(f"/v1/videos/{ids['video_id']}")).json()
    assert pending["planning"] is True and pending["current_version_state"] is None


async def test_create_options_come_from_configuration(harness: ApiHarness, editor: ApiTenant) -> None:
    options = (await editor.client.get("/v1/create-options")).json()
    modes = {m["id"]: m for m in options["modes"]}
    assert modes["talking_head_explainer"]["plannable"] and not modes["podcast"]["plannable"]
    assert modes["talking_head_explainer"]["duration_s"]["min"] > 0
    assert options["default_mode"] == "talking_head_explainer"
    languages = {lang["id"]: lang for lang in options["languages"]}
    assert languages["en"]["support"] == "production" and languages["az"]["support"] == "unsupported"
    assert languages["ar"]["rtl"] is True
    assert {c["id"] for c in options["caption_styles"]} >= {"bold_pop_highlight", "clean_subtitle"}
    assert len(options["music_moods"]) == 3 and "exact_script" in options["input_modes"]
    assert {p["id"] for p in options["platforms"]} >= {"tiktok", "youtube"}
    assert all(c["id"] != "screen_only" or c["label"] for c in options["camera_profiles"])


async def test_plan_previz_intent_then_approve_starts_generation(harness: ApiHarness, editor: ApiTenant) -> None:
    ids = await plan(harness, editor, {"input": AI_AGENTS})
    version_id = ids["version_id"]
    version = (await editor.client.get(f"/v1/versions/{version_id}")).json()
    assert version["state"] == "planned" and version["origin"] == "plan" and version["number"] == 1
    assert version["spec"]["cast"][0]["creator_version_id"] == str(ALEX.CREATOR_VERSION_ID)
    video = (await editor.client.get(f"/v1/videos/{ids['video_id']}")).json()
    assert video["current_version_id"] == version_id and video["title"] == version["spec"]["meta"]["title"]
    assert (video["current_version_state"], video["current_version_number"], video["planning"]) == ("planned", 1, False)
    listed = (await editor.client.get(f"/v1/projects/{ids['project_id']}/videos")).json()["items"]
    assert [v["current_version_state"] for v in listed] == ["planned"]

    storyboard = (await editor.client.get(f"/v1/versions/{version_id}/storyboard")).json()
    shot_count = sum(len(s["shots"]) for s in version["spec"]["scenes"])
    assert len(storyboard["shots"]) == shot_count and [s["order"] for s in storyboard["shots"]] == list(
        range(shot_count)
    )
    talking = [s for s in storyboard["shots"] if s["type"] == "talking_head"]
    assert talking and all(s["text"] for s in talking)
    assert all(s["keyframe"] is None for s in storyboard["shots"])  # previz has not run here
    assert any(s["plate"] and s["plate"]["url"] for s in talking)  # Alex's office plate

    previz = (await editor.client.get(f"/v1/versions/{version_id}/previz")).json()
    assert previz["state"] == "planned" and previz["planner"] == "llm" and previz["timing_source"] == "estimated"
    report = previz["plan_report"]
    assert report["predicted_coverage"]["entries"] and report["memory_items_used"]
    assert previz["blocking"] == []

    intent = (await editor.client.get(f"/v1/versions/{version_id}/intent")).json()
    assert intent["video"]["narrative_goal"] and len(intent["scenes"]) == len(version["spec"]["scenes"])
    decisions = [d for s in intent["scenes"] for d in s["decisions"]]
    assert decisions and all({"rule_id", "applied", "intent_ref"} <= set(d) for d in decisions)
    derived = [d for s in intent["scenes"] for d in s["derived"]] + intent["derived"]
    assert derived and all(d["path"].startswith("/") and d["derived_from"] for d in derived)

    async with harness.services.db.session() as session:
        stages = set(
            (
                await session.execute(sa.select(DirectorRun.stage).where(DirectorRun.version_id == UUID(version_id)))
            ).scalars()
        )
        previz_job = (
            await session.execute(
                sa.select(GenerationJob).where(
                    GenerationJob.video_version_id == UUID(version_id), GenerationJob.kind == "previz"
                )
            )
        ).scalar_one()
    assert {"interpret", "script", "acting", "intent_policy", "finalize"} <= stages
    assert str(previz_job.id) == ids["previz_job_id"] and previz_job.temporal_workflow_id == f"previz-{previz_job.id}"

    early = await editor.client.post(f"/v1/versions/{version_id}:approve", json={}, headers=key())
    assert early.status_code == 409  # previz first
    await previz_ready(harness, version_id)
    headers = key()
    approved = await editor.client.post(f"/v1/versions/{version_id}:approve", json={}, headers=headers)
    assert approved.status_code == 202, approved.text
    assert (await editor.client.post(f"/v1/versions/{version_id}:approve", json={}, headers=headers)).json() == (
        approved.json()
    )
    await harness.services.drain()
    workflow, arg, workflow_id = harness.started[-1]  # type: ignore[attr-defined]
    job_id = approved.json()["job_id"]
    assert (workflow, workflow_id, arg["version_id"]) == ("GenerateVersionWorkflow", f"generate-{job_id}", version_id)
    assert (await editor.client.get(f"/v1/versions/{version_id}")).json()["state"] == "approved"
    again = await editor.client.post(f"/v1/versions/{version_id}:approve", json={}, headers=key())
    assert again.status_code == 409
    async with harness.services.db.session() as session:
        actions = set(
            (await session.execute(sa.select(AuditLog.action).where(AuditLog.target_id == version_id))).scalars()
        )
    assert "video_version.approve" in actions


async def test_approval_needs_blocking_findings_resolved(harness: ApiHarness, editor: ApiTenant) -> None:
    ids = await plan(harness, editor, {"input": AI_AGENTS})
    version_id = ids["version_id"]
    claim = Finding(
        kind="fact_check",
        severity="blocking",
        message="Unsupported claim: '90% of agents fail'",
        detail={"id": "claim:clm_9", "overridable": True},
    )
    await replace_report(harness, version_id, [claim])
    await previz_ready(harness, version_id, flags=[VersionFlag.NEEDS_WORLD_APPROVAL.value])
    path = f"/v1/versions/{version_id}:approve"

    blocked = await editor.client.post(path, json={}, headers=key())
    assert blocked.status_code == 409 and blocked.json()["code"] == "approval_blocked"
    codes = {i["code"] for i in blocked.json()["issues"]}
    assert codes == {"needs_world_approval", "blocking_finding"}
    async with harness.services.db.transaction() as session:
        await rec.set_version_state(session, ALEX.ORG_ID, UUID(version_id), VersionState.PREVIZ_READY, flags=[])

    no_reason = await editor.client.post(path, json={"overrides": ["claim:clm_9"]}, headers=key())
    assert {i["code"] for i in no_reason.json()["issues"]} == {"override_reason"}
    unknown = await editor.client.post(
        path, json={"overrides": ["claim:clm_9", "claim:nope"], "reason": "checked"}, headers=key()
    )
    assert {i["code"] for i in unknown.json()["issues"]} == {"override_unknown"}
    previz = (await editor.client.get(f"/v1/versions/{version_id}/previz")).json()
    assert previz["blocking"] == [
        {"id": "claim:clm_9", "kind": "fact_check", "message": claim.message, "overridable": True}
    ]
    ok = await editor.client.post(
        path, json={"overrides": ["claim:clm_9"], "reason": "Sourced in the brief."}, headers=key()
    )
    assert ok.status_code == 202, ok.text
    async with harness.services.db.session() as session:
        rows = list(
            (
                await session.execute(
                    sa.select(AuditLog).where(
                        AuditLog.target_id == version_id, AuditLog.action == "plan.finding_override"
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1 and (rows[0].after or {}).get("reason") == "Sourced in the brief."

    # policy findings (blocklists, the testimonial guard) cannot be overridden
    second = await plan(harness, editor, {"input": AI_AGENTS})
    policy = Finding(
        kind="policy",
        severity="blocking",
        message="First-person product experience needs a source or a dramatization",
        detail={"id": "testimonial:seg_1", "overridable": False},
    )
    await replace_report(harness, second["version_id"], [policy])
    await previz_ready(harness, second["version_id"])
    refused = await editor.client.post(
        f"/v1/versions/{second['version_id']}:approve",
        json={"overrides": ["testimonial:seg_1"], "reason": "I insist"},
        headers=key(),
    )
    assert refused.status_code == 409
    messages = [i["message"] for i in refused.json()["issues"]]
    assert any("cannot be overridden" in m for m in messages)


async def test_replan_reuses_the_request_and_the_pinned_memory(harness: ApiHarness, editor: ApiTenant) -> None:
    first = await plan(harness, editor, {"input": AI_AGENTS, "target_duration_s": 30})
    v1 = (await editor.client.get(f"/v1/versions/{first['version_id']}")).json()
    unknown = await editor.client.post(
        f"/v1/versions/{first['version_id']}:replan", json={"scope": ["scn_99"]}, headers=key()
    )
    assert unknown.status_code == 422
    response = await editor.client.post(
        f"/v1/versions/{first['version_id']}:replan",
        json={"instruction": "make the opening more aggressive", "scope": [v1["spec"]["scenes"][0]["key"]]},
        headers=key(),
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["video_id"] == first["video_id"] and accepted["version_id"] != first["version_id"]
    await harness.services.drain()
    async with harness.services.db.session() as session:
        job = await session.get_one(GenerationJob, UUID(accepted["job_id"]))
    assert job.input["origin"] == "replan" and job.input["parent_version_id"] == first["version_id"]
    assert job.input["reuse_snapshots"] is True
    assert job.input["request"]["instruction"] == "make the opening more aggressive"
    assert job.input["request"]["target_duration_s"] == 30  # the original request carries over
    result = await run_plan(harness.exec, ALEX.ORG_ID, UUID(accepted["job_id"]))  # type: ignore[attr-defined]
    assert result["status"] == "succeeded"
    v2 = (await editor.client.get(f"/v1/versions/{accepted['version_id']}")).json()
    assert (v2["number"], v2["origin"], v2["parent_version_id"]) == (2, "replan", first["version_id"])
    pins = [(s["character_key"], s["snapshot_id"]) for s in v2["spec"]["memory"]["snapshots"]]
    assert pins == [(s["character_key"], s["snapshot_id"]) for s in v1["spec"]["memory"]["snapshots"]]

    # instructions accumulate; refresh_memory takes a new snapshot
    again = await editor.client.post(
        f"/v1/versions/{accepted['version_id']}:replan",
        json={"instruction": "shorter hook", "refresh_memory": True},
        headers=key(),
    )
    await harness.services.drain()
    async with harness.services.db.session() as session:
        job = await session.get_one(GenerationJob, UUID(again.json()["job_id"]))
    assert job.input["request"]["instruction"] == "make the opening more aggressive\nThen: shorter hook"
    assert job.input["reuse_snapshots"] is False
    await run_plan(harness.exec, ALEX.ORG_ID, UUID(again.json()["job_id"]))  # type: ignore[attr-defined]
    v3 = (await editor.client.get(f"/v1/versions/{again.json()['version_id']}")).json()
    assert v3["spec"]["memory"]["snapshots"][0]["snapshot_id"] != pins[0][1]

    # approved versions are past replanning
    await previz_ready(harness, v3["id"])
    await editor.client.post(f"/v1/versions/{v3['id']}:approve", json={}, headers=key())
    late = await editor.client.post(f"/v1/versions/{v3['id']}:replan", json={}, headers=key())
    assert late.status_code == 409
