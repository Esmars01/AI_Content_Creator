"""Phase 6 DoD on the Compose infrastructure: "make him more skeptical" on a generated video, through
`ProposeEditWorkflow` and `ApplyEditWorkflow` with the real workers. Only the edited scene's
behavior and performance re-run (the voice lock keeps the delivered audio; the hook scene is all
cache hits); comparing the versions shows the acting and CBS differences of that scene only; and
restoring the original is a new version whose build is all cache hits with the same render."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from ce_core.edit.diff import area_of, spec_diff
from ce_core.enums import JobKind, VersionOrigin
from ce_core.ids import new_id
from ce_core.spec.videospec import VideoSpec
from ce_db import execution as rec
from ce_db.models.assets import ExecutionNode, GenerationJob
from ce_db.models.videos import EditProposal, Render, VideoVersion
from ce_db.versions import insert_derived_version
from ce_exec.parents import artifact_shas
from ce_orchestrator.models import BuildResult
from ce_orchestrator.worker import build_input, queues
from ce_testing.fixtures import ALEX, two_scene_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra, pytest.mark.behavior]

TIMEOUT_S = 300


async def _generate(stack: Stack) -> Any:
    spec = two_scene_spec_dict()  # the voice is locked for char_alex
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    assert result.state == "ready", (result.failed, result.statuses)
    return submitted, result


async def _job(stack: Stack, kind: JobKind, version_id: UUID, data: dict[str, Any]) -> UUID:
    async with stack.exec.db.transaction() as session:
        job = GenerationJob(
            org_id=ALEX.ORG_ID,
            kind=kind.value,
            status="queued",
            target_type="video_version",
            target_id=version_id,
            video_version_id=version_id,
            input=data,
        )
        session.add(job)
        await session.flush()
        job.temporal_workflow_id = f"{kind.value}-{job.id}"
        return job.id


async def _run(stack: Stack, workflow: str, job_id: UUID, version_id: UUID, result_type: Any = dict) -> Any:
    arg = build_input(stack.effective, org_id=str(ALEX.ORG_ID), job_id=str(job_id), version_id=str(version_id))
    handle = await stack.temporal.start_workflow(
        workflow,
        arg,
        id=f"{workflow}-{job_id}",
        task_queue=queues(stack.effective).orchestrator,
        result_type=result_type,
    )
    return await asyncio.wait_for(handle.result(), TIMEOUT_S)


async def _statuses(stack: Stack, job_id: UUID) -> dict[str, str]:
    async with stack.exec.db.session() as session:
        rows = await session.execute(
            sa.select(ExecutionNode.node_key, ExecutionNode.status).where(ExecutionNode.job_id == job_id)
        )
        return {k: s for k, s in rows.all()}


async def _resolved(stack: Stack, version_id: UUID) -> dict[str, Any]:
    """The version's CBS per scene, read from its build outputs."""
    async with stack.exec.db.session() as session:
        rows = await rec.load_manifest_rows(session, ALEX.ORG_ID, version_id)
        shas = await artifact_shas(
            session,
            ALEX.ORG_ID,
            {r["node_key"]: r["artifact_id"] for r in rows if r["node_key"].startswith("behavior.resolve:")},
        )
    out = {}
    for key, sha in shas.items():
        out[key.split(":", 1)[1]] = (await stack.exec.docs.output(sha)).data["content"]
    return out


async def _render_artifact(stack: Stack, version_id: UUID) -> UUID | None:
    async with stack.exec.db.session() as session:
        return (
            await session.execute(
                sa.select(Render.artifact_id).where(Render.version_id == version_id, Render.is_proxy.is_(False))
            )
        ).scalar_one()


async def test_make_him_more_skeptical_then_compare_and_restore(stack: Stack) -> None:
    v1, _ = await _generate(stack)

    # 1. propose (the edit stage with the fixture LLM; the selection scopes "him" to the reveal)
    async with stack.exec.db.transaction() as session:
        proposal = EditProposal(
            org_id=ALEX.ORG_ID,
            version_id=v1.version_id,
            instruction="make him more skeptical",
            selection={"kind": "edit", "editor": {"scene_keys": ["scn_reveal"]}},
            status="proposing",
        )
        session.add(proposal)
        await session.flush()
        proposal_id = proposal.id
    propose_job = await _job(
        stack, JobKind.EDIT_PROPOSE, v1.version_id, {"edit_proposal_id": str(proposal_id), "auto_apply": False}
    )
    proposed = await _run(stack, "ProposeEditWorkflow", propose_job, v1.version_id)
    assert proposed["proposal"]["status"] == "proposed" and proposed["build"] is None
    async with stack.exec.db.session() as session:
        row = await session.get_one(EditProposal, proposal_id)
    assert [op["op"] for op in row.ops] == ["set_acting", "add_behavior_event"]
    predicted = {*row.impact["regenerate"], *row.impact["cascade"]}
    assert not {k for k in predicted if k.startswith(("tts.", "asr.", "align."))}  # the voice lock
    assert any(b.get("group") == "voice" for b in row.impact["locks_blocking"])

    # 2. apply → the derived version is generated (its parent passed approval)
    child_id = new_id()
    apply_job = await _job(
        stack,
        JobKind.EDIT_APPLY,
        v1.version_id,
        {"edit_proposal_id": str(proposal_id), "new_version_id": str(child_id), "alternative": None},
    )
    applied = await _run(stack, "ApplyEditWorkflow", apply_job, v1.version_id)
    assert applied["apply"]["version_id"] == str(child_id) and applied["apply"]["next_kind"] == "generate"
    assert applied["build"] == {"kind": "generate", "state": "ready", "failed": []}
    statuses = await _statuses(stack, UUID(applied["apply"]["next_job_id"]))
    ran = {k for k, s in statuses.items() if s == "succeeded"}
    assert set(statuses.values()) <= {"succeeded", "cached"}
    assert {"behavior.resolve:scn_reveal", "avatar.render:sht_4:c1:t1"} <= ran  # the reveal is re-performed
    assert not {k for k in ran if k.startswith(("tts.", "asr.", "align.", "voice."))}  # the delivered audio is kept
    hook = {k for k in statuses if k.endswith((":scn_hook", ":sht_1")) or ":sht_1:" in k}
    assert hook and all(statuses[k] == "cached" for k in hook), {k: statuses[k] for k in hook}
    assert ran <= predicted | {k for k in ran if k.startswith(("qc.", "behavior.observe", "provenance.", "render."))}

    # 3. compare: acting differences of the reveal only, in the spec and in the CBS
    async with stack.exec.db.session() as session:
        parent_row = await session.get_one(VideoVersion, v1.version_id)
        child_row = await session.get_one(VideoVersion, child_id)
    assert (child_row.state, child_row.origin, child_row.parent_version_id) == ("ready", "edit", v1.version_id)
    changed = spec_diff(
        VideoSpec.model_validate(parent_row.spec).content_dict(),
        VideoSpec.model_validate(child_row.spec).content_dict(),
    )
    assert changed and all(e.path.startswith(("/scenes[scn_reveal]", "/intent")) for e in changed)
    assert {area_of(e.path) for e in changed} <= {"acting", "intent"}
    cbs_a, cbs_b = await _resolved(stack, v1.version_id), await _resolved(stack, child_id)
    assert cbs_a["scn_hook"] == cbs_b["scn_hook"] and cbs_a["scn_reveal"] != cbs_b["scn_reveal"]

    # 4. restore v1: a new version, every node a cache hit, the same render
    async with stack.exec.db.transaction() as session:
        restored, job = await insert_derived_version(
            session,
            ALEX.ORG_ID,
            source_version_id=v1.version_id,
            new_version_id=new_id(),
            document=dict(parent_row.spec),
            origin=VersionOrigin.RESTORE,
            requested_by=None,
        )
        restored_id, restore_job = restored.id, job.id
    result: BuildResult = await _run(stack, "GenerateVersionWorkflow", restore_job, restored_id, BuildResult)
    assert result.state == "ready"
    assert set(result.statuses.values()) == {"cached"}, {k: v for k, v in result.statuses.items() if v != "cached"}
    assert await _render_artifact(stack, restored_id) == await _render_artifact(stack, v1.version_id)
    async with stack.exec.db.session() as session:
        assert (await session.get_one(VideoVersion, restored_id)).number == 3
