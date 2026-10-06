"""Derived versions (§12.8): one place creates them, for edits, regenerations, re-routes, lock
changes, take selections, restores, branches and duplicates.

The version's identity columns are written once. It starts in `approved` when its source passed
approval (generation follows and reaches `ready` at once when every node is cached) and in
`planned` otherwise (previz follows, so approval always applies to an up-to-date previz). It
becomes the video's current version; version numbers are one sequence per video across branches.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.canonical import content_digest
from ce_core.enums import JobKind, VersionOrigin, VersionState
from ce_core.errors import NotFoundError
from ce_core.spec.videospec import VideoSpec
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.assets import GenerationJob
from ce_db.models.videos import Video, VideoVersion
from ce_db.projections import rebuild_projections
from ce_db.repository import OrgContext

__all__ = ["PASSED_APPROVAL", "insert_derived_version", "passed_approval"]

PASSED_APPROVAL = frozenset(
    {
        VersionState.APPROVED.value,
        VersionState.GENERATING.value,
        VersionState.READY.value,
        VersionState.PARTIAL.value,
        VersionState.NEEDS_REVIEW.value,
    }
)


async def passed_approval(session: AsyncSession, org_id: UUID, version: VideoVersion) -> bool:
    """A version passed approval when it reached `approved`; a failed or cancelled one did when a
    generation job ran for it."""
    if version.state in PASSED_APPROVAL:
        return True
    if version.state in (VersionState.FAILED.value, VersionState.CANCELLED.value):
        started = (
            await session.execute(
                sa.select(sa.func.count()).where(
                    GenerationJob.org_id == org_id,
                    GenerationJob.video_version_id == version.id,
                    GenerationJob.kind == JobKind.GENERATE.value,
                )
            )
        ).scalar_one()
        return bool(started)
    return False


async def insert_derived_version(
    session: AsyncSession,
    org_id: UUID,
    *,
    source_version_id: UUID,
    new_version_id: UUID,
    document: Mapping[str, Any],
    origin: VersionOrigin,
    requested_by: UUID | None,
    build: Mapping[str, Any] | None = None,
    branch: str | None = None,
    video_id: UUID | None = None,
    parent_version_id: UUID | str | None = "source",
    approved: bool | None = None,
    planned_routes: Mapping[str, Any] | None = None,
    plan_report_artifact_id: UUID | str | None = "source",
    keep_video_title: bool = False,
) -> tuple[VideoVersion, GenerationJob]:
    """The version row, its projections, the video's current-version pointer and the queued
    generation (or previz) job, in the caller's transaction.

    `planned_routes` are the derived version's (an edit's graph); without them the source's are
    copied (restore, branch and duplicate keep the spec). The plan report is the source's unless
    an edit wrote one for the derived version; previz rewrites it with measurements either way."""
    source = (
        await session.execute(
            sa.select(VideoVersion).where(VideoVersion.org_id == org_id, VideoVersion.id == source_version_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise NotFoundError("video version not found", table="video_versions")
    target_video = video_id or source.video_id
    data = dict(document)
    parent = source_version_id if parent_version_id == "source" else parent_version_id
    data["video_id"] = str(target_video)
    data["version_id"] = str(new_version_id)
    data["parent_version_id"] = str(parent) if isinstance(parent, UUID) else None
    spec = VideoSpec.model_validate(data)
    if approved is None:
        approved = await passed_approval(session, org_id, source)
    number = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.max(VideoVersion.number), 0)).where(
                VideoVersion.org_id == org_id, VideoVersion.video_id == target_video
            )
        )
    ).scalar_one() + 1
    dumped = spec.model_dump(mode="json")
    version = VideoVersion(
        id=new_version_id,
        org_id=org_id,
        video_id=target_video,
        number=number,
        parent_version_id=parent if isinstance(parent, UUID) else None,
        branch=branch or source.branch,
        spec=dumped,
        spec_hash=content_digest(dumped),
        spec_content_digest=spec.content_digest(),
        planned_routes=dict(planned_routes) if planned_routes is not None else (source.planned_routes or {}),
        plan_report_artifact_id=(
            source.plan_report_artifact_id if isinstance(plan_report_artifact_id, str) else plan_report_artifact_id
        ),
        state=(VersionState.APPROVED if approved else VersionState.PLANNED).value,
        origin=origin.value,
        coverage_summary={},
        created_by=requested_by,
    )
    session.add(version)
    await session.flush()
    await session.execute(
        sa.update(Video)
        .where(Video.org_id == org_id, Video.id == target_video)
        .values(
            current_version_id=new_version_id,
            mode=str(spec.meta.mode),
            **({} if keep_video_title else {"title": spec.meta.title}),
        )
    )
    await rebuild_projections(session, OrgContext(org_id, requested_by), new_version_id)
    kind = JobKind.GENERATE if approved else JobKind.PREVIZ
    job = GenerationJob(
        org_id=org_id,
        kind=kind.value,
        status="queued",
        target_type="video_version",
        target_id=new_version_id,
        video_version_id=new_version_id,
        requested_by=requested_by,
        input={"origin": origin.value, "source_version_id": str(source_version_id), "build": dict(build or {})},
    )
    session.add(job)
    await session.flush()
    job.temporal_workflow_id = f"{kind.value}-{job.id}"
    await session.flush()
    return version, job
