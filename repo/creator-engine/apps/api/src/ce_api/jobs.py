"""Jobs are Temporal workflows (§9, ADR 0031): the API writes the `generation_jobs` row in the
request's transaction and starts the job's workflow after the commit. The workflow (on the
orchestrator) does the work and reports `job.updated` events; the API never runs job work itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_config.settings import EffectiveConfig
from ce_core.enums import JobKind
from ce_db.models.assets import GenerationJob
from ce_obs import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.events import EventType

__all__ = ["WORKFLOW_OF", "WorkflowStarter", "create_job", "job_event", "start_job", "start_studio_job"]

_log = get_logger("ce.api.jobs")

WORKFLOW_OF: dict[JobKind, str] = {
    JobKind.ASSET_VALIDATION: "AssetValidationWorkflow",
    JobKind.AUTONOMOUS_SUGGEST: "AutonomousSuggestWorkflow",
    JobKind.BENCHMARK: "BenchmarkWorkflow",
    JobKind.CONSISTENCY: "ConsistencyWorkflow",
    JobKind.CRITIQUE: "CritiqueWorkflow",
    JobKind.CREATOR_TEST: "CreatorTestWorkflow",
    JobKind.IDENTITY_PACK: "BuildIdentityPackWorkflow",
    JobKind.VOICE_DESIGN: "VoiceDesignWorkflow",
    JobKind.VOICE_TEST: "VoiceTestWorkflow",
    JobKind.WARDROBE_REFS: "BuildWardrobeWorkflow",
    JobKind.WORLD_PLATES: "BuildWorldPlatesWorkflow",
    JobKind.CALIBRATION: "CalibrationWorkflow",
    JobKind.DELETION: "DeletionWorkflow",
    JobKind.EDIT_APPLY: "ApplyEditWorkflow",
    JobKind.EDIT_PROPOSE: "ProposeEditWorkflow",
    JobKind.GENERATE: "GenerateVersionWorkflow",
    JobKind.MEMORY_UPDATE: "MemoryUpdateWorkflow",
    JobKind.PACKAGE: "PackagingWorkflow",
    JobKind.RESEARCH_INGEST: "ResearchIngestWorkflow",
    JobKind.PLAN: "PlanVideoWorkflow",
    JobKind.PREVIZ: "PrevizWorkflow",
    JobKind.RENDER: "RenderWorkflow",
    JobKind.SCREEN_ANALYSIS: "ScreenAnalysisWorkflow",
}


async def create_job(
    session: AsyncSession,
    *,
    org_id: UUID,
    kind: JobKind,
    target_type: str,
    target_id: UUID,
    requested_by: UUID | None,
    input: dict[str, Any] | None = None,
    video_version_id: UUID | None = None,
) -> GenerationJob:
    job = GenerationJob(
        org_id=org_id,
        kind=kind.value,
        status="queued",
        target_type=target_type,
        target_id=target_id,
        input=input or {},
        requested_by=requested_by,
        video_version_id=video_version_id,
    )
    session.add(job)
    await session.flush()
    job.temporal_workflow_id = f"{kind.value}-{job.id}"
    await session.flush()
    return job


def job_event(job: GenerationJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "progress": float(job.progress or 0),
        "target_type": job.target_type,
        "target_id": str(job.target_id),
        **({"error": job.error} if job.error else {}),
    }


class WorkflowStarter:
    """Starts workflows on the orchestrator's task queue through a lazily connected client."""

    def __init__(self, effective: EffectiveConfig) -> None:
        self.effective = effective
        self.prefix = effective.settings.temporal_task_queue_prefix
        self._client: Any = None
        self._lock = asyncio.Lock()
        self.handles: list[Any] = []
        self.before_start: Callable[[], Awaitable[None]] | None = None

    @property
    def task_queue(self) -> str:
        return f"{self.prefix}orchestrator"

    async def client(self) -> Any:
        async with self._lock:
            if self._client is None:
                from temporalio.client import Client
                from temporalio.contrib.pydantic import pydantic_data_converter

                settings = self.effective.settings
                self._client = await Client.connect(
                    settings.temporal_address,
                    namespace=settings.temporal_namespace,
                    data_converter=pydantic_data_converter,
                )
            return self._client

    async def start(self, workflow: str, arg: Any, *, workflow_id: str) -> Any:
        if self.before_start is not None:
            await self.before_start()
        client = await self.client()
        handle = await client.start_workflow(workflow, arg, id=workflow_id, task_queue=self.task_queue)
        self.handles.append(handle)
        return handle

    async def cancel(self, workflow_id: str) -> None:
        client = await self.client()
        await client.get_workflow_handle(workflow_id).cancel()

    async def drain(self, timeout_s: float = 300.0) -> None:
        """Waits for every workflow this process started (tests; graceful shutdown)."""
        pending, self.handles = self.handles, []
        for handle in pending:
            try:
                await asyncio.wait_for(handle.result(), timeout_s)
            except Exception as exc:
                _log.info("workflow ended with an error", workflow_id=handle.id, error=str(exc)[:200])


async def start_job(services: Any, org_id: UUID, job_id: UUID, kind: JobKind, arg: dict[str, Any]) -> None:
    """Starts the job's workflow; called from `BackgroundTasks`, i.e. after the request committed.
    If the workflow cannot start, the job fails with a recorded reason (never silently)."""
    workflow_id = f"{kind.value}-{job_id}"
    try:
        await services.workflows.start(WORKFLOW_OF[kind], arg, workflow_id=workflow_id)
    except Exception as exc:
        _log.error("workflow start failed", job_id=str(job_id), error=str(exc)[:300])
        async with services.db.transaction() as session:
            await session.execute(
                sa.update(GenerationJob)
                .where(GenerationJob.org_id == org_id, GenerationJob.id == job_id)
                .values(status="failed", error={"code": "workflow_start_failed", "message": type(exc).__name__})
            )
            job = await session.get_one(GenerationJob, job_id)
        await services.events.publish(org_id, EventType.JOB_UPDATED, job_event(job))


async def start_studio_job(
    session: AsyncSession,
    services: Any,
    background: Any,
    *,
    org_id: UUID,
    user_id: UUID | None,
    kind: JobKind,
    target_type: str,
    target_id: UUID,
    args: dict[str, Any] | None = None,
) -> GenerationJob:
    """Creates a studio job (Phase 10: identity packs, wardrobe references, voices, plates, Creator
    Test) and starts its workflow after the commit with the `ce_exec.studio.StudioContext`."""
    job = await create_job(
        session, org_id=org_id, kind=kind, target_type=target_type, target_id=target_id,
        requested_by=user_id, input=args or {},
    )  # fmt: skip
    build = services.effective.bundle.app.build
    context = {
        "kind": kind.value,
        "org_id": str(org_id),
        "job_id": str(job.id),
        "target_id": str(target_id),
        "args": args or {},
        "user_id": str(user_id) if user_id else None,
        "model_timeout_s": float(build.model_node_timeout_s),
        "cpu_timeout_s": float(build.cpu_node_timeout_s),
    }
    background.add_task(start_job, services, org_id, job.id, kind, context)
    return job
