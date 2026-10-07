"""Activities of the orchestrator (queue `orchestrator`) and the render worker (queue `render`).

`run_local_node` is registered on both queues: CPU nodes run on the orchestrator, render nodes on
the render worker. Model nodes go through `dispatch_gpu`, which queues a GPU task carrying its
Temporal task token and completes asynchronously when the scheduler reports (§25); it is never
retried by Temporal (`maximum_attempts=1`), because infrastructure retries belong to the scheduler.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Awaitable
from typing import Any
from uuid import UUID

from ce_db import queue
from ce_exec import calibration, consistency_job, editing, jobs, memory_job, planning, qcgate, runtime, screen, studio
from ce_exec.context import ExecServices
from temporalio import activity

from ce_orchestrator.models import (
    AssetValidationInput,
    BeginResult,
    CalibrationInput,
    CompleteInput,
    DeletionInput,
    DispatchInput,
    EditJobInput,
    EditJobResult,
    FailInput,
    FailJobInput,
    FinalizeInput,
    LocalInput,
    MemoryEnqueueInput,
    NodeRef,
    PlanInput,
    PlanResult,
    PlanVideoInput,
    PlanVideoResult,
    PrevizCompleteInput,
    ScreenAnalysisInput,
)

__all__ = ["Activities"]


HEARTBEAT_EVERY_S = 10.0


async def _heartbeating(work: Awaitable[Any]) -> Any:
    """Runs `work` while heartbeating every HEARTBEAT_EVERY_S. Without heartbeats Temporal can neither
    deliver a cancellation to a running node (the build was cancelled, its ffmpeg kept going and wrote
    results afterwards) nor notice a dead worker before the start-to-close timeout (30 min on the
    render queue); the workflow sets `heartbeat_timeout` for these activities (audit C5)."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S)
            if done:
                return task.result()
            activity.heartbeat()
    except asyncio.CancelledError:
        task.cancel()  # ce_render kills its ffmpeg on cancellation
        with contextlib.suppress(BaseException):
            await task
        raise


class Activities:
    def __init__(self, services: ExecServices) -> None:
        self.svc = services

    @activity.defn(name="plan_build")
    async def plan_build(self, inp: PlanInput) -> PlanResult:
        return await runtime.plan_version(
            self.svc,
            UUID(inp.org_id),
            UUID(inp.job_id),
            UUID(inp.version_id),
            preset_ids=inp.preset_ids or None,
            previz=inp.previz,
        )

    @activity.defn(name="plan_video")
    async def plan_video(self, inp: PlanVideoInput) -> PlanVideoResult:
        return PlanVideoResult.model_validate(await planning.run_plan(self.svc, UUID(inp.org_id), UUID(inp.job_id)))

    @activity.defn(name="fail_job")
    async def fail_job(self, inp: FailJobInput) -> None:
        await planning.fail_job(self.svc, UUID(inp.org_id), UUID(inp.job_id), inp.code, inp.message)

    @activity.defn(name="complete_previz")
    async def complete_previz(self, inp: PrevizCompleteInput) -> dict[str, Any]:
        return await planning.complete_previz(
            self.svc,
            UUID(inp.org_id),
            UUID(inp.job_id),
            UUID(inp.version_id),
            inp.statuses,
            inp.outputs,
            cancelled=inp.cancelled,
        )

    @activity.defn(name="propose_edit")
    async def propose_edit(self, inp: EditJobInput) -> EditJobResult:
        result = await editing.propose_edit(self.svc, UUID(inp.org_id), UUID(inp.job_id))
        return EditJobResult.model_validate(result)

    @activity.defn(name="apply_edit")
    async def apply_edit(self, inp: EditJobInput) -> EditJobResult:
        result = await editing.run_apply_job(self.svc, UUID(inp.org_id), UUID(inp.job_id))
        return EditJobResult.model_validate(result)

    @activity.defn(name="begin_node")
    async def begin_node(self, ref: NodeRef) -> BeginResult:
        return await runtime.begin_node(self.svc, ref)

    @activity.defn(name="run_local_node")
    async def run_local_node(self, inp: LocalInput) -> runtime.FinishResult:
        result: runtime.FinishResult = await _heartbeating(runtime.run_node_local(self.svc, inp.ref, inp.begin))
        return result

    @activity.defn(name="dispatch_gpu")
    async def dispatch_gpu(self, inp: DispatchInput) -> dict[str, Any]:
        token = base64.b64encode(activity.info().task_token).decode("ascii")
        fields = await runtime.dispatch_payload(self.svc, inp.ref, inp.begin)
        async with self.svc.db.transaction() as session:
            await queue.enqueue(
                session,
                org_id=UUID(inp.ref.org_id),
                node_id=UUID(inp.begin.node_id),
                attempt_id=UUID(inp.begin.attempt_id or ""),
                capability=fields["capability"],
                model_key=fields["model_key"],
                priority=int(fields["priority"]),
                vram_gb=float(fields["vram_gb"]),
                est_seconds=float(fields["est_seconds"]),
                constraints=fields["constraints"],
                payload={**fields["payload"], "task_token": token},
            )
        activity.raise_complete_async()

    @activity.defn(name="finalize_model_node")
    async def finalize_model_node(self, inp: FinalizeInput) -> runtime.FinishResult:
        return await runtime.finalize_model(self.svc, inp.ref, inp.begin, inp.worker)

    @activity.defn(name="fail_node")
    async def fail_node(self, inp: FailInput) -> None:
        await runtime.fail_node(self.svc, inp.ref, inp.node_id, inp.error, status=inp.status)

    @activity.defn(name="complete_build")
    async def complete_build(self, inp: CompleteInput) -> dict[str, Any]:
        return await runtime.complete_build(
            self.svc,
            UUID(inp.org_id),
            UUID(inp.job_id),
            UUID(inp.version_id),
            inp.statuses,
            cancelled=inp.cancelled,
            render_only=inp.render_only,
        )

    @activity.defn(name="validate_asset")
    async def validate_asset(self, inp: AssetValidationInput) -> dict[str, Any]:
        return await jobs.validate_asset(self.svc, UUID(inp.org_id), UUID(inp.job_id), UUID(inp.asset_id))

    @activity.defn(name="analyze_screen")
    async def analyze_screen(self, inp: ScreenAnalysisInput) -> dict[str, Any]:
        return await screen.analyze_screen(self.svc, UUID(inp.org_id), UUID(inp.job_id), UUID(inp.asset_id))

    @activity.defn(name="calibrate_model")
    async def calibrate_model(self, inp: CalibrationInput) -> dict[str, Any]:
        return await calibration.calibrate_model_job(self.svc, UUID(inp.org_id), UUID(inp.job_id), UUID(inp.model_id))

    @activity.defn(name="delete_target")
    async def delete_target(self, inp: DeletionInput) -> dict[str, Any]:
        if inp.target_type != "memory_item":
            raise ValueError(f"DeletionWorkflow does not handle {inp.target_type} yet")
        return await jobs.tombstone_memory(self.svc, UUID(inp.org_id), UUID(inp.job_id), UUID(inp.target_id))

    # ------------------------------------------------------------------ QC gate (Phase 11, §26)
    @activity.defn(name="qc_gate")
    async def qc_gate(self, inp: qcgate.GateInput) -> qcgate.GateDecision:
        return await qcgate.shot_gate(self.svc, inp)

    @activity.defn(name="accept_outputs")
    async def accept_outputs(self, inp: qcgate.AcceptInput) -> dict[str, Any]:
        return await qcgate.accept_outputs(self.svc, inp)

    @activity.defn(name="enqueue_consistency")
    async def enqueue_consistency(self, inp: CompleteInput) -> dict[str, Any]:
        ctx = await consistency_job.enqueue_after_build(
            self.svc, UUID(inp.org_id), UUID(inp.version_id), str(inp.statuses.get("state", ""))
        )
        return ctx or {}

    @activity.defn(name="enqueue_memory_update")
    async def enqueue_memory_update(self, inp: MemoryEnqueueInput) -> dict[str, Any]:
        """The `memory_update` job (Phase 12) of a finished build (`ready` only) or an applied edit."""
        if inp.trigger == "ready" and inp.state != "ready":
            return {}
        ctx = await memory_job.enqueue(
            self.svc,
            UUID(inp.org_id),
            trigger=inp.trigger,
            target_type=inp.target_type,
            target_id=UUID(inp.target_id),
            version_id=UUID(inp.version_id) if inp.version_id else None,
        )
        return ctx or {}

    # ------------------------------------------------------------------ studio jobs (Phase 10)
    @activity.defn(name="studio_stage")
    async def studio_stage(self, inp: studio.StudioStageInput) -> studio.StudioStep:
        step = await studio.run_stage(self.svc, inp.ctx, inp.stage, inp.data)
        if step.build is not None:  # the Creator Test's video: the regular generation workflow, as a child
            from ce_orchestrator.worker import build_input

            step.build = build_input(
                self.svc.effective,
                org_id=inp.ctx.org_id,
                job_id=str(step.build["job_id"]),
                version_id=str(step.build["version_id"]),
            ).model_dump(mode="json")
        return step

    @activity.defn(name="studio_begin")
    async def studio_begin(self, inp: studio.StudioCallInput) -> studio.CallBegun:
        return await studio.begin_call(self.svc, inp.ctx, inp.call)

    @activity.defn(name="studio_dispatch")
    async def studio_dispatch(self, inp: studio.StudioDispatchInput) -> dict[str, Any]:
        token = base64.b64encode(activity.info().task_token).decode("ascii")
        await studio.dispatch_call(self.svc, inp.ctx, inp.begun, token)
        activity.raise_complete_async()

    @activity.defn(name="studio_record")
    async def studio_record(self, inp: studio.StudioRecordInput) -> dict[str, Any]:
        return await studio.record_call(self.svc, inp.ctx, inp.begun, inp.worker)

    @activity.defn(name="studio_fail")
    async def studio_fail(self, inp: studio.StudioFailInput) -> None:
        await studio.fail_call(self.svc, inp.ctx, inp.node_id, inp.error)
        if inp.node_id is None:
            await studio.fail_job(self.svc, inp.ctx, inp.error)

    def orchestrator(self) -> list[Any]:
        return [
            self.studio_stage,
            self.studio_begin,
            self.studio_dispatch,
            self.studio_record,
            self.studio_fail,
            self.plan_build,
            self.plan_video,
            self.fail_job,
            self.complete_previz,
            self.propose_edit,
            self.apply_edit,
            self.begin_node,
            self.run_local_node,
            self.dispatch_gpu,
            self.finalize_model_node,
            self.fail_node,
            self.qc_gate,
            self.accept_outputs,
            self.enqueue_consistency,
            self.enqueue_memory_update,
            self.complete_build,
            self.validate_asset,
            self.analyze_screen,
            self.calibrate_model,
            self.delete_target,
        ]

    def render(self) -> list[Any]:
        return [self.run_local_node]
