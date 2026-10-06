"""Phase 12 memory loop on the Compose infrastructure (mock engines, §18.4 / ADR 0057):

- a finished build writes its usage event with the selected hook's embedding and starts
  `MemoryUpdateWorkflow` (trigger `ready`), which turns the measured habits into observation items
  — proposed, never promoted from mock evidence;
- an authored item is indexed by a `memory_update` job (trigger `embed`) — the API calls no model;
- approval proposes the script's persona facts (trigger `approved`), export activates them
  (trigger `exported`) and writes the `exported` usage event;
- a plan made after embedding ranks memory by embedding (the snapshot records the retriever)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.services import build_services
from ce_api.testing import ApiHarness
from ce_db.models.assets import GenerationJob
from ce_db.models.memory import CreatorMemoryItem, CreatorUsageEvent
from ce_db.models.videos import DirectorRun
from ce_exec.memory_job import enqueue
from ce_exec.studio import StudioContext
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]

TIMEOUT_S = 600


@pytest_asyncio.fixture
async def api(stack: Stack) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(build_services(stack.effective, pool_size=4))
    harness.services.workflows.before_start = None  # the stack runs the orchestrator worker
    yield harness
    await harness.aclose()


async def _built(stack: Stack) -> Any:
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    assert result.state == "ready", (result.failed, result.statuses)
    return submitted


async def _job(stack: Stack, *, trigger: str, target_id: Any) -> GenerationJob:
    for _ in range(300):
        async with stack.exec.db.session() as session:
            job = (
                await session.execute(
                    sa.select(GenerationJob).where(
                        GenerationJob.kind == "memory_update",
                        GenerationJob.target_id == target_id,
                        GenerationJob.input["trigger"].astext == trigger,
                    )
                )
            ).scalars().first()  # fmt: skip
        if job is not None and job.status in ("succeeded", "failed"):
            return job
        await asyncio.sleep(1.0)
    raise AssertionError(f"no finished memory_update ({trigger}) job for {target_id}")


async def _run(stack: Stack, ctx: dict[str, Any]) -> Any:
    from ce_orchestrator.worker import queues

    handle = await stack.temporal.start_workflow(
        "MemoryUpdateWorkflow",
        StudioContext.model_validate(ctx),
        id=f"memory_update-{ctx['job_id']}",
        task_queue=queues(stack.effective).orchestrator,
    )
    return await asyncio.wait_for(handle.result(), TIMEOUT_S)


async def test_ready_writes_the_hook_embedding_and_observation_habits(stack: Stack) -> None:
    submitted = await _built(stack)
    job = await _job(stack, trigger="ready", target_id=submitted.version_id)
    assert job.status == "succeeded", job.error
    async with stack.exec.db.session() as session:
        event = (
            await session.execute(
                sa.select(CreatorUsageEvent).where(
                    CreatorUsageEvent.version_id == submitted.version_id, CreatorUsageEvent.event == "ready"
                )
            )
        ).scalar_one()
        observed = (
            (
                await session.execute(
                    sa.select(CreatorMemoryItem).where(
                        CreatorMemoryItem.creator_id == ALEX.CREATOR_ID,
                        CreatorMemoryItem.source["type"].astext == "observation",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert event.hook_embedding_model and event.hook_embedding_model.startswith("mock_embed:")
    assert len(event.hook_embedding) == stack.effective.settings.embedding_dim
    result = dict(job.output or {})
    assert result["trigger"] == "ready", result
    habits = result["creators"][str(ALEX.CREATOR_ID)]["habits"]
    assert habits and result["creators"][str(ALEX.CREATOR_ID)]["mock_evidence"] is True, result
    for item in observed:  # habits measured on mock engines stay proposed (allow_mock_evidence: false)
        assert item.status == "proposed", item.source
        assert item.source["mock"] is True
        assert str(submitted.video_id) in {e["video_id"] for e in item.source["evidence"]}


async def test_an_authored_item_is_indexed_by_a_job(stack: Stack, api: ApiHarness) -> None:
    editor = await api.add_member(ALEX.ORG_ID, "editor")
    phrase = f"stay curious {uuid.uuid4().hex[:6]}"
    created = await editor.client.post(
        f"/v1/creators/{ALEX.CREATOR_ID}/memory",
        json={"kind": "speech_habit.transition_phrase", "value": {"text": phrase}, "text": phrase},
    )
    assert created.status_code == 201, created.text
    assert created.json()["embedding_model"] is None  # the API never calls a model (§9)
    await asyncio.wait_for(api.services.workflows.handles[-1].result(), TIMEOUT_S)
    item = (await editor.client.get(f"/v1/creators/{ALEX.CREATOR_ID}/memory", params={"q": phrase})).json()["items"][0]
    assert item["embedding_model"].startswith("mock_embed:")


async def test_approval_proposes_persona_facts_and_export_activates_them(stack: Stack) -> None:
    submitted = await _built(stack)
    fact = f"Lisbon {uuid.uuid4().hex[:4]}"
    async with stack.exec.db.transaction() as session:  # the fact-check stage's output of this "plan"
        session.add(
            DirectorRun(
                org_id=ALEX.ORG_ID,
                version_id=submitted.version_id,
                stage="fact_check",
                template_version="v1",
                provider="fixture",
                model="test",
                output={
                    "claims": [],
                    "assertions": [
                        {"segment_key": "seg_1", "subject": "I", "predicate": "live in", "object": fact, "kind": "fact"}
                    ],
                },
                status="succeeded",
            )
        )
    approved = await enqueue(
        stack.exec,
        ALEX.ORG_ID,
        trigger="approved",
        target_type="video_version",
        target_id=submitted.version_id,
        version_id=submitted.version_id,
    )
    assert approved is not None
    out = await _run(stack, approved)
    assert out["status"] == "succeeded" and out["proposed"] >= 1, out
    async with stack.exec.db.session() as session:
        item = (
            await session.execute(sa.select(CreatorMemoryItem).where(CreatorMemoryItem.value["object"].astext == fact))
        ).scalar_one()
    assert item.status == "proposed" and item.source["type"] == "plan"
    assert item.embedding_model is not None  # indexed by the same job
    exported = await enqueue(
        stack.exec,
        ALEX.ORG_ID,
        trigger="exported",
        target_type="video_version",
        target_id=submitted.version_id,
        version_id=submitted.version_id,
    )
    assert exported is not None
    done = await _run(stack, exported)
    assert done["status"] == "succeeded" and str(item.id) in done["activated"], done
    async with stack.exec.db.session() as session:
        refreshed = await session.get_one(CreatorMemoryItem, item.id)
        events = (
            (
                await session.execute(
                    sa.select(CreatorUsageEvent.event).where(CreatorUsageEvent.version_id == submitted.version_id)
                )
            )
            .scalars()
            .all()
        )
    assert refreshed.status == "active"
    assert sorted(events) == ["exported", "ready"]


async def test_repeated_edits_propose_a_preference(stack: Stack) -> None:
    from ce_db.models.videos import EditProposal

    submitted = await _built(stack)
    ops = [{"op": "set_behavior_event", "event_type": "small_smile", "changes": {"intensity_delta": -0.2}}]
    ids = []
    async with stack.exec.db.transaction() as session:
        for n in range(3):
            row = EditProposal(
                org_id=ALEX.ORG_ID,
                version_id=submitted.version_id,
                instruction=f"make her smile less ({n})",
                selection={"kind": "edit"},
                ops=ops,
                status="applied",
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
    ctx = await enqueue(stack.exec, ALEX.ORG_ID, trigger="edit_applied", target_type="edit_proposal", target_id=ids[-1])
    assert ctx is not None
    out = await _run(stack, ctx)
    assert out["status"] == "succeeded" and out["proposed"] == 1, out
    async with stack.exec.db.session() as session:
        pref = (
            await session.execute(
                sa.select(CreatorMemoryItem).where(
                    CreatorMemoryItem.creator_id == ALEX.CREATOR_ID,
                    CreatorMemoryItem.kind == "preference",
                    CreatorMemoryItem.source["edit_proposal_id"].astext == str(ids[-1]),
                )
            )
        ).scalar_one()
    assert pref.status == "proposed"
    assert pref.value["dimension"] == "facial_expression" and pref.value["delta"] < 0
    again = await enqueue(
        stack.exec, ALEX.ORG_ID, trigger="edit_applied", target_type="edit_proposal", target_id=ids[-1]
    )
    assert again is not None
    assert (await _run(stack, again))["proposed"] == 0  # a live preference in that direction is not proposed twice
