"""Jobs, build manifests, renders and captions (§30). Reads are org-scoped; starting a render and
cancelling a job hand over to the job's Temporal workflow (the API never runs job work, §9)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import JobKind, VersionState
from ce_core.errors import ConflictError, InvalidInputError, Issue
from ce_db.models.assets import Artifact, ExecutionNode, GenerationJob, JobAttempt
from ce_db.models.videos import BuildManifestEntry, Caption, Render, VideoVersion
from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import Field

from ce_api.common import Page, page
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.jobs import create_job, start_job
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped

router = APIRouter(tags=["jobs"])


class JobOut(Out):
    id: UUID
    kind: str
    status: str
    priority: int
    target_type: str
    target_id: UUID
    video_version_id: UUID | None
    progress: float
    cost_estimate_usd: Decimal | None
    cost_actual_usd: Decimal | None
    error: dict[str, Any] | None
    output: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class AttemptOut(Out):
    id: UUID
    attempt_no: int
    reason: str
    status: str
    adapter_id: str | None
    model_id: str | None
    model_revision: str | None
    error_class: str | None
    error_message: str | None
    seed: int | None
    gpu_seconds: float
    cost_usd: Decimal
    started_at: datetime | None
    ended_at: datetime | None


class NodeOut(Out):
    node_key: str
    node_kind: str
    status: str
    scene_key: str | None
    shot_key: str | None
    chunk_index: int | None
    take_index: int | None
    cache_key: str | None
    route: dict[str, Any] | None
    effective_seed: int | None
    artifact_ids: list[UUID]
    attempts: int = Field(description="number of attempts so far")
    attempt_history: list[AttemptOut] = Field(default_factory=list)


class JobDetail(JobOut):
    nodes: list[NodeOut]


class CancelAccepted(Out):
    job_id: UUID
    status: str


class ManifestOut(Out):
    version_id: UUID
    frozen_at: datetime | None
    routes: dict[str, dict[str, Any]]
    effective_seeds: dict[str, int]
    artifact_map: dict[str, UUID]
    config_digests: dict[str, str]
    impl_versions: dict[str, str]


class RenderOut(Out):
    id: UUID
    version_id: UUID
    preset_id: str
    aspect: str
    is_proxy: bool
    status: str
    provenance_mode: str
    artifact_id: UUID | None
    watermark_payload_id: str | None
    created_at: datetime


class RenderDetail(RenderOut):
    c2pa_manifest: dict[str, Any] | None
    consent_ids: list[UUID]
    media: dict[str, Any] | None = None


class RenderDownload(Out):
    url: str
    expires_at: datetime
    exportable: bool = Field(description="false for mock_dev provenance: such renders cannot be exported (§32)")


class RenderRequest(Body):
    model_config = examples([{"preset_ids": ["youtube_1920x1080_30"], "proxy": False}])

    preset_ids: Annotated[list[str], Field(min_length=1, max_length=10)]
    proxy: bool = False


class RenderAccepted(Out):
    job_id: UUID
    version_id: UUID


@router.get("/v1/jobs", response_model=Page[JobOut])
async def list_jobs(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    status: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[JobOut]:
    query = sa.select(GenerationJob).where(GenerationJob.org_id == principal.org_id)
    if status:
        query = query.where(GenerationJob.status == status)
    if kind:
        query = query.where(GenerationJob.kind == kind)
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, GenerationJob.id, cursor=cursor, limit=size)
    return Page[JobOut](items=[JobOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, principal: Reader, session: DbSession) -> JobDetail:
    job = await get_scoped(session, GenerationJob, principal.ctx, job_id, "job")
    nodes = list(
        (
            await session.execute(
                sa.select(ExecutionNode)
                .where(ExecutionNode.org_id == principal.org_id, ExecutionNode.job_id == job_id)
                .order_by(ExecutionNode.created_at, ExecutionNode.node_key)
            )
        ).scalars()
    )
    attempts: dict[UUID, list[AttemptOut]] = {}
    if nodes:
        rows = (
            await session.execute(
                sa.select(JobAttempt)
                .where(JobAttempt.org_id == principal.org_id, JobAttempt.node_id.in_([n.id for n in nodes]))
                .order_by(JobAttempt.attempt_no)
            )
        ).scalars()
        for row in rows:
            attempts.setdefault(row.node_id, []).append(AttemptOut.model_validate(row))
    out = [NodeOut.model_validate(n).model_copy(update={"attempt_history": attempts.get(n.id, [])}) for n in nodes]
    return JobDetail(**JobOut.model_validate(job).model_dump(), nodes=out)


@router.post("/v1/jobs/{job_id}:cancel", response_model=CancelAccepted, status_code=202)
async def cancel_job(job_id: UUID, principal: Writer, session: DbSession, services: ServicesDep) -> CancelAccepted:
    job = await get_scoped(session, GenerationJob, principal.ctx, job_id, "job")
    if job.status not in ("queued", "running"):
        raise ConflictError(f"a {job.status} job cannot be cancelled")
    if job.temporal_workflow_id:
        await services.workflows.cancel(job.temporal_workflow_id)
    return CancelAccepted(job_id=job.id, status="cancelling")


@router.get("/v1/versions/{version_id}/manifest", response_model=ManifestOut)
async def get_manifest(version_id: UUID, principal: Reader, session: DbSession) -> ManifestOut:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(BuildManifestEntry).where(
                BuildManifestEntry.org_id == principal.org_id, BuildManifestEntry.version_id == version_id
            )
        )
    ).scalars()
    routes: dict[str, dict[str, Any]] = {}
    seeds: dict[str, int] = {}
    artifacts: dict[str, UUID] = {}
    configs: dict[str, str] = {}
    impls: dict[str, str] = {}
    for row in rows:
        if row.route:
            routes[row.node_key] = row.route
        if row.effective_seed is not None:
            seeds[row.node_key] = int(row.effective_seed)
        if row.artifact_id:
            artifacts[row.node_key] = row.artifact_id
        if row.impl_version:
            impls[row.node_key] = row.impl_version
        configs.update({str(k): str(v) for k, v in (row.config_digests or {}).items()})
    return ManifestOut(
        version_id=version.id,
        frozen_at=version.frozen_at,
        routes=routes,
        effective_seeds=seeds,
        artifact_map=artifacts,
        config_digests=configs,
        impl_versions=impls,
    )


@router.get("/v1/versions/{version_id}/renders", response_model=list[RenderOut])
async def list_renders(version_id: UUID, principal: Reader, session: DbSession) -> list[RenderOut]:
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(Render)
            .where(Render.org_id == principal.org_id, Render.version_id == version_id)
            .order_by(Render.created_at)
        )
    ).scalars()
    return [RenderOut.model_validate(r) for r in rows]


@router.post("/v1/versions/{version_id}/renders", response_model=RenderAccepted, status_code=202)
async def request_renders(
    version_id: UUID,
    body: RenderRequest,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> RenderAccepted:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if version.state != VersionState.READY.value:
        raise ConflictError("extra renders need a ready version (§9 RenderWorkflow)")
    unknown = [p for p in body.preset_ids if services.effective.bundle.render_preset(p) is None]
    if unknown:
        raise InvalidInputError(
            "unknown render presets", issues=[Issue("preset_id", f"unknown preset {p}") for p in unknown]
        )
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.RENDER,
        target_type="video_version",
        target_id=version.id,
        requested_by=principal.user_id,
        input={"preset_ids": body.preset_ids, "proxy": body.proxy},
        video_version_id=version.id,
    )
    cfg = services.effective.bundle.app.build
    prefix = services.workflows.prefix
    arg = {
        "org_id": str(principal.org_id),
        "job_id": str(job.id),
        "version_id": str(version.id),
        "preset_ids": body.preset_ids,
        "queues": {"orchestrator": f"{prefix}orchestrator", "render": f"{prefix}render"},
        "max_parallel": cfg.max_parallel_nodes,
        "model_timeout_s": cfg.model_node_timeout_s,
        "cpu_timeout_s": cfg.cpu_node_timeout_s,
        "render_timeout_s": cfg.render_node_timeout_s,
    }
    background.add_task(start_job, services, principal.org_id, job.id, JobKind.RENDER, arg)
    return RenderAccepted(job_id=job.id, version_id=version.id)


@router.get("/v1/renders/{render_id}", response_model=RenderDetail)
async def get_render(render_id: UUID, principal: Reader, session: DbSession) -> RenderDetail:
    render = await get_scoped(session, Render, principal.ctx, render_id, "render")
    media = None
    if render.artifact_id:
        artifact = await get_scoped(session, Artifact, principal.ctx, render.artifact_id, "artifact")
        media = {"mime": artifact.mime, "bytes": artifact.bytes, "sha256": artifact.sha256}
    return RenderDetail(
        **RenderOut.model_validate(render).model_dump(),
        c2pa_manifest=render.c2pa_manifest,
        consent_ids=list(render.consent_ids or []),
        media=media,
    )


class RenderVerification(Out):
    render_id: UUID
    provenance_mode: str
    verdict: str = Field(description="valid | valid_untrusted_root | mock_dev | invalid")
    c2pa: dict[str, Any]
    watermarks: dict[str, Any]


async def _provenance_routes(session: Any, org_id: UUID, render: Render) -> dict[str, str]:
    """The adapters that wrote each layer of this render (its sign/watermark nodes' routes)."""
    keys = {
        f"provenance.sign:{render.preset_id}": "c2pa",
        f"provenance.watermark_video:{render.preset_id}": "watermark_video",
        f"provenance.watermark_audio:{render.preset_id}": "watermark_audio",
    }
    rows = (
        await session.execute(
            sa.select(ExecutionNode.node_key, ExecutionNode.route)
            .where(
                ExecutionNode.org_id == org_id,
                ExecutionNode.version_id == render.version_id,
                ExecutionNode.node_key.in_(list(keys)),
                ExecutionNode.route.is_not(None),
            )
            .order_by(ExecutionNode.created_at)
        )
    ).all()
    out: dict[str, str] = {}
    for key, route in rows:
        if isinstance(route, dict) and route.get("adapter_id"):
            out[keys[key]] = str(route["adapter_id"])
    return out


@router.get("/v1/renders/{render_id}/verify", response_model=RenderVerification)
async def verify_render(
    render_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> RenderVerification:
    """Checks the C2PA manifest (signature, hashes, trust) and the invisible watermarks of a
    render's file (§27). Dev renders verify with an untrusted root by design."""
    import tempfile
    from pathlib import Path

    from ce_api.provenance import verify_render_file

    render = await get_scoped(session, Render, principal.ctx, render_id, "render")
    if render.artifact_id is None or render.status != "ready" or render.is_proxy:
        raise ConflictError("only ready, non-proxy renders carry provenance")
    artifact = await get_scoped(session, Artifact, principal.ctx, render.artifact_id, "artifact")
    routes = await _provenance_routes(session, principal.org_id, render)
    with tempfile.TemporaryDirectory(prefix="ce-render-verify-") as tmp:
        path = Path(tmp) / "render.mp4"
        await services.storage.download(services.settings.s3_bucket_artifacts, artifact.storage_key, path)
        result = await verify_render_file(
            path,
            sha256=artifact.sha256,
            mime=artifact.mime,
            routes=routes,
            payload_id=render.watermark_payload_id,
            provenance_mode=render.provenance_mode,
            settings=services.settings,
        )
    return RenderVerification(render_id=render.id, **result)


@router.get("/v1/renders/{render_id}/download", response_model=RenderDownload)
async def download_render(
    render_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> RenderDownload:
    render = await get_scoped(session, Render, principal.ctx, render_id, "render")
    if render.artifact_id is None or render.status != "ready":
        raise ConflictError("the render is not ready")
    artifact = await get_scoped(session, Artifact, principal.ctx, render.artifact_id, "artifact")
    request = await services.storage.presign_get(
        services.settings.s3_bucket_artifacts,
        artifact.storage_key,
        ttl_s=services.config.storage.presign_ttl_s,
        download_filename=f"{render.preset_id}{'-proxy' if render.is_proxy else ''}.mp4",
    )
    return RenderDownload(url=request.url, expires_at=request.expires_at, exportable=render.provenance_mode == "real")


# ---------------------------------------------------------------------- captions (§27, §30)


class CaptionOut(Out):
    id: UUID
    version_id: UUID
    language: str
    style_id: str
    format: str = Field(description="ass (burned into the render), srt or vtt (platform upload)")
    review_state: str = Field(
        description="n/a for the spoken language; translations are pending until approved (Phase 12)"
    )
    artifact_id: UUID | None
    created_at: datetime


class CaptionDownload(Out):
    url: str
    expires_at: datetime
    filename: str


@router.get("/v1/versions/{version_id}/captions", response_model=list[CaptionOut])
async def list_captions(version_id: UUID, principal: Reader, session: DbSession) -> list[CaptionOut]:
    """The version's caption files: ASS, SRT and VTT per language, written by its build."""
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(Caption)
            .where(Caption.org_id == principal.org_id, Caption.version_id == version_id)
            .order_by(Caption.language, Caption.format)
        )
    ).scalars()
    return [CaptionOut.model_validate(r) for r in rows]


@router.get("/v1/captions/{caption_id}/download", response_model=CaptionDownload)
async def download_caption(
    caption_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> CaptionDownload:
    caption = await get_scoped(session, Caption, principal.ctx, caption_id, "caption")
    if caption.artifact_id is None:
        raise ConflictError("the caption file is not built yet")
    artifact = await get_scoped(session, Artifact, principal.ctx, caption.artifact_id, "artifact")
    filename = f"captions.{caption.language}.{caption.format}"
    request = await services.storage.presign_get(
        services.settings.s3_bucket_artifacts,
        artifact.storage_key,
        ttl_s=services.config.storage.presign_ttl_s,
        download_filename=filename,
    )
    return CaptionDownload(url=request.url, expires_at=request.expires_at, filename=filename)
