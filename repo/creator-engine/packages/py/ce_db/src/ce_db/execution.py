"""Build execution records (§12, §29): artifacts, cache entries, execution nodes, attempts,
BuildManifest rows, takes, renders, artifact references, cost ledger, jobs and version states.

Every function takes the org explicitly and filters or stamps rows with it (I12). Callers are
the orchestrator's activities and the scheduler; tenant-facing routes read through repositories.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import VERSION_STATE_TRANSITIONS, VersionState
from ce_core.errors import ConflictError, NotFoundError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.assets import Artifact, ArtifactRef, CacheEntry, ExecutionNode, GenerationJob, JobAttempt
from ce_db.models.platform import CostLedger
from ce_db.models.videos import BuildManifestEntry, Caption, Render, Take, Video, VideoVersion

__all__ = [
    "CacheHit",
    "add_artifact_refs",
    "cache_drop",
    "cache_lookup",
    "cache_store",
    "insert_manifest_rows",
    "load_manifest_rows",
    "new_attempt",
    "record_cost",
    "register_artifact",
    "set_job",
    "set_version_state",
    "upsert_caption",
    "upsert_node",
    "upsert_render",
    "upsert_take",
]


@dataclass(frozen=True)
class CacheHit:
    artifact_id: UUID
    sha256: str
    effective_seed: int | None


async def register_artifact(
    session: AsyncSession,
    org_id: UUID,
    *,
    sha256: str,
    kind: str,
    mime: str,
    size: int,
    storage_key: str,
    media: Mapping[str, Any] | None = None,
    produced_by_node_id: UUID | None = None,
) -> UUID:
    """One row per (org, sha256, kind); storage already deduplicates by hash (§12.2)."""
    existing = (
        await session.execute(
            sa.select(Artifact.id)
            .where(Artifact.org_id == org_id, Artifact.sha256 == sha256, Artifact.kind == kind)
            .order_by(Artifact.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = Artifact(
        org_id=org_id,
        kind=kind,
        storage_key=storage_key,
        mime=mime,
        bytes=size,
        sha256=sha256,
        media=dict(media or {}),
        produced_by_node_id=produced_by_node_id,
    )
    session.add(row)
    await session.flush()
    return row.id


async def cache_lookup(session: AsyncSession, org_id: UUID, cache_key: str) -> CacheHit | None:
    row = (
        await session.execute(
            sa.select(CacheEntry.artifact_id, Artifact.sha256, CacheEntry.effective_seed)
            .join(Artifact, sa.and_(Artifact.org_id == CacheEntry.org_id, Artifact.id == CacheEntry.artifact_id))
            .where(CacheEntry.org_id == org_id, CacheEntry.cache_key == cache_key, Artifact.qc_state != "qc_rejected")
        )
    ).one_or_none()
    return CacheHit(row[0], row[1], row[2]) if row else None


async def cache_store(
    session: AsyncSession, org_id: UUID, cache_key: str, artifact_id: UUID, effective_seed: int | None
) -> None:
    """Points the key at the accepted artifact (a QC retry that succeeds replaces the entry)."""
    stmt = insert(CacheEntry).values(
        org_id=org_id, cache_key=cache_key, artifact_id=artifact_id, effective_seed=effective_seed
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[CacheEntry.org_id, CacheEntry.cache_key],
            set_={"artifact_id": artifact_id, "effective_seed": effective_seed, "updated_at": sa.func.now()},
        )
    )


async def cache_drop(session: AsyncSession, org_id: UUID, cache_key: str) -> None:
    await session.execute(sa.delete(CacheEntry).where(CacheEntry.org_id == org_id, CacheEntry.cache_key == cache_key))


async def upsert_node(
    session: AsyncSession,
    org_id: UUID,
    *,
    job_id: UUID,
    version_id: UUID,
    node_key: str,
    node_kind: str,
    scene_key: str | None = None,
    shot_key: str | None = None,
    chunk_index: int | None = None,
    take_index: int | None = None,
    **values: Any,
) -> ExecutionNode:
    """The node's row for this job (unique per job and node key); `values` updates it."""
    row = (
        await session.execute(
            sa.select(ExecutionNode)
            .where(ExecutionNode.org_id == org_id, ExecutionNode.job_id == job_id, ExecutionNode.node_key == node_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = ExecutionNode(
            org_id=org_id,
            job_id=job_id,
            version_id=version_id,
            node_key=node_key,
            node_kind=node_kind,
            scene_key=scene_key,
            shot_key=shot_key,
            chunk_index=chunk_index,
            take_index=take_index,
        )
        session.add(row)
    for column, value in values.items():
        setattr(row, column, value)
    await session.flush()
    return row


async def new_attempt(
    session: AsyncSession,
    org_id: UUID,
    node_id: UUID,
    *,
    reason: str,
    seed: int | None,
    route: Mapping[str, Any] | None,
    started_at: datetime,
) -> JobAttempt:
    last = (
        await session.execute(sa.select(sa.func.max(JobAttempt.attempt_no)).where(JobAttempt.node_id == node_id))
    ).scalar_one()
    attempt = JobAttempt(
        org_id=org_id,
        node_id=node_id,
        attempt_no=int(last or 0) + 1,
        reason=reason,
        adapter_id=(route or {}).get("adapter_id"),
        model_id=(route or {}).get("model_id"),
        model_revision=(route or {}).get("revision"),
        translator_version=(route or {}).get("translator_version"),
        seed=seed,
        started_at=started_at,
        status="running",
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def insert_manifest_rows(
    session: AsyncSession, org_id: UUID, version_id: UUID, rows: Iterable[Mapping[str, Any]]
) -> int:
    """Insert-only (§12.3): rows for node keys that already have one are skipped, so a resumed
    build only adds entries. Returns the number of rows inserted."""
    values = [
        {
            "org_id": org_id,
            "version_id": version_id,
            "node_key": r["node_key"],
            "route": r.get("route"),
            "effective_seed": r.get("effective_seed"),
            "artifact_id": r.get("artifact_id"),
            "config_digests": dict(r.get("config_digests") or {}),
            "impl_version": r.get("impl_version"),
        }
        for r in rows
    ]
    if not values:
        return 0
    stmt = insert(BuildManifestEntry).values(values).on_conflict_do_nothing(index_elements=["version_id", "node_key"])
    result = await session.execute(stmt.returning(BuildManifestEntry.node_key))
    return len(result.all())


async def load_manifest_rows(session: AsyncSession, org_id: UUID, version_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            sa.select(BuildManifestEntry).where(
                BuildManifestEntry.org_id == org_id, BuildManifestEntry.version_id == version_id
            )
        )
    ).scalars()
    return [
        {
            "node_key": r.node_key,
            "route": r.route,
            "effective_seed": r.effective_seed,
            "artifact_id": str(r.artifact_id) if r.artifact_id else None,
            "config_digests": r.config_digests,
            "impl_version": r.impl_version,
        }
        for r in rows
    ]


async def add_artifact_refs(
    session: AsyncSession, org_id: UUID, artifact_ids: Iterable[UUID], *, ref_type: str, ref_id: str
) -> None:
    values = [
        {"org_id": org_id, "artifact_id": a, "ref_type": ref_type, "ref_id": ref_id}
        for a in dict.fromkeys(artifact_ids)
    ]
    if values:
        await session.execute(insert(ArtifactRef).values(values).on_conflict_do_nothing())


async def upsert_take(
    session: AsyncSession,
    org_id: UUID,
    *,
    version_id: UUID,
    shot_key: str,
    take_index: int,
    **values: Any,
) -> Take:
    take_key = f"tk_{shot_key.removeprefix('sht_')}_{take_index}"
    row = (
        await session.execute(
            sa.select(Take).where(
                Take.org_id == org_id,
                Take.version_id == version_id,
                Take.shot_key == shot_key,
                Take.take_key == take_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Take(org_id=org_id, version_id=version_id, shot_key=shot_key, take_key=take_key, take_index=take_index)
        session.add(row)
    for column, value in values.items():
        setattr(row, column, value)
    await session.flush()
    return row


async def upsert_render(
    session: AsyncSession, org_id: UUID, *, version_id: UUID, preset_id: str, is_proxy: bool, **values: Any
) -> Render:
    row = (
        await session.execute(
            sa.select(Render).where(
                Render.org_id == org_id,
                Render.version_id == version_id,
                Render.preset_id == preset_id,
                Render.is_proxy == is_proxy,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Render(org_id=org_id, version_id=version_id, preset_id=preset_id, is_proxy=is_proxy, **values)
        session.add(row)
    else:
        for column, value in values.items():
            setattr(row, column, value)
    await session.flush()
    return row


async def upsert_caption(
    session: AsyncSession,
    org_id: UUID,
    *,
    version_id: UUID,
    language: str,
    format: str,
    style_id: str,
    artifact_id: UUID | None,
    review_state: str = "n/a",
    route: dict[str, Any] | None = None,
) -> Caption:
    """One caption file of a version (§27: ASS for burning, SRT/VTT for upload), per language and format.

    A translation's review (Phase 12) belongs to its content: a changed file goes back to `pending`,
    and a file identical to one reviewed on another version of the same video carries that review."""
    row = (
        await session.execute(
            sa.select(Caption).where(
                Caption.org_id == org_id,
                Caption.version_id == version_id,
                Caption.language == language,
                Caption.format == format,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Caption(org_id=org_id, version_id=version_id, language=language, format=format, style_id=style_id)
        session.add(row)
    changed = row.artifact_id != artifact_id
    row.style_id, row.artifact_id = style_id, artifact_id
    if route is not None:
        row.route = dict(route)
    if review_state != "pending":
        row.review_state = review_state
    elif changed or row.review_state in (None, "n/a"):
        row.review_state, row.reviewed_by, row.reviewed_at, row.review_note = "pending", None, None, None
        if artifact_id is not None:
            video_id = (
                sa.select(VideoVersion.video_id)
                .where(VideoVersion.org_id == org_id, VideoVersion.id == version_id)
                .scalar_subquery()
            )
            reviewed = (
                await session.execute(
                    sa.select(Caption)
                    .join(
                        VideoVersion,
                        sa.and_(VideoVersion.org_id == Caption.org_id, VideoVersion.id == Caption.version_id),
                    )
                    .where(
                        Caption.org_id == org_id,
                        VideoVersion.video_id == video_id,
                        Caption.version_id != version_id,
                        Caption.language == language,
                        Caption.format == format,
                        Caption.artifact_id == artifact_id,
                        Caption.review_state.in_(("approved", "rejected")),
                    )
                    .order_by(Caption.reviewed_at.desc().nulls_last())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if reviewed is not None:
                row.review_state, row.reviewed_by = reviewed.review_state, reviewed.reviewed_by
                row.reviewed_at, row.review_note = reviewed.reviewed_at, reviewed.review_note
    await session.flush()
    return row


async def record_cost(
    session: AsyncSession,
    org_id: UUID,
    *,
    kind: str,
    quantity: float,
    unit: str,
    unit_price_usd: float,
    version_id: UUID | None = None,
    video_id: UUID | None = None,
    project_id: UUID | None = None,
    node_id: UUID | None = None,
    worker_id: UUID | None = None,
) -> Decimal:
    amount = (Decimal(str(quantity)) * Decimal(str(unit_price_usd))).quantize(Decimal("0.000001"))
    session.add(
        CostLedger(
            org_id=org_id,
            kind=kind,
            quantity=quantity,
            unit=unit,
            unit_price_usd=Decimal(str(unit_price_usd)),
            amount_usd=amount,
            version_id=version_id,
            video_id=video_id,
            project_id=project_id,
            node_id=node_id,
            worker_id=worker_id,
        )
    )
    await session.flush()
    return amount


async def set_job(session: AsyncSession, org_id: UUID, job_id: UUID, **values: Any) -> GenerationJob:
    job = (
        await session.execute(
            sa.select(GenerationJob).where(GenerationJob.org_id == org_id, GenerationJob.id == job_id).with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError("generation job not found", table="generation_jobs")
    for column, value in values.items():
        setattr(job, column, value)
    await session.flush()
    return job


async def set_version_state(
    session: AsyncSession, org_id: UUID, version_id: UUID, state: VersionState, **values: Any
) -> VideoVersion:
    """Moves a version along the §12.8 state machine (status columns only; identity is immutable)."""
    version = (
        await session.execute(
            sa.select(VideoVersion)
            .where(VideoVersion.org_id == org_id, VideoVersion.id == version_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("video version not found", table="video_versions")
    current = VersionState(version.state)
    if current != state and state not in VERSION_STATE_TRANSITIONS.get(current, frozenset()):
        raise ConflictError(f"a version in {current} cannot move to {state}")
    version.state = state.value
    for column, value in values.items():
        setattr(version, column, value)
    if state in (VersionState.READY, VersionState.NEEDS_REVIEW) and version.frozen_at is None:
        version.frozen_at = sa.func.now()
    await session.flush()
    video = await session.get(Video, version.video_id)
    if video is not None and video.org_id == org_id and video.current_version_id is None:
        video.current_version_id = version.id
    return version


def statuses(rows: Sequence[ExecutionNode]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row.status] = out.get(row.status, 0) + 1
    return out
