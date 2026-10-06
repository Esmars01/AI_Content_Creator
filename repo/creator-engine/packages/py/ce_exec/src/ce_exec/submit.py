"""Creating an approved version from a spec and its generation job (the fixture path of `ce video
generate-fixture` and the end-to-end tests, until planning (Phase 4) and approval (Phase 5/6)).

The spec is validated against the approved records it references, its plan-time routes are
computed (`video_versions.planned_routes`, the initial pins of the build, §12.3) and its
projections are rebuilt. The version starts in `approved`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_build import build_graph, planned_routes
from ce_core.canonical import content_digest
from ce_core.enums import JobKind
from ce_core.ids import new_id
from ce_core.spec.videospec import VideoSpec
from ce_db.models.assets import GenerationJob
from ce_db.models.videos import Video, VideoVersion
from ce_db.projections import rebuild_projections
from ce_db.repository import OrgContext
from ce_db.session import Database

from ce_exec.context import ExecServices
from ce_exec.refs_loader import load_refs

__all__ = ["Submitted", "submit_spec"]


@dataclass(frozen=True)
class Submitted:
    video_id: UUID
    version_id: UUID
    job_id: UUID
    workflow_id: str


async def submit_spec(
    svc: ExecServices,
    *,
    org_id: UUID,
    project_id: UUID,
    spec_data: dict[str, Any],
    user_id: UUID | None = None,
    video_id: UUID | None = None,
) -> Submitted:
    """Inserts (video?, version, job). With `video_id` the version is added to that video as the
    next number with the latest version as parent; otherwise a new video is created."""
    db: Database = svc.db
    async with db.transaction() as session:
        parent_id: UUID | None = None
        number = 1
        if video_id is None:
            video = Video(
                org_id=org_id,
                project_id=project_id,
                title=str(spec_data["meta"]["title"]),
                mode=str(spec_data["meta"]["mode"]),
            )
            session.add(video)
            await session.flush()
            video_id = video.id
        else:
            latest = (
                await session.execute(
                    sa.select(VideoVersion.id, VideoVersion.number)
                    .where(VideoVersion.org_id == org_id, VideoVersion.video_id == video_id)
                    .order_by(VideoVersion.number.desc())
                    .limit(1)
                )
            ).one_or_none()
            if latest is not None:
                parent_id, number = latest[0], latest[1] + 1
        version_id = new_id()
        data = {
            **spec_data,
            "video_id": str(video_id),
            "version_id": str(version_id),
            "parent_version_id": str(parent_id) if parent_id else None,
        }
        spec = VideoSpec.model_validate(data)
        refs = await load_refs(session, org_id, spec)
        graph = build_graph(spec, refs, svc.bundle, svc.catalog)
        dumped = spec.model_dump(mode="json")
        session.add(
            VideoVersion(
                id=version_id,
                org_id=org_id,
                video_id=video_id,
                number=number,
                parent_version_id=parent_id,
                spec=dumped,
                spec_hash=content_digest(dumped),
                spec_content_digest=spec.content_digest(),
                planned_routes=planned_routes(graph),
                state="approved",
                origin="plan" if parent_id is None else "restore",
                created_by=user_id,
            )
        )
        await session.flush()
        await session.execute(
            sa.update(Video).where(Video.org_id == org_id, Video.id == video_id).values(current_version_id=version_id)
        )
        await rebuild_projections(session, OrgContext(org_id, user_id), version_id)
        job = GenerationJob(
            org_id=org_id,
            kind=JobKind.GENERATE.value,
            status="queued",
            target_type="video_version",
            target_id=version_id,
            video_version_id=version_id,
            requested_by=user_id,
            input={"source": "fixture"},
        )
        session.add(job)
        await session.flush()
        workflow_id = f"generate-{job.id}"
        job.temporal_workflow_id = workflow_id
        job_id = job.id
    return Submitted(video_id, version_id, job_id, workflow_id)
