"""Videos and versions (§30). Creating, replanning and approving videos is in `planning`."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.spec.videospec import VideoSpec
from ce_db.models.assets import GenerationJob
from ce_db.models.videos import Project, Video, VideoVersion
from ce_db.repository import TenantRepository
from fastapi import APIRouter, Query
from pydantic import Field

from ce_api.common import Page, page
from ce_api.deps import DbSession, Reader, ServicesDep
from ce_api.schemas import Out

router = APIRouter(tags=["videos"])


class VideoOut(Out):
    id: UUID
    project_id: UUID
    title: str
    mode: str
    current_version_id: UUID | None
    status: str
    budget_usd: Decimal | None
    created_at: datetime
    updated_at: datetime
    current_version_state: str | None = Field(default=None, description="state of the current version")
    current_version_number: int | None = None
    planning: bool = Field(default=False, description="a plan job for this video is queued or running")


class VersionSummary(Out):
    id: UUID
    video_id: UUID
    number: int
    parent_version_id: UUID | None
    branch: str
    state: str
    origin: str
    flags: list[str]
    cost_estimate_usd: Decimal | None
    cost_actual_usd: Decimal | None
    frozen_at: datetime | None
    created_at: datetime


class VersionOut(VersionSummary):
    spec: dict[str, Any] = Field(description="the VideoSpec (with the computed script.wording_locked)")
    spec_hash: str
    spec_content_digest: str
    coverage_summary: dict[str, Any]
    claims_summary: dict[str, int] = Field(description="claim count per verdict (the fact-check stage)")


def _size(services: ServicesDep, limit: int | None) -> int:
    return min(limit or services.config.api.default_page_size, services.config.api.max_page_size)


@router.get("/v1/projects/{project_id}/videos", response_model=Page[VideoOut])
async def list_videos(
    project_id: UUID,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[VideoOut]:
    await TenantRepository(Project, session, principal.ctx).get(project_id)
    query = sa.select(Video).where(Video.org_id == principal.org_id, Video.project_id == project_id)
    rows, next_cursor = await page(session, query, Video.id, cursor=cursor, limit=_size(services, limit))
    return Page[VideoOut](items=await _with_state(session, principal.org_id, list(rows)), next_cursor=next_cursor)


@router.get("/v1/videos/{video_id}", response_model=VideoOut)
async def get_video(video_id: UUID, principal: Reader, session: DbSession) -> VideoOut:
    video = await TenantRepository(Video, session, principal.ctx).get(video_id)
    return (await _with_state(session, principal.org_id, [video]))[0]


async def _with_state(session: Any, org_id: UUID, videos: list[Video]) -> list[VideoOut]:
    """Videos with their current version's state and whether planning is under way (one query each)."""
    current = [v.current_version_id for v in videos if v.current_version_id]
    states: dict[UUID, tuple[str, int]] = {}
    if current:
        rows = await session.execute(
            sa.select(VideoVersion.id, VideoVersion.state, VideoVersion.number).where(
                VideoVersion.org_id == org_id, VideoVersion.id.in_(current)
            )
        )
        states = {r.id: (r.state, r.number) for r in rows}
    planning: set[UUID] = set()
    if videos:
        jobs = await session.execute(
            sa.select(GenerationJob.target_id).where(
                GenerationJob.org_id == org_id,
                GenerationJob.kind == "plan",
                GenerationJob.target_type == "video",
                GenerationJob.target_id.in_([v.id for v in videos]),
                GenerationJob.status.in_(("queued", "running")),
            )
        )
        planning = set(jobs.scalars())
    out = []
    for video in videos:
        state = states.get(video.current_version_id) if video.current_version_id else None
        out.append(
            VideoOut.model_validate(video).model_copy(
                update={
                    "current_version_state": state[0] if state else None,
                    "current_version_number": state[1] if state else None,
                    "planning": video.id in planning,
                }
            )
        )
    return out


@router.get("/v1/videos/{video_id}/versions", response_model=Page[VersionSummary])
async def list_versions(
    video_id: UUID,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[VersionSummary]:
    await TenantRepository(Video, session, principal.ctx).get(video_id)
    query = sa.select(VideoVersion).where(VideoVersion.org_id == principal.org_id, VideoVersion.video_id == video_id)
    rows, next_cursor = await page(session, query, VideoVersion.id, cursor=cursor, limit=_size(services, limit))
    return Page[VersionSummary](items=[VersionSummary.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/versions/{version_id}", response_model=VersionOut)
async def get_version(version_id: UUID, principal: Reader, session: DbSession) -> VersionOut:
    row = await TenantRepository(VideoVersion, session, principal.ctx).get(version_id)
    model = VideoSpec.model_validate(row.spec)
    claims = Counter(str(c.verdict) for c in (model.research.claims if model.research else []))
    spec = model.to_api()
    summary = VersionSummary.model_validate(row).model_dump()
    return VersionOut(
        **summary,
        spec=spec,
        spec_hash=row.spec_hash,
        spec_content_digest=row.spec_content_digest,
        coverage_summary=row.coverage_summary,
        claims_summary=dict(sorted(claims.items())),
    )
