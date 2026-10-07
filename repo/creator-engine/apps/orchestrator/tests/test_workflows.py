"""Workflow tests (§37): the real workflows on the Temporal dev server with fake activities.

Covers ordering and parallelism, the cache short-circuit, the render queue, partial failure
(dependents skipped, independent branches finish), CPU retries vs non-retryable errors, GPU
dispatch without retries, cancellation propagated to running activities, plan failure, resume
on another worker after a worker dies mid-activity, and replay determinism of the histories.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ce_exec.runtime import FinishResult
from ce_orchestrator.models import (
    BeginResult,
    BuildInput,
    CompleteInput,
    DispatchInput,
    EditJobInput,
    EditJobResult,
    FailInput,
    FailJobInput,
    FinalizeInput,
    LocalInput,
    NodeInfo,
    NodeRef,
    PlanInput,
    PlanResult,
    PlanVideoInput,
    PlanVideoResult,
    PrevizCompleteInput,
    TaskQueues,
)
from ce_orchestrator.workflows import (
    WORKFLOWS,
    ApplyEditWorkflow,
    GenerateVersionWorkflow,
    PlanVideoWorkflow,
    ProposeEditWorkflow,
    RenderWorkflow,
)
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

pytestmark = [pytest.mark.infra]


def _node(key: str, queue: str, deps: tuple[str, ...] = (), scene: str | None = None) -> NodeInfo:
    return NodeInfo(key=key, kind=key.split(":")[0], queue=queue, deps=list(deps), scene_key=scene)  # type: ignore[arg-type]


VOICE = "voice.prepare:c1"
TTS1, TTS2 = "tts.segment:seg_1", "tts.segment:seg_2"
AVATAR, CAMERA, KEYFRAME = "avatar.render:sht_1", "post.camera:sht_1", "image.keyframe:sht_2"
MIX, RENDER, SIGN, QC = "audio.mix", "render.final:tiktok", "provenance.sign:tiktok", "qc.final:tiktok"


def example_plan() -> PlanResult:
    nodes = [
        _node(VOICE, "orchestrator"),
        _node(TTS1, "gpu", (VOICE,), "sc_1"),
        _node(AVATAR, "gpu", (TTS1,), "sc_1"),
        _node(CAMERA, "orchestrator", (AVATAR,), "sc_1"),
        _node(TTS2, "gpu", (VOICE,), "sc_2"),
        _node(KEYFRAME, "orchestrator", (), "sc_2"),
        _node(MIX, "orchestrator", (TTS1, TTS2)),
        _node(RENDER, "render", (MIX, CAMERA, KEYFRAME)),
        _node(SIGN, "orchestrator", (RENDER,)),
        _node(QC, "orchestrator", (RENDER,)),
    ]
    return PlanResult(
        graph_sha="g" * 64,
        nodes=nodes,
        pre=[VOICE],
        scenes={"sc_1": [TTS1, AVATAR, CAMERA], "sc_2": [TTS2, KEYFRAME]},
        post=[MIX, RENDER, SIGN, QC],
    )


class FakeActivities:
    """Activity implementations registered under the production activity names."""

    def __init__(
        self,
        plan: PlanResult | None,
        *,
        cached: set[str] | None = None,
        fail: set[str] | None = None,
        flaky: dict[str, int] | None = None,
        block_first: set[str] | None = None,
        delay_s: float = 0.02,
        broken_bookkeeping: bool = False,
        slow: dict[str, float] | None = None,
    ) -> None:
        self.slow = slow or {}
        self.spans: dict[str, tuple[float, float]] = {}
        self.plan = plan
        self.broken_bookkeeping = broken_bookkeeping
        self.cached = cached or set()
        self.fail = fail or set()
        self.flaky = flaky or {}
        self.block_first = block_first or set()
        self.delay_s = delay_s
        self.attempts: Counter[str] = Counter()
        self.queues: dict[str, str] = {}
        self.upstreams: dict[str, dict[str, str]] = {}
        self.failed_nodes: list[FailInput] = []
        self.completed: list[CompleteInput] = []
        self.cancelled: list[str] = []
        self.blocking = asyncio.Event()
        self.running = 0
        self.max_running = 0
        self.plan_inputs: list[PlanInput] = []
        self.planned: PlanVideoResult | None = None
        self.plan_crash: str | None = None  # plan_video raises this (a non-retryable error type name)
        self.failed_jobs: list[FailJobInput] = []
        self.previz_completed: list[PrevizCompleteInput] = []
        self.edit_result: EditJobResult | None = None
        self.edit_calls: list[tuple[str, str]] = []
        self.accepted: list[Any] = []
        self.memory: list[Any] = []  # `memory_update` enqueues (Phase 12)

    @activity.defn(name="plan_build")
    async def plan_build(self, inp: PlanInput) -> PlanResult:
        self.plan_inputs.append(inp)
        if self.plan is None:
            raise ValueError("the spec does not compile")
        return self.plan

    @activity.defn(name="begin_node")
    async def begin_node(self, ref: NodeRef) -> BeginResult:
        key = ref.node_key
        self.upstreams[key] = dict(ref.upstream)
        if key in self.cached:
            return BeginResult(status="cached", cache_key=f"ck:{key}", node_id=f"n:{key}", sha=f"cached:{key}")
        info = next(n for n in self.plan.nodes if n.key == key) if self.plan else None
        if info is not None and info.queue == "gpu":
            return BeginResult(status="dispatch", cache_key=f"ck:{key}", node_id=f"n:{key}")
        return BeginResult(status="local", cache_key=f"ck:{key}", node_id=f"n:{key}")

    async def _work(self, key: str) -> None:
        self.attempts[key] += 1
        self.queues[key] = activity.info().task_queue
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if key in self.block_first and self.attempts[key] == 1:
                self.blocking.set()
                try:
                    while True:
                        activity.heartbeat()
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    self.cancelled.append(key)
                    raise
            started = asyncio.get_running_loop().time()
            await asyncio.sleep(self.slow.get(key, self.delay_s))
            self.spans[key] = (started, asyncio.get_running_loop().time())
        finally:
            self.running -= 1

    @activity.defn(name="run_local_node")
    async def run_local_node(self, inp: LocalInput) -> FinishResult:
        key = inp.ref.node_key
        await self._work(key)
        if key in self.fail:
            raise ValueError(f"{key}: invalid input")  # non-retryable in CPU_RETRY
        if self.attempts[key] <= self.flaky.get(key, 0):
            raise RuntimeError(f"{key}: transient")
        return FinishResult(sha=f"out:{key}")

    @activity.defn(name="dispatch_gpu")
    async def dispatch_gpu(self, inp: DispatchInput) -> dict[str, Any]:
        key = inp.ref.node_key
        await self._work(key)
        if key in self.fail:
            raise RuntimeError(f"{key}: worker lost")  # retryable type, but dispatch has NO_RETRY
        return {"outputs": [f"blob:{key}"]}

    @activity.defn(name="finalize_model_node")
    async def finalize_model_node(self, inp: FinalizeInput) -> FinishResult:
        assert inp.worker["outputs"] == [f"blob:{inp.ref.node_key}"]
        return FinishResult(sha=f"out:{inp.ref.node_key}")

    @activity.defn(name="fail_node")
    async def fail_node(self, inp: FailInput) -> None:
        self.failed_nodes.append(inp)
        if self.broken_bookkeeping:
            raise ValueError("the error row cannot be written")  # non-retryable: fails at once

    @activity.defn(name="complete_build")
    async def complete_build(self, inp: CompleteInput) -> dict[str, Any]:
        self.completed.append(inp)
        failed = sorted(k for k, s in inp.statuses.items() if s in ("failed", "skipped"))
        rendered = any(
            k.startswith("provenance.sign:") and s in ("succeeded", "cached") for k, s in inp.statuses.items()
        )
        if inp.cancelled:
            state, job = "cancelled", "cancelled"
        elif not failed:
            state, job = "ready", "succeeded"
        else:
            state, job = ("partial", "partial") if rendered else ("failed", "failed")
        return {"state": state, "job_status": job, "failed": failed}

    @activity.defn(name="plan_video")
    async def plan_video(self, inp: PlanVideoInput) -> PlanVideoResult:
        if self.plan_crash is not None:
            raise ApplicationError("no route for voice.tts: language az not supported", type=self.plan_crash)
        assert self.planned is not None
        return self.planned

    @activity.defn(name="fail_job")
    async def fail_job(self, inp: FailJobInput) -> None:
        self.failed_jobs.append(inp)

    @activity.defn(name="complete_previz")
    async def complete_previz(self, inp: PrevizCompleteInput) -> dict[str, Any]:
        self.previz_completed.append(inp)
        failed = sorted(k for k, s in inp.statuses.items() if s in ("failed", "skipped"))
        return {"state": "failed" if failed else "previz_ready", "failed": failed}

    @activity.defn(name="propose_edit")
    async def propose_edit(self, inp: EditJobInput) -> EditJobResult:
        self.edit_calls.append(("propose", inp.job_id))
        assert self.edit_result is not None
        return self.edit_result

    @activity.defn(name="apply_edit")
    async def apply_edit(self, inp: EditJobInput) -> EditJobResult:
        self.edit_calls.append(("apply", inp.job_id))
        assert self.edit_result is not None
        return self.edit_result

    @activity.defn(name="qc_gate")
    async def qc_gate(self, inp: Any) -> dict[str, Any]:
        return {"action": "pass", "step": "pass"}  # the QC gate itself is tested in tests/e2e/test_qc_gate_mock.py

    @activity.defn(name="accept_outputs")
    async def accept_outputs(self, inp: Any) -> dict[str, Any]:
        self.accepted.append(inp)
        return {}

    @activity.defn(name="enqueue_consistency")
    async def enqueue_consistency(self, inp: CompleteInput) -> dict[str, Any]:
        return {}  # no consistency job in these tests

    @activity.defn(name="enqueue_memory_update")
    async def enqueue_memory_update(self, inp: Any) -> dict[str, Any]:
        self.memory.append(inp)
        return {}  # the memory loop runs end to end in tests/e2e/test_memory_loop_mock.py

    def activities(self) -> list[Any]:
        return [
            self.qc_gate,
            self.accept_outputs,
            self.enqueue_consistency,
            self.enqueue_memory_update,
            self.propose_edit,
            self.apply_edit,
            self.plan_video,
            self.fail_job,
            self.complete_previz,
            self.plan_build,
            self.begin_node,
            self.run_local_node,
            self.dispatch_gpu,
            self.finalize_model_node,
            self.fail_node,
            self.complete_build,
        ]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"), data_converter=pydantic_data_converter
    )
    yield WorkflowEnvironment.from_client(client)


def _build(queue: str, *, max_parallel: int = 6) -> BuildInput:
    return BuildInput(
        org_id=str(uuid.uuid4()),
        job_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        queues=TaskQueues(orchestrator=queue, render=f"{queue}-render"),
        max_parallel=max_parallel,
        cpu_timeout_s=10,
        render_timeout_s=10,
        model_timeout_s=30,
    )


@contextlib.asynccontextmanager
async def _workers(client: Client, fake: FakeActivities, queue: str, **options: Any) -> AsyncIterator[None]:
    async with (
        Worker(client, task_queue=queue, workflows=WORKFLOWS, activities=fake.activities(), **options),
        Worker(client, task_queue=f"{queue}-render", activities=[fake.run_local_node]),
    ):
        yield


async def _generate(client: Client, build: BuildInput) -> Any:
    return await client.execute_workflow(
        GenerateVersionWorkflow.run, build, id=f"wf-test-{uuid.uuid4()}", task_queue=build.queues.orchestrator
    )


async def _replay(client: Client, workflow_id: str) -> None:
    replayer = Replayer(workflows=WORKFLOWS, data_converter=pydantic_data_converter)
    await replayer.replay_workflow(await client.get_workflow_handle(workflow_id).fetch_history())


async def test_a_build_runs_every_node_in_order_and_replays_deterministically(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), cached={KEYFRAME})
    build = _build(queue)
    workflow_id = f"wf-test-{uuid.uuid4()}"
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(GenerateVersionWorkflow.run, build, id=workflow_id, task_queue=queue)
    assert result.state == "ready" and result.failed == []
    assert result.statuses[KEYFRAME] == "cached" and fake.attempts[KEYFRAME] == 0  # a cache hit runs nothing
    assert all(result.statuses[n.key] == "succeeded" for n in example_plan().nodes if n.key != KEYFRAME)
    assert result.outputs[RENDER] == f"out:{RENDER}" and result.outputs[KEYFRAME] == f"cached:{KEYFRAME}"
    # every node saw exactly its dependencies' outputs, including across scene boundaries
    assert fake.upstreams[RENDER] == {MIX: f"out:{MIX}", CAMERA: f"out:{CAMERA}", KEYFRAME: f"cached:{KEYFRAME}"}
    assert fake.upstreams[TTS2] == {VOICE: f"out:{VOICE}"}
    assert fake.queues[RENDER] == f"{queue}-render" and fake.queues[MIX] == queue
    assert len(fake.completed) == 1 and not fake.completed[0].cancelled
    # Phase 12: a finished build asks for its memory_update job (trigger `ready`, §18.4)
    assert [(m["trigger"], m["target_id"]) for m in fake.memory] == [("ready", build.version_id)]
    await _replay(env.client, workflow_id)
    await _replay(env.client, f"{workflow_id}:scene:sc_1")


async def test_a_failed_node_skips_its_dependents_and_independent_branches_finish(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), fail={AVATAR})
    async with _workers(env.client, fake, queue):
        result = await _generate(env.client, _build(queue))
    assert result.statuses[AVATAR] == "failed" and fake.attempts[AVATAR] == 1  # dispatch is never retried
    assert {result.statuses[k] for k in (CAMERA, RENDER, SIGN, QC)} == {"skipped"}
    assert result.statuses[TTS2] == result.statuses[MIX] == result.statuses[KEYFRAME] == "succeeded"
    assert fake.attempts[CAMERA] == fake.attempts[RENDER] == 0
    assert [f.ref.node_key for f in fake.failed_nodes] == [AVATAR] and fake.failed_nodes[0].node_id == f"n:{AVATAR}"
    assert "worker lost" in fake.failed_nodes[0].error
    assert result.state == "failed" and set(result.failed) == {AVATAR, CAMERA, RENDER, SIGN, QC}


async def test_a_failure_after_the_render_leaves_a_partial_version(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), fail={QC})
    async with _workers(env.client, fake, queue):
        result = await _generate(env.client, _build(queue))
    assert result.state == "partial" and result.failed == [QC]
    assert result.statuses[SIGN] == "succeeded" and result.outputs[SIGN] == f"out:{SIGN}"


async def test_cpu_nodes_retry_transient_errors_but_not_invalid_input(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), flaky={MIX: 2}, fail={KEYFRAME})
    async with _workers(env.client, fake, queue):
        result = await _generate(env.client, _build(queue))
    assert result.statuses[MIX] == "succeeded" and fake.attempts[MIX] == 3
    assert result.statuses[KEYFRAME] == "failed" and fake.attempts[KEYFRAME] == 1
    assert result.statuses[RENDER] == "skipped" and result.state == "failed"


async def test_a_failing_bookkeeping_activity_still_closes_the_build(env: WorkflowEnvironment) -> None:
    """Regression (audit C1): when `fail_node` itself failed, the exception escaped the DAG and the
    workflow failed without `complete_build`, leaving the job "running" and the version "generating"
    for good (nothing reconciles them)."""
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), fail={AVATAR}, broken_bookkeeping=True)
    workflow_id = f"wf-test-{uuid.uuid4()}"
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(
            GenerateVersionWorkflow.run, _build(queue), id=workflow_id, task_queue=queue
        )
    assert len(fake.completed) == 1  # the job and the version got their final state
    assert result.state == "failed" and result.statuses[AVATAR] == "failed"
    assert {result.statuses[k] for k in (CAMERA, RENDER, SIGN, QC)} == {"skipped"}
    assert result.statuses[TTS2] == result.statuses[MIX] == "succeeded"  # independent branches finished
    await _replay(env.client, f"{workflow_id}:scene:sc_1")


async def test_a_render_whose_plan_fails_still_closes_its_job(env: WorkflowEnvironment) -> None:
    """Regression (audit C2): RenderWorkflow called plan_build without handling its failure, so an
    unknown preset failed the workflow and the render job never left "running"."""
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(None)  # plan_build raises a non-retryable ValueError
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(
            RenderWorkflow.run, _build(queue), id=f"wf-test-{uuid.uuid4()}", task_queue=queue
        )
    assert result.state == "failed" and result.failed == ["plan"]
    assert len(fake.completed) == 1 and fake.completed[0].render_only


async def test_side_nodes_run_alongside_the_scenes(env: WorkflowEnvironment) -> None:
    """Audit P4: a video-level node with no dependencies that no scene needs (SFX) ran before the
    scenes, so no scene started until it finished; now it runs alongside them."""
    sfx = "audio.sfx:boom"
    plan = example_plan()
    plan.nodes.append(_node(sfx, "gpu"))
    mix = next(n for n in plan.nodes if n.key == MIX)
    mix.deps.append(sfx)
    plan.side = [sfx]
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(plan, slow={sfx: 1.5})
    async with _workers(env.client, fake, queue):
        result = await _generate(env.client, _build(queue))
    assert result.state == "ready" and result.statuses[sfx] == "succeeded"
    assert fake.spans[TTS1][0] < fake.spans[sfx][1]  # a scene started before the SFX was done
    assert fake.spans[MIX][0] >= fake.spans[sfx][1]  # and what needs the SFX still waited for it


async def test_parallelism_is_bounded(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    keys = [f"image.generate:k{i}" for i in range(6)]
    plan = PlanResult(graph_sha="g" * 64, nodes=[_node(k, "orchestrator") for k in keys], pre=keys, scenes={}, post=[])
    fake = FakeActivities(plan, delay_s=0.3)
    async with _workers(env.client, fake, queue):
        result = await _generate(env.client, _build(queue, max_parallel=2))
    assert result.state == "ready" and fake.max_running == 2


async def test_a_plan_failure_fails_the_build(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(None)
    async with _workers(env.client, fake, queue):
        result = await _generate(env.client, _build(queue))
    assert result.state == "failed" and result.failed == ["plan"]
    assert "does not compile" in result.statuses["plan"]
    assert fake.completed[0].statuses == {"plan": "failed"}


async def test_cancellation_reaches_running_activities_and_closes_the_build(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), block_first={AVATAR})
    build = _build(queue)
    async with _workers(env.client, fake, queue):
        handle = await env.client.start_workflow(
            GenerateVersionWorkflow.run, build, id=f"wf-test-{uuid.uuid4()}", task_queue=queue
        )
        await asyncio.wait_for(fake.blocking.wait(), 30)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as failure:
            await handle.result()
        assert isinstance(failure.value.cause, CancelledError)
        for _ in range(100):  # cancellation is delivered to the activity on its next heartbeat
            if fake.cancelled:
                break
            await asyncio.sleep(0.1)
    assert fake.cancelled == [AVATAR]
    assert len(fake.completed) == 1 and fake.completed[0].cancelled
    assert fake.attempts[CAMERA] == fake.attempts[RENDER] == 0


async def test_a_build_resumes_on_another_worker_after_its_worker_dies(env: WorkflowEnvironment) -> None:
    """Worker A runs without a workflow cache (every task replays the history) and is shut down
    while an activity runs; worker B picks the workflow up, retries only that activity and
    finishes. Nodes completed on A are not run again."""
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan(), block_first={MIX})
    build = _build(queue)
    workflow_id = f"wf-test-{uuid.uuid4()}"
    async with _workers(env.client, fake, queue, max_cached_workflows=0):
        handle = await env.client.start_workflow(GenerateVersionWorkflow.run, build, id=workflow_id, task_queue=queue)
        await asyncio.wait_for(fake.blocking.wait(), 30)
    assert fake.cancelled == [MIX]  # worker A's shutdown interrupted the activity
    before = dict(fake.attempts)
    async with _workers(env.client, fake, queue):
        result = await asyncio.wait_for(handle.result(), 60)
    assert result.state == "ready"
    assert fake.attempts[MIX] == 2
    assert all(fake.attempts[k] == before[k] for k in before if k != MIX)
    await _replay(env.client, workflow_id)


def previz_plan() -> PlanResult:
    """What `plan_version(previz=True)` returns: TTS, verification, alignment and a keyframe."""
    align = "align.segment:seg_1"
    nodes = [
        _node(VOICE, "orchestrator"),
        _node(TTS1, "gpu", (VOICE,), "sc_1"),
        _node(align, "orchestrator", (TTS1,), "sc_1"),
        _node(KEYFRAME, "orchestrator", (), "sc_2"),
    ]
    return PlanResult(
        graph_sha="p" * 64,
        nodes=nodes,
        pre=[VOICE],
        scenes={"sc_1": [TTS1, align], "sc_2": [KEYFRAME]},
        post=[],
        manifest=False,
    )


async def test_planning_runs_previz_as_a_child_and_replays(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(previz_plan())
    build = _build(queue)
    previz_job = str(uuid.uuid4())
    fake.planned = PlanVideoResult(status="succeeded", version_id=build.version_id, previz_job_id=previz_job)
    workflow_id = f"plan-{build.job_id}"
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(PlanVideoWorkflow.run, build, id=workflow_id, task_queue=queue)
    assert result["plan"]["status"] == "succeeded"
    assert result["previz"] == {"state": "previz_ready", "failed": []}
    (plan_input,) = fake.plan_inputs
    assert plan_input.previz and plan_input.job_id == previz_job and plan_input.version_id == build.version_id
    (done,) = fake.previz_completed
    assert done.job_id == previz_job and not done.cancelled
    assert set(done.outputs) == {"align.segment:seg_1"}  # measured word timings come from alignment
    assert fake.completed == []  # previz never closes a build
    await _replay(env.client, workflow_id)
    await _replay(env.client, f"previz-{previz_job}")


async def test_a_failed_plan_starts_no_previz(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(previz_plan())
    fake.planned = PlanVideoResult(status="failed")
    build = _build(queue)
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(
            PlanVideoWorkflow.run, build, id=f"plan-{build.job_id}", task_queue=queue
        )
    assert result == {"plan": {"status": "failed", "version_id": None, "previz_job_id": None}, "previz": None}
    assert fake.plan_inputs == [] and fake.previz_completed == []


async def test_a_crashed_plan_activity_still_fails_the_plan_job(env: WorkflowEnvironment) -> None:
    """Audit PLAN-FAIL: planning in a language no voice engine speaks raised GraphError out of the
    activity; the workflow failed in a second and the plan job stayed `running` at 5 % forever."""
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(previz_plan())
    fake.plan_crash = "GraphError"
    build = _build(queue)
    workflow_id = f"plan-{build.job_id}"
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(PlanVideoWorkflow.run, build, id=workflow_id, task_queue=queue)
    assert result["plan"]["status"] == "failed" and result["previz"] is None
    (failed,) = fake.failed_jobs
    assert failed.job_id == build.job_id and failed.code == "planning_failed"
    assert "language az not supported" in failed.message
    assert fake.plan_inputs == []
    await _replay(env.client, workflow_id)


async def test_a_failed_previz_node_fails_the_previz(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(previz_plan(), fail={TTS1})
    build = _build(queue)
    fake.planned = PlanVideoResult(status="succeeded", version_id=build.version_id, previz_job_id=str(uuid.uuid4()))
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(
            PlanVideoWorkflow.run, build, id=f"plan-{build.job_id}", task_queue=queue
        )
    assert result["previz"]["state"] == "failed"
    assert set(result["previz"]["failed"]) == {TTS1, "align.segment:seg_1"}


async def test_an_auto_applied_edit_generates_the_derived_version_and_replays(env: WorkflowEnvironment) -> None:
    """A thin wrapper (regenerate, take selection, locks, re-route) proposes and applies in one job;
    the derived version of an approved parent is generated as a child workflow (§28, §12.8)."""
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan())
    build = _build(queue)
    child_version, child_job = str(uuid.uuid4()), str(uuid.uuid4())
    fake.edit_result = EditJobResult(
        status="proposed",
        edit_proposal_id=str(uuid.uuid4()),
        version_id=child_version,
        next_job_id=child_job,
        next_kind="generate",
    )
    workflow_id = f"edit_propose-{build.job_id}"
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(ProposeEditWorkflow.run, build, id=workflow_id, task_queue=queue)
    assert fake.edit_calls == [("propose", build.job_id)]
    assert result["build"] == {"kind": "generate", "state": "ready", "failed": []}
    (plan_input,) = fake.plan_inputs
    assert (plan_input.job_id, plan_input.version_id, plan_input.previz) == (child_job, child_version, False)
    (done,) = fake.completed
    assert done.job_id == child_job
    await _replay(env.client, workflow_id)
    await _replay(env.client, f"generate-{child_job}")


async def test_a_proposal_alone_builds_nothing(env: WorkflowEnvironment) -> None:
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(example_plan())
    build = _build(queue)
    fake.edit_result = EditJobResult(status="proposed", edit_proposal_id=str(uuid.uuid4()))
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(
            ProposeEditWorkflow.run, build, id=f"edit_propose-{build.job_id}", task_queue=queue
        )
    assert result["build"] is None and result["proposal"]["status"] == "proposed"
    assert fake.plan_inputs == [] and fake.completed == []


async def test_applying_an_edit_to_an_unapproved_version_runs_previz(env: WorkflowEnvironment) -> None:
    """Approval always applies to an up-to-date previz: a derived version of a version that never
    passed approval is previzualized, not generated (§12.8)."""
    queue = f"wf-{uuid.uuid4()}"
    fake = FakeActivities(previz_plan())
    build = _build(queue)
    child_version, child_job = str(uuid.uuid4()), str(uuid.uuid4())
    fake.edit_result = EditJobResult(
        status="succeeded", version_id=child_version, next_job_id=child_job, next_kind="previz"
    )
    workflow_id = f"edit_apply-{build.job_id}"
    async with _workers(env.client, fake, queue):
        result = await env.client.execute_workflow(ApplyEditWorkflow.run, build, id=workflow_id, task_queue=queue)
    assert fake.edit_calls == [("apply", build.job_id)]
    assert result["build"] == {"kind": "previz", "state": "previz_ready", "failed": []}
    (plan_input,) = fake.plan_inputs
    assert plan_input.previz and plan_input.version_id == child_version
    assert len(fake.previz_completed) == 1 and fake.completed == []
    await _replay(env.client, workflow_id)
