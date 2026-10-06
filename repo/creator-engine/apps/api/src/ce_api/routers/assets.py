"""Assets (§30): multipart presigned upload, completion with validation, read, delete.

Flow: `:initiate-upload` creates the row (`uploading`) and a multipart upload with one presigned
PUT per part → the client uploads the parts directly to storage → `:complete` (with an
`Idempotency-Key`) assembles the object and starts an `asset_validation` job (size, magic bytes,
ffprobe; §33) that moves the asset to `ready` or `rejected`. A validated screen recording is then
analyzed (`screen_analysis` job, §27); `GET /v1/assets/{id}/screen-analysis` returns the result.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_core.ids import new_id
from ce_db.models.assets import ASSET_KINDS, Asset, GenerationJob
from ce_db.models.videos import Project
from ce_storage import MIN_PART_BYTES, CompletedPart, MultipartUpload, asset_key, content_key
from ce_storage.base import MAX_PART_NUMBER
from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from ce_api.common import Page, begin_idempotent, finish_idempotent, page
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.jobs import create_job, start_job
from ce_api.schemas import Body, Out, examples
from ce_api.uploads.validation import KIND_FAMILIES, family_of
from ce_api.versioning import get_scoped

router = APIRouter(tags=["assets"])


class InitiateUpload(Body):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    mime: str
    bytes: Annotated[int, Field(gt=0)]
    kind: str
    project_id: UUID | None = None
    tags: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=20)

    model_config = examples([{"filename": "desk.png", "mime": "image/png", "bytes": 482113, "kind": "image"}])


class PartUrl(Out):
    part_number: int
    url: str
    headers: dict[str, str]
    expires_at: datetime


class UploadPlan(Out):
    upload_id: str
    part_size: int
    parts: list[PartUrl]


class UploadInitiated(Out):
    asset_id: UUID
    upload: UploadPlan


class CompletePart(Body):
    part_number: Annotated[int, Field(ge=1, le=MAX_PART_NUMBER)]
    etag: Annotated[str, Field(min_length=1, max_length=200)]


class CompleteUpload(Body):
    parts: list[CompletePart] | None = Field(
        default=None, description="the ETag of every part; omit to use the parts storage received"
    )

    model_config = examples([{"parts": [{"part_number": 1, "etag": "9b2cf535f27731c974343645a3985328"}]}])


class CompleteAccepted(Out):
    asset_id: UUID
    job_id: UUID
    status: str = "validating"


class AssetOut(Out):
    id: UUID
    project_id: UUID | None
    kind: str
    mime: str
    bytes: int
    sha256: str
    status: str
    filename: str | None = None
    probe: dict[str, Any]
    rights: dict[str, Any]
    tags: list[str]
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class AssetDetail(AssetOut):
    download_url: str | None = Field(default=None, description="presigned GET (short TTL) when the asset is ready")


def _out(row: Asset, model: type[AssetOut] = AssetOut, **extra: Any) -> Any:
    data = AssetOut.model_validate(row).model_dump()
    upload = (row.probe or {}).get("upload", {})
    data["filename"] = upload.get("filename") or (row.rights or {}).get("filename")
    data["probe"] = {k: v for k, v in (row.probe or {}).items() if k != "upload"}
    return model(**data, **extra)


def _plan_parts(size: int) -> tuple[int, int]:
    part_size = max(MIN_PART_BYTES, math.ceil(size / MAX_PART_NUMBER))
    return part_size, max(1, math.ceil(size / part_size))


@router.post("/v1/assets:initiate-upload", response_model=UploadInitiated, status_code=201)
async def initiate_upload(
    body: InitiateUpload, principal: Writer, session: DbSession, services: ServicesDep
) -> UploadInitiated:
    uploads = services.config.uploads
    issues: list[Issue] = []
    if body.mime not in uploads.allowed_media_types:
        issues.append(Issue("upload_media_type", f"{body.mime} is not accepted", path="/mime"))
    if body.bytes > uploads.max_bytes:
        issues.append(Issue("upload_too_large", f"uploads are at most {uploads.max_bytes} bytes", path="/bytes"))
    if body.kind not in ASSET_KINDS:
        issues.append(Issue("asset_kind", f"unknown asset kind {body.kind!r}", path="/kind"))
    elif family_of(body.mime) not in KIND_FAMILIES.get(body.kind, frozenset()):
        issues.append(Issue("asset_kind_media", f"a {body.kind} asset cannot be {body.mime}", path="/kind"))
    if not body.filename.isprintable() or "/" in body.filename or "\\" in body.filename:
        issues.append(Issue("filename", "filenames are printable and have no path separators", path="/filename"))
    if issues:
        raise InvalidInputError("the upload is not accepted", issues=issues)
    if body.project_id is not None:
        await get_scoped(session, Project, principal.ctx, body.project_id, "project")
    asset_id = new_id()
    key = asset_key(principal.org_id, asset_id)
    bucket = services.settings.s3_bucket_assets
    multipart = await services.storage.create_multipart(bucket, key, content_type=body.mime)
    part_size, count = _plan_parts(body.bytes)
    ttl = services.config.storage.presign_ttl_s
    parts = []
    for number in range(1, count + 1):
        request = await services.storage.presign_part(multipart, number, ttl_s=ttl)
        parts.append(
            PartUrl(part_number=number, url=request.url, headers=dict(request.headers), expires_at=request.expires_at)
        )
    session.add(
        Asset(
            id=asset_id,
            org_id=principal.org_id,
            project_id=body.project_id,
            kind=body.kind,
            storage_key=key,
            mime=body.mime,
            bytes=body.bytes,
            sha256="",
            status="uploading",
            tags=body.tags,
            probe={"upload": {"upload_id": multipart.upload_id, "filename": body.filename, "part_size": part_size}},
            rights={"owner": str(principal.user_id), "filename": body.filename},
            created_by=principal.user_id,
        )
    )
    await session.flush()
    return UploadInitiated(
        asset_id=asset_id, upload=UploadPlan(upload_id=multipart.upload_id, part_size=part_size, parts=parts)
    )


@router.post("/v1/assets/{asset_id}:complete", response_model=CompleteAccepted, status_code=202)
async def complete_upload(
    asset_id: UUID,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
    body: CompleteUpload | None = None,
) -> Any:
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    asset = (
        await session.execute(
            sa.select(Asset).where(Asset.org_id == principal.org_id, Asset.id == asset_id).with_for_update()
        )
    ).scalar_one_or_none()
    if asset is None:
        raise NotFoundError("asset not found")
    if asset.status != "uploading":
        raise ConflictError(f"the asset is already {asset.status}")
    upload = (asset.probe or {}).get("upload", {})
    if upload.get("upload_id"):
        multipart = MultipartUpload(services.settings.s3_bucket_assets, asset.storage_key, upload["upload_id"])
        parts = [CompletedPart(p.part_number, p.etag.strip('"')) for p in body.parts] if body and body.parts else None
        await services.storage.complete_multipart(multipart, parts)
        asset.probe = {**asset.probe, "upload": {**upload, "upload_id": None, "completed": True}}
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.ASSET_VALIDATION,
        target_type="asset",
        target_id=asset.id,
        requested_by=principal.user_id,
    )
    accepted = CompleteAccepted(asset_id=asset.id, job_id=job.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    background.add_task(
        start_job,
        services,
        principal.org_id,
        job.id,
        JobKind.ASSET_VALIDATION,
        {"org_id": str(principal.org_id), "job_id": str(job.id), "asset_id": str(asset.id)},
    )
    return accepted


@router.get("/v1/assets", response_model=Page[AssetOut])
async def list_assets(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    kind: str | None = None,
    status: str | None = None,
    project_id: UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[AssetOut]:
    query = sa.select(Asset).where(Asset.org_id == principal.org_id)
    if kind:
        query = query.where(Asset.kind == kind)
    if status:
        query = query.where(Asset.status == status)
    if project_id:
        query = query.where(Asset.project_id == project_id)
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, Asset.id, cursor=cursor, limit=size)
    return Page[AssetOut](items=[_out(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/assets/{asset_id}", response_model=AssetDetail)
async def get_asset(asset_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> AssetDetail:
    row = await get_scoped(session, Asset, principal.ctx, asset_id, "asset")
    url = None
    if row.status == "ready":
        filename = (row.rights or {}).get("filename")
        request = await services.storage.presign_get(
            services.settings.s3_bucket_assets,
            row.storage_key,
            ttl_s=services.config.storage.presign_ttl_s,
            download_filename=filename,
        )
        url = request.url
    detail: AssetDetail = _out(row, AssetDetail, download_url=url)
    return detail


class ScreenAnalysisOut(Out):
    asset_id: UUID
    status: Literal["ready", "queued", "running", "failed", "not_analyzed"]
    job_id: UUID | None = None
    analysis: dict[str, Any] | None = Field(
        default=None,
        description="scenes, keyframes (OCR boxes, added/removed text, changed regions), dead time, the VLM "
        "summary and an event timeline; OCR and VLM text is data, never instructions",
    )
    keyframes_url: str | None = Field(default=None, description="presigned GET of the 1 fps keyframe clip")
    error: dict[str, Any] | None = None


@router.get("/v1/assets/{asset_id}/screen-analysis", response_model=ScreenAnalysisOut)
async def get_screen_analysis(
    asset_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> ScreenAnalysisOut:
    """The `screen_analysis` of a screen recording (§27), or the state of its analysis job."""
    row = await get_scoped(session, Asset, principal.ctx, asset_id, "asset")
    if row.kind != "screen_recording":
        raise ConflictError(f"only screen recordings are analyzed (this asset is a {row.kind})")
    meta = (row.probe or {}).get("screen_analysis") or {}
    job = (
        await session.execute(
            sa.select(GenerationJob)
            .where(
                GenerationJob.org_id == principal.org_id,
                GenerationJob.kind == JobKind.SCREEN_ANALYSIS.value,
                GenerationJob.target_id == asset_id,
            )
            .order_by(GenerationJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if meta.get("sha256") and meta.get("asset_sha256") == row.sha256:
        bucket = services.settings.s3_bucket_artifacts
        analysis = json.loads(await services.storage.get(bucket, content_key(str(meta["sha256"]))))
        url = None
        clip = analysis.get("keyframes_clip_sha256")
        if clip:
            request = await services.storage.presign_get(
                bucket, content_key(str(clip)), ttl_s=services.config.storage.presign_ttl_s
            )
            url = request.url
        return ScreenAnalysisOut(
            asset_id=row.id, status="ready", job_id=meta.get("job_id"), analysis=analysis, keyframes_url=url
        )
    if job is None:
        return ScreenAnalysisOut(asset_id=row.id, status="not_analyzed")
    if job.status not in ("queued", "running", "failed"):  # an analysis of earlier content or version
        return ScreenAnalysisOut(asset_id=row.id, status="not_analyzed", job_id=job.id)
    return ScreenAnalysisOut(asset_id=row.id, status=job.status, job_id=job.id, error=job.error)  # type: ignore[arg-type]


@router.delete("/v1/assets/{asset_id}", status_code=204)
async def delete_asset(asset_id: UUID, principal: Writer, session: DbSession, services: ServicesDep) -> Response:
    row = await get_scoped(session, Asset, principal.ctx, asset_id, "asset")
    bucket, key = services.settings.s3_bucket_assets, row.storage_key
    upload_id = ((row.probe or {}).get("upload") or {}).get("upload_id")
    await session.delete(row)
    await session.flush()  # a referenced asset fails here with a conflict, before storage is touched
    if upload_id:
        await services.storage.abort_multipart(MultipartUpload(bucket, key, upload_id))
    await services.storage.delete(bucket, key)
    return Response(status_code=204)
