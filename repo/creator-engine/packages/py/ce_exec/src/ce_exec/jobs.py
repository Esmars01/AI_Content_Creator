"""Work of the Phase 1 jobs moved into workflows (ADR 0031): `AssetValidationWorkflow` and
`DeletionWorkflow`. Each writes its `generation_jobs` row and `job.updated` events exactly as the
in-process runner did; no model or GPU calls (§30). A validated screen recording also gets a queued
`screen_analysis` job (Phase 7), which the validation workflow runs as a child (`ce_exec.screen`)."""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import JobKind
from ce_core.errors import CEError
from ce_db import execution as rec
from ce_db.models.assets import Asset, GenerationJob
from ce_db.models.memory import CreatorMemoryItem
from ce_obs import bound_context, get_logger
from ce_obs.events import EventType
from ce_storage.validation import validate_file
from sqlalchemy.ext.asyncio import AsyncSession

from ce_exec.context import ExecServices

__all__ = ["job_event", "run_job", "tombstone_memory", "validate_asset"]

_log = get_logger("ce.exec.jobs")


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


async def _set(
    svc: ExecServices, org_id: UUID, job_id: UUID, extra: dict[str, Any] | None = None, **values: Any
) -> None:
    async with svc.db.transaction() as session:
        job = await rec.set_job(session, org_id, job_id, **values)
        event = {**job_event(job), **(extra or {})}
    await svc.publish(org_id, EventType.JOB_UPDATED, event)


async def run_job(
    svc: ExecServices, org_id: UUID, job_id: UUID, work: Callable[[AsyncSession], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    with bound_context(org_id=org_id, job_id=job_id):
        await _set(svc, org_id, job_id, status="running", progress=0.05)
        try:
            async with svc.db.transaction() as session:
                result = await work(session)
        except CEError as exc:
            _log.warning("job failed", code=exc.code, error=exc.message)
            await _set(svc, org_id, job_id, status="failed", error={"code": exc.code, "message": exc.message})
            return {"status": "failed", "code": exc.code}
        except Exception as exc:
            _log.exception("job crashed")
            await _set(svc, org_id, job_id, status="failed", error={"code": "internal", "message": type(exc).__name__})
            raise
        await _set(svc, org_id, job_id, {"result": result}, status="succeeded", progress=1.0)
        return {"status": "succeeded", **result}


async def validate_asset(svc: ExecServices, org_id: UUID, job_id: UUID, asset_id: UUID) -> dict[str, Any]:
    async def work(session: AsyncSession) -> dict[str, Any]:
        asset = (
            await session.execute(
                sa.select(Asset).where(Asset.org_id == org_id, Asset.id == asset_id).with_for_update()
            )
        ).scalar_one()
        if asset.status != "uploading":  # a retried activity after success
            return {"asset_id": str(asset_id), "status": asset.status, "issues": []}
        bucket = svc.settings.s3_bucket_assets
        with tempfile.TemporaryDirectory(prefix="ce-validate-") as tmp:
            path = Path(tmp) / "upload"
            await svc.storage.download(bucket, asset.storage_key, path)
            result = await validate_file(
                path,
                mime=asset.mime,
                declared_bytes=asset.bytes,
                ffprobe_timeout_s=svc.bundle.app.uploads.ffprobe_timeout_s,
            )
        probe = {k: v for k, v in (asset.probe or {}).items() if k != "upload"}
        analysis_job: GenerationJob | None = None
        if result.ok:
            asset.status, asset.sha256, asset.bytes = "ready", result.sha256, result.size
            asset.probe = {**probe, **result.probe}
            if asset.kind == "screen_recording":  # §27: analyzed on upload (ScreenAnalysisWorkflow)
                parent = await session.get(GenerationJob, job_id)
                analysis_job = GenerationJob(
                    org_id=org_id,
                    kind=JobKind.SCREEN_ANALYSIS.value,
                    status="queued",
                    target_type="asset",
                    target_id=asset_id,
                    parent_job_id=job_id,
                    requested_by=parent.requested_by if parent is not None else None,
                )
                session.add(analysis_job)
                await session.flush()
                analysis_job.temporal_workflow_id = f"{JobKind.SCREEN_ANALYSIS.value}-{analysis_job.id}"
        else:
            asset.status, asset.sha256 = "rejected", result.sha256
            asset.probe = {
                **probe,
                **result.probe,
                "rejected": [{"code": i.code, "message": i.message, **i.detail} for i in result.issues],
            }
        await session.flush()
        return {
            "asset_id": str(asset_id),
            "status": asset.status,
            "kind": asset.kind,
            "issues": [i.code for i in result.issues],
            "screen_analysis_job_id": str(analysis_job.id) if analysis_job is not None else None,
        }

    return await run_job(svc, org_id, job_id, work)


async def tombstone_memory(svc: ExecServices, org_id: UUID, job_id: UUID, item_id: UUID) -> dict[str, Any]:
    async def work(session: AsyncSession) -> dict[str, Any]:
        await session.execute(
            sa.update(CreatorMemoryItem)
            .where(CreatorMemoryItem.org_id == org_id, CreatorMemoryItem.id == item_id)
            .values(value=None, text=None, embedding=None, status="forgotten", pinned=False, deleted_at=svc.clock())
        )
        return {"memory_item_id": str(item_id), "tombstone": True}

    return await run_job(svc, org_id, job_id, work)
