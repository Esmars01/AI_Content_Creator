"""Edits, locks and versions over HTTP (§28, §30, §12.8): proposals, apply/reject, the thin
wrappers (locks, regenerate, re-route, take selection), restore/branch/duplicate/resume, the
takes gallery and compare.

Workflow starts are recorded instead of sent to Temporal; each job's activity body (`propose_edit`,
`run_apply_job`) runs in-process with the fixture LLM. The builds that follow run end to end in
`tests/e2e/test_edit_mock.py`."""

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
from ce_core.enums import VersionState
from ce_db import execution as rec
from ce_db.models.assets import GenerationJob
from ce_db.models.platform import AuditLog
from ce_db.models.videos import EditProposal, Project, Take, Video, VideoVersion
from ce_exec.context import build_services as build_exec_services
from ce_exec.editing import propose_edit, run_apply_job
from ce_exec.submit import Submitted, submit_spec
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, two_scene_spec_dict

pytestmark = [pytest.mark.infra]


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


async def submitted(harness: ApiHarness, data: dict[str, Any] | None = None) -> Submitted:
    """An approved version of the two-scene example (no build ran: proposals plan from scratch)."""
    spec = data or two_scene_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    return await submit_spec(
        harness.exec,  # type: ignore[attr-defined]
        org_id=ALEX.ORG_ID,
        project_id=await new_project(harness),
        spec_data=spec,
    )


async def new_project(harness: ApiHarness) -> UUID:
    """A project of its own, so other tests reading the seeded project see only their videos."""
    async with harness.services.db.transaction() as session:
        project = Project(org_id=ALEX.ORG_ID, name=f"edits {uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        return project.id


def last_start(harness: ApiHarness) -> tuple[str, dict[str, Any], str]:
    started: list[tuple[str, dict[str, Any], str]] = harness.started  # type: ignore[attr-defined]
    return started[-1]


async def run_propose(harness: ApiHarness, job_id: str) -> dict[str, Any]:
    await harness.services.drain()
    return await propose_edit(harness.exec, ALEX.ORG_ID, UUID(job_id))  # type: ignore[attr-defined]


async def run_apply(harness: ApiHarness, job_id: str) -> dict[str, Any]:
    await harness.services.drain()
    return await run_apply_job(harness.exec, ALEX.ORG_ID, UUID(job_id))  # type: ignore[attr-defined]


async def version_row(harness: ApiHarness, version_id: UUID | str) -> VideoVersion:
    async with harness.services.db.session() as session:
        row: VideoVersion = await session.get_one(VideoVersion, UUID(str(version_id)))
        return row


async def current_version(harness: ApiHarness, video_id: UUID) -> UUID | None:
    async with harness.services.db.session() as session:
        return (await session.get_one(Video, video_id)).current_version_id


# ---------------------------------------------------------------------------------- proposals


async def test_an_instruction_becomes_a_proposal_and_applying_it_derives_a_version(
    harness: ApiHarness, editor: ApiTenant
) -> None:
    sub = await submitted(harness)
    response = await editor.client.post(
        f"/v1/versions/{sub.version_id}/edits",
        json={"instruction": "Make him more skeptical.", "selection": {"scene_keys": ["scn_reveal"]}},
        headers=key(),
    )
    assert response.status_code == 202, response.text
    ids = response.json()
    workflow, arg, workflow_id = last_start(harness)
    assert (workflow, workflow_id) == ("ProposeEditWorkflow", f"edit_propose-{ids['job_id']}")
    assert arg["version_id"] == str(sub.version_id)
    pending = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert pending["status"] == "proposing" and pending["selection"]["editor"] == {"scene_keys": ["scn_reveal"]}

    result = await run_propose(harness, ids["job_id"])
    assert result["status"] == "proposed", result
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert edit["status"] == "proposed" and edit["impact"]["planner"] == "fixture"
    assert [op["op"] for op in edit["ops"]] == ["set_acting", "add_behavior_event"]
    paths = [op["path"] for op in edit["patch"]]
    assert any(p.startswith("/scenes[scn_reveal]") for p in paths)
    assert not any(p.startswith("/scenes[scn_hook]") for p in paths)
    regenerate = set(edit["impact"]["regenerate"]) | set(edit["impact"]["cascade"])
    assert "behavior.resolve:scn_reveal" in regenerate
    assert not any(k.endswith(":scn_hook") or ":sht_1" in k for k in regenerate)  # the hook is untouched
    assert edit["coverage_delta"]["items"] and edit["impact"]["estimate"]
    assert {a["strategy"] for a in edit["alternatives"]} >= {"editorial_only"}
    listed = (await editor.client.get(f"/v1/versions/{sub.version_id}/edits")).json()
    assert [e["id"] for e in listed] == [ids["edit_proposal_id"]]

    applied = await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:apply", json={}, headers=key())
    assert applied.status_code == 202, applied.text
    apply_ids = applied.json()
    assert last_start(harness)[0] == "ApplyEditWorkflow"
    outcome = await run_apply(harness, apply_ids["job_id"])
    assert outcome["status"] == "succeeded" and outcome["version_id"] == apply_ids["new_version_id"]
    assert outcome["next_kind"] == "generate"  # the parent passed approval: generation follows

    child = await version_row(harness, apply_ids["new_version_id"])
    parent = await version_row(harness, sub.version_id)
    assert (child.origin, child.parent_version_id, child.state) == ("edit", sub.version_id, "approved")
    assert child.number == parent.number + 1 and child.planned_routes and child.plan_report_artifact_id
    assert parent.spec == (await version_row(harness, sub.version_id)).spec  # never mutated (I3)
    assert await current_version(harness, sub.video_id) == child.id
    reveal = next(s for s in child.spec["scenes"] if s["key"] == "scn_reveal")
    assert any(e["type"] == "eyebrow_raise" for e in reveal["acting"]["events"])
    assert child.spec["scenes"][0] == parent.spec["scenes"][0]  # the hook scene is byte-identical
    previz = (await editor.client.get(f"/v1/versions/{child.id}/previz")).json()
    notes = [f for f in previz["plan_report"]["findings"] if f["detail"].get("check") == "derived_version"]
    assert notes and "more skeptical" in notes[0]["message"].lower()
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert edit["status"] == "applied" and edit["result_version_id"] == str(child.id)
    async with harness.services.db.session() as session:
        job = (
            await session.execute(
                sa.select(GenerationJob).where(
                    GenerationJob.video_version_id == child.id, GenerationJob.kind == "generate"
                )
            )
        ).scalar_one()
    assert job.status == "queued" and job.input["origin"] == "edit"

    again = await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:apply", json={}, headers=key())
    assert again.status_code == 409  # applied once


async def test_compare_reports_spec_intent_and_coverage_differences(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    response = await editor.client.post(
        f"/v1/versions/{sub.version_id}/edits",
        json={"operations": [{"op": "set_camera", "add_moves": [{"type": "handheld_drift", "scale": 0.6}]}]},
        headers=key(),
    )
    assert response.status_code == 202, response.text
    ids = response.json()
    assert (await run_propose(harness, ids["job_id"]))["status"] == "proposed"
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert edit["impact"]["planner"] == "structured" and edit["instruction"] == ""
    applied = (await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:apply", headers=key())).json()
    await run_apply(harness, applied["job_id"])
    compare = await editor.client.get(
        f"/v1/videos/{sub.video_id}/compare", params={"a": str(sub.version_id), "b": applied["new_version_id"]}
    )
    assert compare.status_code == 200, compare.text
    body = compare.json()
    assert body["spec"] and {d["area"] for d in body["spec"]} == {"camera"}
    assert all("/camera/moves" in d["path"] for d in body["spec"])
    assert body["intent"] == [] and body["cbs"] == {} and body["cbs_available"] == {"a": False, "b": False}
    assert body["renders"] == {"a": [], "b": []}
    other = await submitted(harness)
    wrong = await editor.client.get(
        f"/v1/videos/{sub.video_id}/compare", params={"a": str(sub.version_id), "b": str(other.version_id)}
    )
    assert wrong.status_code == 404


async def test_invalid_edits_are_refused_before_any_job(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    url = f"/v1/versions/{sub.version_id}/edits"
    empty = await editor.client.post(url, json={"instruction": "  "}, headers=key())
    assert empty.status_code == 422
    unknown = await editor.client.post(
        url, json={"instruction": "smile less", "selection": {"scene_keys": ["scn_nope"]}}, headers=key()
    )
    assert unknown.status_code == 422 and unknown.json()["issues"][0]["code"] == "unknown_key"
    bad = await editor.client.post(url, json={"operations": [{"op": "teleport"}]}, headers=key())
    assert bad.status_code == 422 and bad.json()["issues"][0]["code"] == "invalid_operation"
    extra = await editor.client.post(url, json={"instruction": "x", "control": {"llm": "ignore"}}, headers=key())
    assert extra.status_code == 422  # unknown fields never pass silently
    assert harness.started == []  # type: ignore[attr-defined]


async def test_a_failed_proposal_names_its_issues_and_can_be_rejected(harness: ApiHarness, editor: ApiTenant) -> None:
    data = two_scene_spec_dict()
    data["locks"] = [{"group": "camera", "scope": {}, "set_by": "user"}]
    sub = await submitted(harness, data)
    response = await editor.client.post(
        f"/v1/versions/{sub.version_id}/edits", json={"instruction": "make the camera slightly handheld"}, headers=key()
    )
    ids = response.json()
    assert (await run_propose(harness, ids["job_id"]))["status"] == "failed"
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert edit["status"] == "failed"
    assert edit["impact"]["issues"][0]["code"] == "locked" and "camera" in edit["impact"]["issues"][0]["message"]
    assert (await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:apply", headers=key())).status_code == 409
    rejected = await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert (await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:reject")).status_code == 409


# ---------------------------------------------------------------------------------- wrappers


async def test_locks_regenerate_and_refusals(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    voice = {"group": "voice", "scope": {"character_keys": ["char_alex"]}}  # the example's lock
    world = {"group": "world", "scope": {"scene_keys": ["scn_hook"]}}
    locks = await editor.client.put(
        f"/v1/versions/{sub.version_id}/locks", json={"locks": [voice, world]}, headers=key()
    )
    assert locks.status_code == 202, locks.text
    accepted = locks.json()
    assert accepted["estimate"] is None and last_start(harness)[0] == "ProposeEditWorkflow"
    result = await run_propose(harness, accepted["job_id"])
    assert result["status"] == "proposed" and result["version_id"] == accepted["new_version_id"]
    locked = await version_row(harness, accepted["new_version_id"])
    assert locked.origin == "lock_change"
    assert [(lock["group"], lock["scope"]["scene_keys"]) for lock in locked.spec["locks"]] == [
        ("voice", None),
        ("world", ["scn_hook"]),
    ]
    same = await editor.client.put(f"/v1/versions/{locked.id}/locks", json={"locks": [voice, world]}, headers=key())
    assert same.status_code == 409
    unknown = await editor.client.put(
        f"/v1/versions/{locked.id}/locks", json={"locks": [{"group": "x"}]}, headers=key()
    )
    assert unknown.status_code == 422

    # A blocked component is refused synchronously, naming the lock; nothing is queued.
    before = len(harness.started)  # type: ignore[attr-defined]
    refused = await editor.client.post(
        f"/v1/versions/{locked.id}/scenes/scn_hook:regenerate", json={"components": ["background"]}, headers=key()
    )
    assert refused.status_code == 409 and "world" in refused.json()["detail"]
    assert refused.json()["issues"][0]["detail"]["groups"] == ["world"]
    assert len(harness.started) == before  # type: ignore[attr-defined]
    # Another scene is not in the lock's scope.
    regen = await editor.client.post(
        f"/v1/versions/{locked.id}/scenes/scn_reveal:regenerate",
        json={"components": ["broll"], "quality_tier": "final"},
        headers=key(),
    )
    assert regen.status_code == 202, regen.text
    out = await run_propose(harness, regen.json()["job_id"])
    assert out["status"] == "proposed", out
    child = await version_row(harness, regen.json()["new_version_id"])
    assert child.origin == "regenerate" and child.spec["meta"]["quality_tier"] == "final"
    assert set(child.spec["generation"]["seed_overrides"]) == {"video.broll:sht_2:t1"}
    assert (
        await editor.client.post(
            f"/v1/versions/{locked.id}/scenes/scn_nope:regenerate", json={"components": ["broll"]}, headers=key()
        )
    ).status_code == 404
    assert (
        await editor.client.post(
            f"/v1/versions/{locked.id}/shots/sht_1:regenerate", json={"components": ["teleport"]}, headers=key()
        )
    ).status_code == 422
    # Removing the lock is the user's call, through the same endpoint.
    unlock = await editor.client.put(f"/v1/versions/{locked.id}/locks", json={"locks": []}, headers=key())
    assert unlock.status_code == 202
    await run_propose(harness, unlock.json()["job_id"])
    assert (await version_row(harness, unlock.json()["new_version_id"])).spec["locks"] == []


async def test_acting_regenerates_as_a_director_edit(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    response = await editor.client.post(
        f"/v1/versions/{sub.version_id}/scenes/scn_reveal:regenerate", json={"components": ["acting"]}, headers=key()
    )
    assert response.status_code == 202, response.text
    ids = response.json()
    async with harness.services.db.session() as session:
        row = await session.get_one(EditProposal, UUID(ids["edit_proposal_id"]))
    assert row.instruction == "Regenerate the acting of the selected scenes" and row.ops == []
    assert row.selection == {"kind": "regenerate", "editor": {"scene_keys": ["scn_reveal"]}}
    out = await run_propose(harness, ids["job_id"])
    assert out["status"] == "proposed", out
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert edit["impact"]["planner"] == "template" and edit["ops"][0]["op"] == "regenerate"
    child = await version_row(harness, ids["new_version_id"])
    assert child.origin == "regenerate"
    assert set(child.spec["generation"]["seed_overrides"]) == {"avatar.render:sht_4:c1:t1"}  # only the reveal
    mixed = await editor.client.post(
        f"/v1/versions/{sub.version_id}/scenes/scn_reveal:regenerate",
        json={"components": ["acting", "music"]},
        headers=key(),
    )
    assert mixed.status_code == 422


async def test_take_selection_and_reroute_derive_versions(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    parent = await version_row(harness, sub.version_id)
    talking = next(s for s in parent.spec["scenes"][0]["shots"] if s["type"] == "talking_head")
    select = await editor.client.post(
        f"/v1/versions/{sub.version_id}/shots/{talking['key']}/takes/tk_2:select", headers=key()
    )
    assert select.status_code == 202, select.text
    assert (await run_propose(harness, select.json()["job_id"]))["status"] == "proposed"
    child = await version_row(harness, select.json()["new_version_id"])
    chosen = next(s for s in child.spec["scenes"][0]["shots"] if s["key"] == talking["key"])
    assert child.origin == "take_select" and chosen["takes"]["selected_take_key"] == "tk_2"
    bad = await editor.client.post(f"/v1/versions/{sub.version_id}/shots/sht_1/takes/nope:select", headers=key())
    assert bad.status_code == 422

    reroute = await editor.client.post(
        f"/v1/versions/{sub.version_id}:reroute",
        json={"node_keys": [f"avatar.render:{talking['key']}:c1:t1"], "reason": "try another engine"},
        headers=key(),
    )
    assert reroute.status_code == 202, reroute.text
    assert (await run_propose(harness, reroute.json()["job_id"]))["status"] == "proposed"
    child = await version_row(harness, reroute.json()["new_version_id"])
    # a re-route without an engine pin changes no spec content: only the build directive
    assert child.origin == "reroute" and child.spec_content_digest == parent.spec_content_digest
    async with harness.services.db.session() as session:
        job = (
            await session.execute(sa.select(GenerationJob).where(GenerationJob.video_version_id == child.id))
        ).scalar_one()
    assert job.input["build"]["force_reroute"] == [f"avatar.render:{talking['key']}:c1:t1"]
    unknown = await editor.client.post(
        f"/v1/versions/{sub.version_id}:reroute", json={"node_keys": ["avatar.render:sht_9:c1:t1"]}, headers=key()
    )
    out = await run_propose(harness, unknown.json()["job_id"])
    async with harness.services.db.session() as session:
        row = await session.get_one(EditProposal, UUID(unknown.json()["edit_proposal_id"]))
    assert out["status"] == "failed" and row.impact["issues"][0]["code"] == "unknown_node"


async def test_the_takes_gallery_lists_ranked_takes(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    async with harness.services.db.transaction() as session:
        for index in (1, 2):
            session.add(
                Take(
                    org_id=ALEX.ORG_ID,
                    version_id=sub.version_id,
                    shot_key="sht_1",
                    take_key=f"tk_{index}",
                    take_index=index,
                    effective_seed=1000 + index,
                    rank=3 - index,
                    selected=index == 2,
                    behavior_signature={"score": 0.5 * index},
                )
            )
    gallery = (await editor.client.get(f"/v1/versions/{sub.version_id}/takes")).json()
    assert [(t["take_key"], t["rank"], t["selected"]) for t in gallery["takes"]] == [
        ("tk_1", 2, False),
        ("tk_2", 1, True),
    ]
    assert gallery["takes"][1]["behavior_signature"] == {"score": 1.0} and gallery["takes"][0]["video"] is None


# ---------------------------------------------------------------------------------- versions


async def test_restore_creates_a_new_current_version(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    response = await editor.client.post(
        f"/v1/versions/{sub.version_id}/edits",
        json={"operations": [{"op": "set_camera", "add_moves": [{"type": "handheld_drift", "scale": 0.6}]}]},
        headers=key(),
    )
    ids = response.json()
    await run_propose(harness, ids["job_id"])
    applied = (await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:apply", headers=key())).json()
    await run_apply(harness, applied["job_id"])
    restored = await editor.client.post(f"/v1/versions/{sub.version_id}:restore", headers=key())
    assert restored.status_code == 202, restored.text
    body = restored.json()
    assert last_start(harness)[0] == "GenerateVersionWorkflow" and body["state"] == "approved"
    original = await version_row(harness, sub.version_id)
    copy = await version_row(harness, body["version_id"])
    assert copy.id not in (original.id, UUID(applied["new_version_id"]))  # a new version, not a pointer move
    assert (copy.origin, copy.parent_version_id, copy.number) == ("restore", original.id, 3)
    assert copy.spec_content_digest == original.spec_content_digest
    assert copy.plan_report_artifact_id == original.plan_report_artifact_id
    assert await current_version(harness, sub.video_id) == copy.id
    async with harness.services.db.session() as session:
        audit = (await session.execute(sa.select(AuditLog).where(AuditLog.action == "video_version.restore"))).scalars()
        assert any(a.target_id == str(copy.id) or str(a.target_id) == str(copy.id) for a in audit)


async def test_branch_and_duplicate(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    branched = await editor.client.post(
        f"/v1/versions/{sub.version_id}:branch", json={"name": "punchier"}, headers=key()
    )
    assert branched.status_code == 202, branched.text
    version = await version_row(harness, branched.json()["version_id"])
    assert (version.branch, version.origin, version.parent_version_id) == ("punchier", "branch", sub.version_id)
    dup_name = await editor.client.post(
        f"/v1/versions/{sub.version_id}:branch", json={"name": "punchier"}, headers=key()
    )
    assert dup_name.status_code == 409
    bad_name = await editor.client.post(f"/v1/versions/{sub.version_id}:branch", json={"name": "No!"}, headers=key())
    assert bad_name.status_code == 422

    duplicated = await editor.client.post(
        f"/v1/videos/{sub.video_id}:duplicate", json={"version_id": str(sub.version_id)}, headers=key()
    )
    assert duplicated.status_code == 202, duplicated.text
    body = duplicated.json()
    assert body["video_id"] != str(sub.video_id)
    copy = await version_row(harness, body["version_id"])
    original = await version_row(harness, sub.version_id)
    assert (copy.number, copy.origin, copy.parent_version_id) == (1, "duplicate", original.id)
    assert copy.spec["generation"]["seed_namespace"] == original.spec["generation"]["seed_namespace"]
    video = (await editor.client.get(f"/v1/videos/{body['video_id']}")).json()
    assert video["title"].endswith("(copy)") and video["current_version_id"] == body["version_id"]
    elsewhere = await submitted(harness)
    wrong = await editor.client.post(
        f"/v1/videos/{sub.video_id}:duplicate", json={"version_id": str(elsewhere.version_id)}, headers=key()
    )
    assert wrong.status_code == 404


async def test_resume_reruns_a_failed_generation_in_place(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    early = await editor.client.post(f"/v1/versions/{sub.version_id}:resume", headers=key())
    assert early.status_code == 409  # approved, not failed
    async with harness.services.db.transaction() as session:
        await rec.set_version_state(session, ALEX.ORG_ID, sub.version_id, VersionState.GENERATING)
        await rec.set_version_state(session, ALEX.ORG_ID, sub.version_id, VersionState.FAILED)
        await rec.set_job(session, ALEX.ORG_ID, sub.job_id, status="failed", progress=1.0)
    resumed = await editor.client.post(f"/v1/versions/{sub.version_id}:resume", headers=key())
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["version_id"] == str(sub.version_id)  # in place: no new version
    workflow, arg, _ = last_start(harness)
    assert workflow == "GenerateVersionWorkflow" and arg["version_id"] == str(sub.version_id)
    again = await editor.client.post(f"/v1/versions/{sub.version_id}:resume", headers=key())
    assert again.status_code == 409  # its job is queued


async def test_variants_and_remix_are_roadmap_items(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    variants = await editor.client.post(f"/v1/versions/{sub.version_id}:variants")
    remix = await editor.client.post(f"/v1/versions/{sub.version_id}:remix", json={"transform": "shorten"})
    assert variants.status_code == remix.status_code == 501
    assert "V1" in variants.json()["detail"]


async def test_viewers_cannot_edit(harness: ApiHarness) -> None:
    sub = await submitted(harness)
    viewer = await harness.add_member(ALEX.ORG_ID, "viewer")
    assert (await viewer.client.get(f"/v1/versions/{sub.version_id}/edits")).status_code == 200
    denied = await viewer.client.post(
        f"/v1/versions/{sub.version_id}/edits", json={"instruction": "smile less"}, headers=key()
    )
    assert denied.status_code == 403
    assert (await viewer.client.post(f"/v1/versions/{sub.version_id}:restore", headers=key())).status_code == 403
    stranger = await harness.new_tenant()  # I12: ids in bodies are tenant-scoped too
    estimate = await stranger.client.post("/v1/estimates", json={"version_id": str(sub.version_id)})
    assert estimate.status_code == 404


async def test_the_vocabulary_lists_editor_tokens_locks_and_components(harness: ApiHarness, editor: ApiTenant) -> None:
    body = (await editor.client.get("/v1/vocabulary")).json()
    assert "skeptical" in body["categories"]["emotion"] and "handheld_drift" in body["categories"]["camera.move_type"]
    groups = {g["group"]: g for g in body["lock_groups"]}
    assert groups["voice"]["pins_routes"] and groups["world"]["scope_fields"] == ["scene_keys"]
    acting = next(c for c in body["regenerate_components"] if c["name"] == "acting")
    assert acting["requires_director"] and acting["blocked_by"] == ["acting"]


async def test_estimates_come_from_the_planned_routes_and_the_proposal(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    whole = (await editor.client.post("/v1/estimates", json={"version_id": str(sub.version_id)})).json()
    assert whole["basis"] == "planned_routes" and whole["nodes"]
    assert {n["node_key"].split(":")[0] for n in whole["nodes"]} >= {"tts.segment", "avatar.render"}
    shot = (
        await editor.client.post(
            "/v1/estimates",
            json={
                "regenerate": {
                    "version_id": str(sub.version_id),
                    "components": ["avatar_video"],
                    "shot_keys": ["sht_4"],
                }
            },
        )
    ).json()
    assert [n["node_key"] for n in shot["nodes"]] == ["avatar.render:sht_4:c1:t1"]
    assert shot["usd"] <= whole["usd"] and shot["seconds"] <= whole["seconds"]
    regen = await editor.client.post(
        f"/v1/versions/{sub.version_id}/shots/sht_4:regenerate", json={"components": ["avatar_video"]}, headers=key()
    )
    assert regen.json()["estimate"] == {"usd": shot["usd"], "seconds": shot["seconds"], "basis": "planned_routes"}
    pending = (
        await editor.client.post("/v1/estimates", json={"edit_proposal_id": regen.json()["edit_proposal_id"]})
    ).json()
    assert pending["pending"] and pending["basis"] == "edit_proposal"
    await run_propose(harness, regen.json()["job_id"])
    done = (
        await editor.client.post("/v1/estimates", json={"edit_proposal_id": regen.json()["edit_proposal_id"]})
    ).json()
    assert not done["pending"]
    both = await editor.client.post(
        "/v1/estimates", json={"version_id": str(sub.version_id), "edit_proposal_id": regen.json()["edit_proposal_id"]}
    )
    assert both.status_code == 422
