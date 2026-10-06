"""Behavior, coverage and take observations (§30 "Intent, behavior, coverage"). Read-only: the
documents come from the version's BuildManifest (the content-addressed output documents of the
`behavior.*` nodes) and the `behavior_observations` rows; nothing is computed by a model here."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_behavior.compiler import realized_methods
from ce_behavior.coverage import coverage_report
from ce_behavior.resolve import CastInput, build_envelope
from ce_core.behavior.cbs import CanonicalBehaviorSpec, CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.coverage import BehaviorCoverageReport, CoverageEntry
from ce_core.errors import NotFoundError
from ce_core.spec.videospec import VideoSpec
from ce_db.models.assets import Artifact
from ce_db.models.behavior import BehaviorObservation, QCReport
from ce_db.models.creators import CreatorVersion
from ce_db.models.videos import BuildManifestEntry, Take, VideoVersion
from ce_storage.content import ContentStore
from fastapi import APIRouter
from pydantic import Field

from ce_api.deps import DbSession, Reader, ServicesDep
from ce_api.schemas import Out
from ce_api.versioning import get_scoped

router = APIRouter(tags=["behavior"])

Stage = Literal["predicted", "compiled", "observed"]


class CoverageOut(Out):
    version_id: UUID
    stage: Stage = Field(description="observed after render.final; compiled while the build runs")
    summary: dict[str, int] = Field(description="counts by level and by outcome; `delivered` = *_CONFIRMED only (I9)")
    decision: dict[str, Any] | None = Field(
        default=None, description="the Performance QA decision (recorded; the QC ladder executes it in Phase 11)"
    )
    downgrades: list[dict[str, str]] = Field(default_factory=list)
    entries: list[CoverageEntry]


class SceneBehaviorOut(Out):
    version_id: UUID
    scene_key: str
    cbs: CanonicalBehaviorSpec = Field(description="envelope (assembled on read) and content")
    compiled: list[CompiledBehavior] = Field(description="one per segment and per shot chunk of the scene")
    coverage: list[CoverageEntry] = Field(description="the scene's entries of the coverage report")
    stage: Stage


class ObservationOut(Out):
    id: UUID
    level_scope: Literal["take", "viewer"]
    item_ref: str
    character_key: str
    dimension: str
    requested: dict[str, Any]
    level: str
    method: str
    approximation_executed: bool | None
    verdict: str
    outcome: str
    measures: dict[str, Any]
    confidence: float
    adapter_id: str | None
    translator_version: str | None
    model_revision: str | None
    language: str | None
    created_at: datetime


class TakeObservationsOut(Out):
    take_id: UUID
    version_id: UUID
    shot_key: str
    take_key: str
    take_index: int
    selected: bool
    rank: int | None
    behavior_signature: dict[str, Any] | None
    qc: dict[str, Any] | None = Field(default=None, description="score, metric score, verdict and QA decision")
    observations: list[ObservationOut]


async def _documents(
    session: DbSession, services: ServicesDep, org_id: UUID, version_id: UUID, prefix: str
) -> dict[str, dict[str, Any]]:
    """Output documents (`data`) of the version's manifest entries whose node key starts with `prefix`."""
    rows = (
        await session.execute(
            sa.select(BuildManifestEntry.node_key, Artifact.sha256)
            .join(
                Artifact,
                sa.and_(Artifact.org_id == BuildManifestEntry.org_id, Artifact.id == BuildManifestEntry.artifact_id),
            )
            .where(
                BuildManifestEntry.org_id == org_id,
                BuildManifestEntry.version_id == version_id,
                BuildManifestEntry.node_key.startswith(prefix),
            )
        )
    ).all()
    store = ContentStore(services.storage, services.settings.s3_bucket_artifacts)
    out: dict[str, dict[str, Any]] = {}
    for node_key, sha in rows:
        out[node_key] = dict(json.loads(await store.read_bytes(sha)).get("data", {}))
    return out


async def _cbs_by_scene(
    session: DbSession, services: ServicesDep, org_id: UUID, version_id: UUID
) -> dict[str, CBSContent]:
    docs = await _documents(session, services, org_id, version_id, "behavior.resolve:")
    return {key.split(":", 1)[1]: CBSContent.model_validate(d["content"]) for key, d in docs.items()}


async def _compiled(
    session: DbSession, services: ServicesDep, org_id: UUID, version_id: UUID
) -> list[CompiledBehavior]:
    docs = await _documents(session, services, org_id, version_id, "behavior.compile_")
    return [CompiledBehavior.model_validate(d["compiled"]) for _, d in sorted(docs.items())]


async def _report(
    session: DbSession, services: ServicesDep, org_id: UUID, version_id: UUID
) -> tuple[BehaviorCoverageReport, dict[str, Any]]:
    observed = await _documents(session, services, org_id, version_id, "behavior.coverage:")
    if observed:
        data = next(iter(observed.values()))
        return BehaviorCoverageReport.model_validate(data["report"]), data
    cbs = await _cbs_by_scene(session, services, org_id, version_id)
    if not cbs:
        raise NotFoundError("no behavior has been resolved for this version yet")
    compiled = await _compiled(session, services, org_id, version_id)
    report = coverage_report(cbs, realized_methods(compiled), services.vocab, stage="compiled", version_id=version_id)
    return report, {"summary": report.summary_counts(), "decision": None, "downgrades": []}


@router.get("/v1/versions/{version_id}/coverage", response_model=CoverageOut)
async def get_coverage(version_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> CoverageOut:
    """Requested / compiled / observed, one entry per CBS item and channel (§16.4)."""
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    report, data = await _report(session, services, principal.org_id, version_id)
    return CoverageOut(
        version_id=version_id,
        stage=report.stage,
        summary=dict(data.get("summary") or report.summary_counts()),
        decision=data.get("decision"),
        downgrades=list(data.get("downgrades") or []),
        entries=report.entries,
    )


@router.get("/v1/versions/{version_id}/behavior", response_model=SceneBehaviorOut)
async def get_behavior(
    version_id: UUID,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    scene_key: str | None = None,
) -> SceneBehaviorOut:
    """The scene's CBS, its CompiledBehavior documents and its coverage entries (§15.6, §15.7).
    Without `scene_key`, the first scene."""
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    spec = VideoSpec.model_validate(version.spec)
    ordered = sorted(spec.scenes, key=lambda s: s.order)
    scene = next((s for s in ordered if s.key == scene_key), None) if scene_key else (ordered[0] if ordered else None)
    if scene is None:
        raise NotFoundError(f"scene {scene_key} not found in this version")
    scene_key = scene.key
    cbs = (await _cbs_by_scene(session, services, principal.org_id, version_id)).get(scene_key)
    if cbs is None:
        raise NotFoundError(f"no behavior has been resolved for scene {scene_key} yet")
    cast: dict[str, CastInput] = {}
    for member in spec.cast:
        creator = (
            await session.execute(
                sa.select(CreatorVersion).where(
                    CreatorVersion.org_id == principal.org_id, CreatorVersion.id == member.creator_version_id
                )
            )
        ).scalar_one_or_none()
        snapshot = next((p.snapshot_id for p in spec.memory.snapshots if p.character_key == member.key), None)
        cast[member.key] = CastInput(
            character_key=member.key,
            dna=None,
            dna_behavior_digest="",
            creator_version_id=member.creator_version_id,
            appearance_version_id=member.overrides.appearance_version_id
            or (creator.appearance_version_id if creator else None),
            voice_version_id=member.overrides.voice_version_id or (creator.voice_version_id if creator else None),
            memory_snapshot_id=snapshot,
        )
    envelope = build_envelope(cbs, spec=spec, scene=scene, cast=cast, vocab_version=str(spec.vocab_version))
    scene_segments = set(scene.segment_keys)
    shots = {s.key for s in scene.shots}
    compiled = [
        c
        for c in await _compiled(session, services, principal.org_id, version_id)
        if c.target_key in scene_segments or c.target_key.split(":c", 1)[0] in shots
    ]
    report, _ = await _report(session, services, principal.org_id, version_id)
    refs = {c.item_ref for c in cbs.requested_controls}
    return SceneBehaviorOut(
        version_id=version_id,
        scene_key=scene_key,
        cbs=envelope,
        compiled=compiled,
        coverage=[e for e in report.entries if e.item_ref in refs],
        stage=report.stage,
    )


@router.get("/v1/takes/{take_id}/observations", response_model=TakeObservationsOut)
async def get_take_observations(take_id: UUID, principal: Reader, session: DbSession) -> TakeObservationsOut:
    """The take's per-item verdicts (raw engine output, `level_scope = take`) and its QC report."""
    take = await get_scoped(session, Take, principal.ctx, take_id, "take")
    rows = (
        await session.execute(
            sa.select(BehaviorObservation)
            .where(BehaviorObservation.org_id == principal.org_id, BehaviorObservation.take_id == take_id)
            .order_by(BehaviorObservation.item_ref, BehaviorObservation.dimension)
        )
    ).scalars()
    qc = None
    if take.qc_report_id is not None:
        report = await get_scoped(session, QCReport, principal.ctx, take.qc_report_id, "qc report")
        checks = dict(report.checks or {})
        behavior = dict(checks.get("behavior") or {})
        qc = {
            "verdict": report.verdict,
            "score": checks.get("score"),
            "metric_score": checks.get("metric_score"),
            "behavior_score": behavior.get("score"),
            "decision": behavior.get("decision"),
        }
    return TakeObservationsOut(
        take_id=take.id,
        version_id=take.version_id,
        shot_key=take.shot_key,
        take_key=take.take_key,
        take_index=take.take_index,
        selected=take.selected,
        rank=take.rank,
        behavior_signature=take.behavior_signature,
        qc=qc,
        observations=[ObservationOut.model_validate(r) for r in rows],
    )
