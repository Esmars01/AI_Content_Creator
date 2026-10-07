"""The Director over the database: loading a planning context and persisting a plan (§13, I7).

`load_context` offers the org's approved creators (with their voice, appearance age, default
wardrobes and worlds, memory records, version numbers and recent usage) and approved worlds.
`persist_plan` writes, in the caller's transaction: the new MemorySnapshots (immutable, I7), the
version row in state `planned` (its spec is an immutable identity column, so the row is created
once planning is done), the plan report artifact, the `director_runs` records and the projections.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.canonical import content_digest
from ce_core.enums import ArtifactKind, VersionOrigin, VersionState
from ce_core.identity.creator import CreatorDNA, VoiceDNA
from ce_core.identity.world import WorldDNA
from ce_db.execution import register_artifact
from ce_db.models.creators import AppearanceVersion, Creator, CreatorVersion, VoiceVersion
from ce_db.models.memory import MemorySnapshot
from ce_db.models.research import BrandKit, Claim, ResearchFact, ResearchSource
from ce_db.models.videos import DirectorRun, Project, Video, VideoVersion
from ce_db.models.worlds import World, WorldVersion
from ce_db.projections import rebuild_projections
from ce_db.repository import OrgContext
from ce_db.versions import next_version_number
from ce_memory.store import create_snapshot, load_records, recent_usage, version_numbers
from ce_research.ingest import StoredFact
from sqlalchemy.ext.asyncio import AsyncSession

from ce_director.context import BrandDefault, CreatorOption, DirectorContext, WorldOption
from ce_director.director import PlanOutcome

__all__ = [
    "brand_default",
    "load_context",
    "load_facts",
    "persist_plan",
    "pinned_snapshots",
    "voice_dna",
    "write_claim_ledger",
]


def _voice(row: VoiceVersion | None) -> VoiceDNA | None:
    return voice_dna(row) if row is not None else None


def voice_dna(row: VoiceVersion) -> VoiceDNA:
    """A voice version as the Director reads it; raises ValidationError when the version is not
    usable (the approval endpoint checks the same, audit CR-VOICE-TRANSCRIPT)."""
    return VoiceDNA.model_validate(
        {
            "description": row.description or "",
            "references": list(row.references or []),
            "wpm": dict(row.wpm or {}),
            "lexicon": list(row.lexicon or []),
            "default_prosody": dict(row.default_prosody or {}),
        }
    )


async def load_context(
    session: AsyncSession,
    org_id: UUID,
    *,
    video_id: UUID,
    version_id: UUID,
    now: datetime,
    pinned: dict[UUID, tuple[UUID, list[dict[str, object]]]] | None = None,
) -> DirectorContext:
    creators: list[CreatorOption] = []
    rows = (
        await session.execute(
            sa.select(Creator, CreatorVersion)
            .join(
                CreatorVersion,
                sa.and_(CreatorVersion.org_id == Creator.org_id, CreatorVersion.id == Creator.current_version_id),
            )
            .where(Creator.org_id == org_id, Creator.status == "active", CreatorVersion.status == "approved")
            .order_by(Creator.created_at, Creator.id)
        )
    ).all()
    for creator, version in rows:
        voice = await session.get(VoiceVersion, version.voice_version_id) if version.voice_version_id else None
        appearance = (
            await session.get(AppearanceVersion, version.appearance_version_id)
            if version.appearance_version_id
            else None
        )
        appearance_dna = dict(appearance.dna or {}) if appearance is not None else {}
        creators.append(
            CreatorOption(
                creator_id=creator.id,
                creator_version_id=version.id,
                dna=CreatorDNA.model_validate(version.dna),
                voice_version_id=version.voice_version_id,
                voice=_voice(voice),
                appearance_version_id=version.appearance_version_id,
                appearance_age=appearance_dna.get("age_appearance"),
                appearance_text=" ".join(
                    str(appearance_dna.get(k, "")) for k in ("hair", "face_shape", "body_type", "grooming_style")
                ),
                wardrobe_version_ids=list(version.default_wardrobe_version_ids or []),
                default_world_ids=list(version.default_world_ids or []),
                memory=await load_records(session, org_id, creator.id),
                version_numbers=await version_numbers(session, org_id, creator.id),
                recent_usage=await recent_usage(session, org_id, creator.id, exclude_video_id=video_id),
            )
        )
    worlds: list[WorldOption] = []
    world_rows = (
        await session.execute(
            sa.select(World, WorldVersion)
            .join(
                WorldVersion, sa.and_(WorldVersion.org_id == World.org_id, WorldVersion.id == World.current_version_id)
            )
            .where(World.org_id == org_id, World.status == "active", WorldVersion.status == "approved")
            .order_by(World.created_at, World.id)
        )
    ).all()
    for world, world_version in world_rows:
        worlds.append(
            WorldOption(
                world.id,
                world_version.id,
                WorldDNA.model_validate(world_version.dna),
                frozenset((world_version.plates or {}).keys()),
            )
        )
    return DirectorContext(
        org_id=org_id,
        video_id=video_id,
        version_id=version_id,
        now=now,
        creators=creators,
        worlds=worlds,
        pinned_snapshots=dict(pinned or {}),
    )


async def brand_default(
    session: AsyncSession, org_id: UUID, *, project_id: UUID | None, parent: Any | None = None
) -> BrandDefault | None:
    """A replan keeps its version's brand; a new plan takes the project's brand kit (Phase 12),
    with the logo overlay on when the kit has a logo. Archived kits are not applied to new plans."""
    if parent is not None:
        brand = dict((parent.spec or {}).get("brand") or {})
        kit = brand.get("brand_kit_id")
        if kit:
            return BrandDefault(UUID(str(kit)), bool(brand.get("logo_overlay")))
        return None
    if project_id is None:
        return None
    project = (
        await session.execute(sa.select(Project).where(Project.org_id == org_id, Project.id == project_id))
    ).scalar_one_or_none()
    if project is None or project.brand_kit_id is None:
        return None
    kit = (
        await session.execute(
            sa.select(BrandKit).where(
                BrandKit.org_id == org_id, BrandKit.id == project.brand_kit_id, BrandKit.archived_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if kit is None:
        return None
    return BrandDefault(kit.id, kit.logo_asset_id is not None, kit.caption_style_id)


async def pinned_snapshots(
    session: AsyncSession, org_id: UUID, spec: dict[str, Any]
) -> dict[UUID, tuple[UUID, list[dict[str, object]]]]:
    """The snapshots a version pinned, keyed by creator version (a replan reuses them, §18.5)."""
    out: dict[UUID, tuple[UUID, list[dict[str, object]]]] = {}
    for pin in spec.get("memory", {}).get("snapshots", []):
        row = (
            await session.execute(
                sa.select(MemorySnapshot).where(
                    MemorySnapshot.org_id == org_id, MemorySnapshot.id == UUID(str(pin["snapshot_id"]))
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            out[row.creator_version_id] = (row.id, list(row.items or []))
    return out


async def persist_plan(
    session: AsyncSession,
    org_id: UUID,
    outcome: PlanOutcome,
    *,
    report_sha: str,
    report_bytes: int,
    report_key: str,
    created_by: UUID | None,
    origin: VersionOrigin = VersionOrigin.PLAN,
    parent_version_id: UUID | None = None,
    runs: Sequence[Any] = (),
) -> VideoVersion:
    spec = outcome.spec
    video_id = spec.video_id
    number = await next_version_number(session, org_id, video_id)
    artifact_id = await register_artifact(
        session,
        org_id,
        sha256=report_sha,
        kind=ArtifactKind.PLAN_REPORT.value,
        mime="application/json",
        size=report_bytes,
        storage_key=report_key,
    )
    dumped = spec.model_dump(mode="json")
    version = VideoVersion(
        id=spec.version_id,
        org_id=org_id,
        video_id=video_id,
        number=number,
        parent_version_id=parent_version_id,
        spec=dumped,
        spec_hash=content_digest(dumped),
        spec_content_digest=spec.content_digest(),
        planned_routes=outcome.planned_routes,
        plan_report_artifact_id=artifact_id,
        state=VersionState.PLANNED.value,
        origin=origin.value,
        coverage_summary=outcome.report.predicted_coverage.summary_counts()
        if outcome.report.predicted_coverage
        else {},
        created_by=created_by,
    )
    session.add(version)
    await session.flush()
    for snapshot_id, draft in outcome.snapshots:  # immutable once written (I7)
        await create_snapshot(session, org_id, draft, created_for_version_id=version.id, snapshot_id=snapshot_id)
    await session.execute(
        sa.update(Video)
        .where(Video.org_id == org_id, Video.id == video_id)
        .values(current_version_id=version.id, title=spec.meta.title, mode=str(spec.meta.mode))
    )
    for run in runs or outcome.runs:
        session.add(DirectorRun(org_id=org_id, version_id=version.id, **run.row()))
    await session.flush()
    await write_claim_ledger(session, org_id, video_id=video_id, version_id=version.id, spec=spec, outcome=outcome)
    await rebuild_projections(session, OrgContext(org_id, created_by), version.id)
    return version


async def write_claim_ledger(
    session: AsyncSession, org_id: UUID, *, video_id: UUID, version_id: UUID, spec: Any, outcome: PlanOutcome
) -> int:
    """The claim ledger (§29 `claims`, Phase 12): one row per `(video_id, claim_key)`, stable across
    versions. A replan updates the row's latest check (verdict, evidence quotes, flags) and keeps an
    earlier override only while the claim text is unchanged; evidence ids that are persistent facts
    are stored as `evidence_fact_ids`, every quote (with its source and span) in `evidence`."""
    claims = list(spec.research.claims) if spec.research else []
    checks = {(c.segment_key, c.text): c for c in outcome.claims}
    written = 0
    for claim in claims:
        segment = next((s.key for s in spec.script.segments if claim.key in s.claim_keys), None)
        check = checks.get((segment, claim.text)) if segment else None
        evidence: list[dict[str, Any]] = []
        fact_ids: list[UUID] = []
        for evidence_id in claim.evidence_ids:
            fact = outcome.evidence.get(evidence_id)
            if fact is None:
                continue
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "source_id": str(fact.source_id),
                    "source_title": fact.source_title,
                    "source_uri": fact.source_uri,
                    "trust": fact.trust,
                    "span": {"start": fact.start, "end": fact.end},
                    "quote": fact.text[:1000],
                }
            )
            if not fact.ref:  # a persistent fact (its id is the evidence id)
                fact_ids.append(fact.id)
        row = (
            await session.execute(
                sa.select(Claim)
                .where(Claim.org_id == org_id, Claim.video_id == video_id, Claim.claim_key == claim.key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        values = {
            "text": claim.text,
            "verdict": claim.verdict,
            "evidence_fact_ids": fact_ids,
            "evidence": evidence,
            "confidence": float(check.confidence) if check else 0.0,
            "last_version_id": version_id,
            "segment_key": segment,
            "reasons": list(check.reasons) if check else [],
            "closed_book": bool(spec.research.closed_book) if spec.research else False,
            "blocking": bool(check.blocking) if check else claim.verdict in ("unsupported", "conflicting"),
            "overridable": bool(check.overridable) if check else True,
            "detected": bool(check.detected) if check else False,
        }
        if row is None:
            session.add(
                Claim(org_id=org_id, video_id=video_id, claim_key=claim.key, first_version_id=version_id, **values)
            )
        else:
            if row.text != claim.text or not values["overridable"]:
                values.update({"override_by": None, "override_reason": None, "override_at": None})
            for key, value in values.items():
                setattr(row, key, value)
        written += 1
    await session.flush()
    return written


async def load_facts(
    session: AsyncSession, org_id: UUID, *, project_id: UUID | None, source_ids: Sequence[UUID]
) -> list[StoredFact]:
    """Facts of the persistent research sources a plan attached (Phase 12). Only ingested sources of
    the video's project (or org-wide sources without a project) are used; others are ignored."""
    if not source_ids:
        return []
    sources = (
        (
            await session.execute(
                sa.select(ResearchSource).where(
                    ResearchSource.org_id == org_id,
                    ResearchSource.id.in_(list(source_ids)),
                    ResearchSource.status == "ingested",
                    sa.or_(ResearchSource.project_id == project_id, ResearchSource.project_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {s.id: s for s in sources}
    if not by_id:
        return []
    rows = (
        (
            await session.execute(
                sa.select(ResearchFact)
                .where(ResearchFact.org_id == org_id, ResearchFact.source_id.in_(list(by_id)))
                .order_by(ResearchFact.source_id, ResearchFact.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    out: list[StoredFact] = []
    for row in rows:
        source = by_id[row.source_id]
        span = dict(row.quote_span or {})
        out.append(
            StoredFact(
                id=row.id,
                source_id=row.source_id,
                index=int(row.chunk_index),
                text=row.text,
                start=int(span.get("start", 0)),
                end=int(span.get("end", len(row.text))),
                trust=source.trust,
                embedding=tuple(float(x) for x in row.embedding) if row.embedding is not None else None,
                embedding_model=row.embedding_model,
                source_title=source.title or source.uri or "",
                source_uri=source.uri,
            )
        )
    return out
