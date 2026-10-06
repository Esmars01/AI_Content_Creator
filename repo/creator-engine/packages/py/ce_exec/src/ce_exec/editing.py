"""Edit jobs (§28, §12.8): `ProposeEditWorkflow` and `ApplyEditWorkflow` call these with ids only.

- `propose_edit`: an `edit_proposals` row in `proposing` → the operations (from the instruction
  through `EditDirector`, or given) → `compute_proposal` (patch, validation, impact, coverage
  delta, alternatives) → the row becomes `proposed` (or `failed`, with the issues in
  `impact.issues`) and `edit.proposed` is published. Thin wrappers (regenerate, take selection,
  locks, re-route) set `auto_apply`.
- `apply_proposal`: a derived version (origin from the proposal) whose spec is the parent's spec
  with the patch applied; it starts in `approved` when the parent passed approval (generation
  follows), otherwise in `planned` (previz follows, §12.8). It becomes the video's current version.
- `create_derived_version`: the shared version factory (also restore, branch, duplicate).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import asdict, replace
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_build import BuildOptions, ParentBuild, planned_routes
from ce_build.refs import BuildRefs, VoiceRef, WardrobeRef, WorldRef
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_core.behavior.plan_report import Finding, PlanReport, event_timings
from ce_core.edit.ops import EditOperation, parse_operations
from ce_core.edit.patch import SpecPatch, apply_patch
from ce_core.enums import ArtifactKind, RecordStatus, VersionOrigin
from ce_core.errors import Issue
from ce_core.identity.creator import CreatorDNA, VoiceDNA
from ce_core.identity.world import WorldDNA
from ce_core.ids import new_id
from ce_core.spec.anchors import estimate_segment_timings
from ce_core.spec.validate import (
    CreatorVersionInfo,
    InMemoryReferences,
    OwnedVersionInfo,
    RecordInfo,
    SnapshotInfo,
    VoiceVersionInfo,
    WorldVersionInfo,
)
from ce_core.spec.videospec import VideoSpec
from ce_db import execution as rec
from ce_db.models.assets import Asset, GenerationJob
from ce_db.models.creators import (
    AppearanceVersion,
    CreatorVersion,
    Voice,
    VoiceVersion,
    Wardrobe,
    WardrobeVersion,
)
from ce_db.models.memory import MemorySnapshot
from ce_db.models.videos import DirectorRun, EditProposal, Video, VideoVersion
from ce_db.models.worlds import World, WorldVersion
from ce_db.versions import PASSED_APPROVAL, insert_derived_version
from ce_director import DirectorDeps
from ce_director.edit import EditContext, EditDirector, EditRequest, RecordOption, Selection
from ce_director.proposal import Proposal, ProposalInputs, compute_proposal
from ce_llm import PromptLibrary, prompts_root, provider_from_settings
from ce_obs import get_logger
from ce_obs.events import EventType
from ce_render.timeline import SegmentAudio, build_timeline

from ce_exec.context import ExecServices, VersionData
from ce_exec.parents import load_parent
from ce_exec.planning import _measured_timings, _store_report, read_plan_report
from ce_exec.refs_loader import load_assets
from ce_exec.screen import screen_operations

__all__ = [
    "PASSED_APPROVAL",
    "apply_proposal",
    "build_proposal",
    "create_derived_version",
    "propose_edit",
    "run_apply_job",
]

_log = get_logger("ce.exec.editing")

ORIGIN_OF_KIND = {
    "edit": VersionOrigin.EDIT,
    "regenerate": VersionOrigin.REGENERATE,
    "reroute": VersionOrigin.REROUTE,
    "lock_change": VersionOrigin.LOCK_CHANGE,
    "take_select": VersionOrigin.TAKE_SELECT,
    "approximations_stale": VersionOrigin.EDIT,
}


def _job_event(job: GenerationJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "progress": float(job.progress or 0),
        "target_type": job.target_type,
        "target_id": str(job.target_id),
        **({"error": job.error} if job.error else {}),
    }


# ---------------------------------------------------------------------- records the edit may use


def _op_asset_ids(operations: Sequence[EditOperation]) -> set[UUID]:
    """Org assets the operations point at (a new screen recording, B-roll clip, music file…)."""
    from ce_core.edit.ops import operation_list

    found: set[UUID] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "asset_id" and isinstance(item, str):
                    with contextlib.suppress(ValueError):
                        found.add(UUID(item))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(operation_list.dump_python(list(operations), mode="json"))
    return found


async def _options(
    session: Any, org_id: UUID, spec: VideoSpec, refs: BuildRefs, extra_asset_ids: Iterable[UUID] = ()
) -> tuple[list[RecordOption], BuildRefs, InMemoryReferences]:
    """Approved worlds of the org, and outfits and voice versions of the cast's creators: the edit
    may switch to them. Returns the options, build references that also cover them, and the
    validation lookup over every approved record involved. `extra_asset_ids`: assets operations
    point at (ready ones of the org become references; others stay unknown to validation)."""
    approved = RecordStatus.APPROVED
    options: list[RecordOption] = []
    worlds: dict[UUID, WorldRef] = dict(refs.worlds)
    wardrobes: dict[UUID, WardrobeRef] = dict(refs.wardrobes)
    voices: dict[UUID, VoiceRef] = dict(refs.voices)
    lookup = InMemoryReferences()
    creator_ids = {c.creator_id for c in refs.creators.values()}

    world_rows = (
        await session.execute(
            sa.select(World, WorldVersion)
            .join(WorldVersion, sa.and_(WorldVersion.org_id == World.org_id, WorldVersion.world_id == World.id))
            .where(World.org_id == org_id, World.status == "active", WorldVersion.status == "approved")
        )
    ).all()
    plate_assets = {
        UUID(str(a))
        for _, wv in world_rows
        for times in (wv.plates or {}).values()
        for weathers in times.values()
        for a in weathers.values()
    }
    wardrobe_rows = (
        await session.execute(
            sa.select(Wardrobe, WardrobeVersion)
            .join(
                WardrobeVersion,
                sa.and_(WardrobeVersion.org_id == Wardrobe.org_id, WardrobeVersion.wardrobe_id == Wardrobe.id),
            )
            .where(
                Wardrobe.org_id == org_id, Wardrobe.creator_id.in_(creator_ids), WardrobeVersion.status == "approved"
            )
        )
    ).all()
    voice_rows = (
        await session.execute(
            sa.select(Voice, VoiceVersion)
            .join(VoiceVersion, sa.and_(VoiceVersion.org_id == Voice.org_id, VoiceVersion.voice_id == Voice.id))
            .where(
                Voice.org_id == org_id,
                sa.or_(Voice.creator_id.in_(creator_ids), Voice.creator_id.is_(None)),
                VoiceVersion.status == "approved",
            )
        )
    ).all()
    extra_assets = set(plate_assets)
    requested = {a for a in extra_asset_ids if a not in refs.assets}
    if requested:
        found = (
            await session.execute(
                sa.select(Asset.id).where(Asset.org_id == org_id, Asset.id.in_(requested), Asset.status == "ready")
            )
        ).scalars()
        extra_assets.update(found)
    for _, wv in wardrobe_rows:
        extra_assets.update(wv.reference_asset_ids or [])
    for _, vv in voice_rows:
        extra_assets.update(UUID(str(r["asset_id"])) for r in vv.references or [] if r.get("asset_id"))
    assets = dict(refs.assets)
    missing = {a for a in extra_assets if a not in assets}
    if missing:
        try:
            assets.update(await load_assets(session, org_id, missing))
        except LookupError:
            _log.warning("some option assets are not ready; their records are not offered", count=len(missing))
    for world, wv in world_rows:
        try:
            plates = {
                cam: {tod: {w: assets[UUID(str(a))] for w, a in ws.items()} for tod, ws in tods.items()}
                for cam, tods in (wv.plates or {}).items()
            }
        except KeyError:
            continue
        dna = WorldDNA.model_validate(wv.dna)
        worlds.setdefault(wv.id, WorldRef(wv.id, wv.dna, plates))
        lookup.world_versions[wv.id] = WorldVersionInfo(wv.id, approved, dna)
        if world.current_version_id == wv.id:
            options.append(
                RecordOption(
                    "world",
                    wv.id,
                    dna.name,
                    f"{dna.kind.replace('_', ' ')} — {dna.geometry.layout}",
                    tuple(dna.style_tags),
                )
            )
    for wardrobe, wv in wardrobe_rows:
        reference_assets = tuple(assets[a] for a in wv.reference_asset_ids or [] if a in assets)
        wardrobes.setdefault(wv.id, WardrobeRef(wv.id, wv.spec, reference_assets))
        lookup.wardrobe_versions[wv.id] = OwnedVersionInfo(wv.id, approved, wardrobe.creator_id)
        if wardrobe.current_version_id == wv.id:
            spec_data = wv.spec or {}
            options.append(
                RecordOption(
                    "wardrobe",
                    wv.id,
                    str(spec_data.get("name", wardrobe.name)),
                    str(spec_data.get("description", "")),
                    tuple(spec_data.get("style_tags", [])),
                    owner=str(wardrobe.creator_id),
                )
            )
    for voice, vv in voice_rows:
        voice_dna = VoiceDNA.model_validate(
            {
                "description": vv.description or "",
                "references": list(vv.references or []),
                "wpm": dict(vv.wpm or {}),
                "lexicon": list(vv.lexicon or []),
                "default_prosody": dict(vv.default_prosody or {}),
            }
        )
        try:
            voices.setdefault(vv.id, VoiceRef.from_dna(vv.id, voice_dna, assets))
        except KeyError:
            continue
        lookup.voice_versions[vv.id] = VoiceVersionInfo(vv.id, approved, voice.creator_id, voice_dna)
        options.append(
            RecordOption(
                "voice",
                vv.id,
                f"{voice.name} v{vv.number}",
                vv.description or "",
                owner=str(voice.creator_id) if voice.creator_id else None,
                accent=(vv.default_prosody or {}).get("accent"),
            )
        )
    # the cast and what the spec already references (creators, appearances, snapshots, assets)
    for member in spec.cast:
        row = await session.get(CreatorVersion, member.creator_version_id)
        if row is None or row.org_id != org_id:
            continue
        lookup.creator_versions[row.id] = CreatorVersionInfo(
            row.id, row.creator_id, RecordStatus(row.status), CreatorDNA.model_validate(row.dna)
        )
        if row.appearance_version_id and row.voice_version_id:
            lookup.defaults[row.id] = (row.appearance_version_id, row.voice_version_id)
        appearance_ids = {row.appearance_version_id, member.overrides.appearance_version_id} - {None}
        for appearance_id in appearance_ids:
            appearance = await session.get(AppearanceVersion, appearance_id)
            if appearance is not None and appearance.org_id == org_id:
                lookup.appearance_versions[appearance.id] = OwnedVersionInfo(
                    appearance.id, RecordStatus(appearance.status), row.creator_id
                )
    for pin in spec.memory.snapshots:
        snapshot = await session.get(MemorySnapshot, pin.snapshot_id)
        if snapshot is not None and snapshot.org_id == org_id:
            lookup.snapshots[snapshot.id] = SnapshotInfo(snapshot.id, snapshot.creator_version_id)
    for asset_id in assets:
        lookup.assets[asset_id] = RecordInfo(asset_id, RecordStatus.APPROVED)
    for version_id in refs.voices:
        if version_id not in lookup.voice_versions:
            row = await session.get(VoiceVersion, version_id)
            if row is not None:
                lookup.voice_versions[version_id] = VoiceVersionInfo(version_id, RecordStatus(row.status), None)
    superset = replace(refs, worlds=worlds, wardrobes=wardrobes, voices=voices, assets=assets)
    return options, superset, lookup


async def _word_times(
    svc: ExecServices, data: VersionData
) -> tuple[dict[str, list[tuple[float, float]]], Literal["measured", "estimated"]]:
    """Measured word times (the version's aligned dialogue) when it generated, else estimated."""
    cfg = svc.bundle.app.render.timeline
    async with svc.db.session() as session:
        rows = await rec.load_manifest_rows(session, data.org_id, data.version_id)
        from ce_exec.parents import artifact_shas

        shas = await artifact_shas(
            session,
            data.org_id,
            {r["node_key"]: r["artifact_id"] for r in rows if r["node_key"].startswith("align.") and r["artifact_id"]},
        )
    segments: dict[str, SegmentAudio] = {}
    for key, sha in shas.items():
        try:
            out = await svc.docs.output(sha)
        except Exception:  # a missing document: fall back to estimates
            segments = {}
            break
        segments[key.split(":", 1)[1]] = SegmentAudio(
            duration_s=float(out.data["duration_s"]),
            words=tuple((float(a), float(b)) for a, b in out.data["words"]),
        )
    spec = data.spec
    if segments and set(segments) >= {s.key for s in spec.script.segments}:
        timeline = build_timeline(spec, segments, lead_s=cfg.lead_s, gap_s=cfg.segment_gap_s, tail_s=cfg.tail_s)
        return {k: list(v) for k, v in timeline.word_times.items()}, "measured"
    return _estimated_words(svc, spec, data.refs), "estimated"


def _wpm(svc: ExecServices, spec: VideoSpec, refs: BuildRefs) -> float:
    """The calibrated speaking rate of the first cast member's voice (the default without one)."""
    member = spec.cast[0]
    creator = refs.creators.get(member.creator_version_id)
    voice_id = member.overrides.voice_version_id or (creator.voice_version_id if creator else None)
    voice = refs.voices.get(voice_id) if voice_id else None
    default = svc.bundle.app.spec.default_wpm
    return float(voice.wpm_for(spec.meta.language, default)) if voice else float(default)


def _estimated_words(svc: ExecServices, spec: VideoSpec, refs: BuildRefs) -> dict[str, list[tuple[float, float]]]:
    cfg = svc.bundle.app.render.timeline
    order = [
        (k, spec.script.segment(k).text) for s in sorted(spec.scenes, key=lambda s: s.order) for k in s.segment_keys
    ]
    timings = estimate_segment_timings(order, _wpm(svc, spec, refs), start_s=cfg.lead_s, gap_s=cfg.segment_gap_s)
    return {k: [(w.start_s, w.end_s) for w in t.words] for k, t in timings.items()}


def derived_plan_report(
    svc: ExecServices,
    parent: PlanReport | None,
    spec: VideoSpec,
    refs: BuildRefs,
    predicted: BehaviorCoverageReport | None,
    *,
    note: str,
    detail: dict[str, Any],
    assumptions: Sequence[str] = (),
) -> PlanReport:
    """The plan report of an edited version (§13 previz, §12.8): estimated timings of the new spec,
    its predicted coverage, the parent's findings (timing findings are re-measured) and a note
    naming the edit. Previz replaces the estimates with measurements."""
    cfg = svc.bundle.app.render.timeline
    words = _estimated_words(svc, spec, refs)
    ends = [w[1] for times in words.values() for w in times]
    total = (max(ends) if ends else cfg.lead_s) + cfg.tail_s
    base = parent or PlanReport(
        version_id=spec.version_id,
        input_mode=spec.brief.input_mode,
        planner="template",
        estimated_duration_s=0.0,
        target_duration_s=float(spec.meta.target_duration_s),
    )
    findings = [f for f in base.findings if f.kind != "timing" and f.detail.get("check") != "derived_version"]
    findings.append(Finding(kind="other", severity="info", message=note, detail={"check": "derived_version", **detail}))
    return base.model_copy(
        update={
            "version_id": spec.version_id,
            "input_mode": spec.brief.input_mode,
            "estimated_duration_s": round(total, 2),
            "target_duration_s": float(spec.meta.target_duration_s),
            "timing_source": "estimated",
            "state_timings": _measured_timings(spec, base, words),
            "event_timings": event_timings(spec, words),
            "predicted_coverage": predicted if predicted is not None else base.predicted_coverage,
            "findings": findings,
            "assumptions": [*base.assumptions, *[a for a in assumptions if a not in base.assumptions]],
            "cost_estimate_usd": None,
        }
    )


# ---------------------------------------------------------------------- proposals


async def build_proposal(
    svc: ExecServices,
    data: VersionData,
    operations: Sequence[EditOperation],
    *,
    actor: str = "user",
    allow_lock_removal: bool = False,
    options: BuildOptions | None = None,
) -> tuple[Proposal, list[RecordOption], BuildRefs]:
    """`compute_proposal` over the database: the version's build (when it generated) is the parent
    of the derived version; option records extend the references."""
    async with svc.db.session() as session:
        record_options, superset, lookup = await _options(
            session, data.org_id, data.spec, data.refs, extra_asset_ids=_op_asset_ids(operations)
        )
    base = options or BuildOptions(provenance_mode=svc.provenance_mode)
    loaded = await load_parent(svc, data.org_id, data.version_id, base)
    parent: ParentBuild | None = loaded[0] if loaded is not None else None
    inputs = ProposalInputs(
        parent_spec=data.spec,
        parent_refs=superset,
        bundle=svc.bundle,
        catalog=svc.catalog,
        references=lookup,
        refs_for=lambda _spec: superset,
        parent_build=parent,
        actor=actor,
        allow_lock_removal=allow_lock_removal,
        options=base,
    )
    proposal = compute_proposal(inputs, list(operations))
    if proposal.status == "proposed" and proposal.spec is not None:
        # §27: the Director plans zooms, speed segments and the webcam bubble of new screen shots
        extra, notes = await screen_operations(
            svc, data.org_id, data.spec, proposal.spec, _wpm(svc, proposal.spec, superset), operations
        )
        if extra:
            planned = compute_proposal(inputs, [*operations, *extra])
            if planned.status == "proposed":
                proposal = planned
            else:
                notes.append(
                    "the planned screen zooms did not apply: " + "; ".join(i.message for i in planned.issues)[:300]
                )
        if notes:
            proposal.side_effects.append({"kind": "screen_plan", "notes": notes})
    return proposal, record_options, superset


async def propose_edit(svc: ExecServices, org_id: UUID, job_id: UUID) -> dict[str, Any]:
    """The `edit_propose` job: operations → proposal; returns the proposal id and, for an
    auto-applied wrapper, what to run next."""
    async with svc.db.transaction() as session:
        job = await rec.set_job(session, org_id, job_id, status="running", progress=0.05)
        inp = dict(job.input or {})
        requested_by = job.requested_by
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job))
    proposal_id = UUID(inp["edit_proposal_id"])
    async with svc.db.session() as session:
        row = await session.get_one(EditProposal, proposal_id)
        version_id = row.version_id
        instruction = row.instruction or ""
        selection = dict(row.selection or {})
        given = row.ops if row.ops else None
    data = await svc.version(org_id, version_id, fresh=True)
    kind = str(selection.get("kind", "edit"))
    assumptions: list[str] = []
    runs: list[Any] = []
    planner = "structured"
    issues: list[Issue] = []
    operations: list[EditOperation] = []
    try:
        if given:
            operations = parse_operations(given)
        else:
            async with svc.db.session() as session:
                record_options, superset, _ = await _options(session, org_id, data.spec, data.refs)
            word_times, source = await _word_times(svc, data)
            deps = DirectorDeps(
                bundle=svc.bundle,
                catalog=svc.catalog,
                prompts=PromptLibrary(prompts_root()),
                provider=provider_from_settings(svc.settings),
                refs_for=None,  # type: ignore[arg-type]
                allow_template=svc.settings.app_env in ("dev", "test"),
            )
            sel = selection.get("editor")
            plan = await EditDirector(deps).plan(
                EditRequest(instruction=instruction, selection=Selection.model_validate(sel) if sel else None),
                EditContext(
                    spec=data.spec, refs=superset, word_times=word_times, timing_source=source, options=record_options
                ),  # type: ignore[arg-type]
            )
            operations, assumptions, runs, planner = plan.operations, plan.assumptions, plan.runs, plan.planner
    except Exception as exc:  # EditError and validation errors become the proposal's issues
        found = getattr(exc, "issues", None)
        issues = list(found) if found else [Issue(code="edit_invalid", message=str(exc)[:500])]
    proposal: Proposal | None = None
    actor = "user" if (given or kind != "edit") else "director"
    allow_lock_removal = kind in ("lock_change",) or bool(given and inp.get("structured"))
    if not issues:
        proposal, _, _ = await build_proposal(svc, data, operations, actor=actor, allow_lock_removal=allow_lock_removal)
        issues = proposal.issues
    status = "proposed" if proposal is not None and proposal.status == "proposed" else "failed"
    impact = dict(proposal.impact) if proposal is not None else {}
    impact["issues"] = [asdict(i) for i in issues]
    impact["assumptions"] = assumptions
    impact["planner"] = planner
    impact["actor"] = actor
    impact["allow_lock_removal"] = allow_lock_removal
    if proposal is not None:
        impact["build"] = proposal.build
        impact["side_effects"] = proposal.side_effects
    async with svc.db.transaction() as session:
        row = await session.get_one(EditProposal, proposal_id)
        from ce_core.edit.ops import operation_list

        # the proposal's operations: the planned ones (screen zooms, §27) are applied as proposed
        stored = proposal.operations if proposal is not None and proposal.status == "proposed" else operations
        row.ops = operation_list.dump_python(stored, mode="json") if stored else (row.ops or [])
        row.patch = list(proposal.patch.get("ops", [])) if proposal is not None else []
        row.impact = impact
        row.coverage_delta = proposal.coverage_delta if proposal is not None else {}
        row.alternatives = proposal.alternatives if proposal is not None else []
        row.status = status
        for run in runs:
            session.add(DirectorRun(org_id=org_id, version_id=version_id, **run.row()))
        job = await rec.set_job(
            session,
            org_id,
            job_id,
            status="succeeded" if status == "proposed" else "failed",
            progress=1.0,
            output={"edit_proposal_id": str(proposal_id), "status": status},
            error=None if status == "proposed" else {"code": "edit_invalid", "issues": impact["issues"][:10]},
        )
        project_id = (await session.get_one(Video, data.video_id)).project_id
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job), project_id=project_id)
    await svc.publish(
        org_id,
        EventType.EDIT_PROPOSED,
        {"edit_proposal_id": str(proposal_id), "version_id": str(version_id), "status": status},
        project_id=project_id,
    )
    result: dict[str, Any] = {"edit_proposal_id": str(proposal_id), "status": status}
    if status == "proposed" and inp.get("auto_apply"):
        new_version = UUID(inp["new_version_id"]) if inp.get("new_version_id") else new_id()
        applied = await apply_proposal(svc, org_id, proposal_id, new_version_id=new_version, requested_by=requested_by)
        result.update(applied)
    return result


async def run_apply_job(svc: ExecServices, org_id: UUID, job_id: UUID) -> dict[str, Any]:
    """The `edit_apply` job: applies the proposal, then hands over to generation or previz."""
    async with svc.db.transaction() as session:
        job = await rec.set_job(session, org_id, job_id, status="running", progress=0.1)
        inp = dict(job.input or {})
        requested_by = job.requested_by
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job))
    try:
        result = await apply_proposal(
            svc,
            org_id,
            UUID(inp["edit_proposal_id"]),
            new_version_id=UUID(inp["new_version_id"]),
            requested_by=requested_by,
            alternative=inp.get("alternative"),
        )
    except Exception as exc:
        found = getattr(exc, "issues", None)
        error: dict[str, Any] = {"code": "apply_failed", "message": str(exc)[:500]}
        if found:
            error["issues"] = [asdict(i) for i in found][:10]
        async with svc.db.transaction() as session:
            job = await rec.set_job(session, org_id, job_id, status="failed", progress=1.0, error=error)
        await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job))
        return {"status": "failed", "next_job_id": None}
    async with svc.db.transaction() as session:
        job = await rec.set_job(
            session,
            org_id,
            job_id,
            status="succeeded",
            progress=1.0,
            video_version_id=UUID(result["version_id"]),
            output=result,
        )
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job))
    return {"status": "succeeded", **result}


class _ApplyError(Exception):
    def __init__(self, message: str, issues: Sequence[Issue] = ()) -> None:
        super().__init__(message)
        self.issues = list(issues)


async def apply_proposal(
    svc: ExecServices,
    org_id: UUID,
    proposal_id: UUID,
    *,
    new_version_id: UUID,
    requested_by: UUID | None,
    alternative: str | None = None,
) -> dict[str, Any]:
    """Creates the derived version of a proposal and queues its generation (or previz) job.

    The proposal is recomputed first: it must still validate (records it uses may have changed
    since), and its graph gives the derived version's planned routes and plan report."""
    async with svc.db.session() as session:
        row = await session.get_one(EditProposal, proposal_id)
        if row.status == "applied" and row.result_version_id is not None:
            return {"version_id": str(row.result_version_id), "next_job_id": None, "next_kind": None}
        if row.status != "proposed":
            raise _ApplyError(f"a {row.status} proposal cannot be applied")
        selection = dict(row.selection or {})
        ops = parse_operations(row.ops or [])
        impact = dict(row.impact or {})
        side_effects = list(impact.get("side_effects") or [])
        version_id = row.version_id
        alternatives = list(row.alternatives or [])
        instruction = row.instruction or ""
        parent_row = await session.get_one(VideoVersion, version_id)
        parent_report = await read_plan_report(svc, session, org_id, parent_row)
        parent_number = parent_row.number
    data = await svc.version(org_id, version_id, fresh=True)
    options = BuildOptions(provenance_mode=svc.provenance_mode)
    if alternative and alternative != "full_reperformance":
        chosen = next((a for a in alternatives if a.get("strategy") == alternative), None)
        if chosen is None:
            raise _ApplyError(f"the proposal offers no {alternative} alternative")
        if chosen.get("operations"):
            ops = parse_operations(chosen["operations"])
        if chosen.get("lipsync_patch_shots"):
            options = replace(options, lipsync_patch_shots=frozenset(chosen["lipsync_patch_shots"]))
    proposal, _, refs = await build_proposal(
        svc,
        data,
        ops,
        actor=str(impact.get("actor") or "user"),
        allow_lock_removal=bool(impact.get("allow_lock_removal")),
        options=options,
    )
    if proposal.status != "proposed" or proposal.spec is None or proposal.graph is None:
        raise _ApplyError("the proposal no longer validates", proposal.issues)
    patch = SpecPatch.model_validate(proposal.patch)
    build = {
        **proposal.build,
        "lipsync_patch_shots": sorted({*proposal.build.get("lipsync_patch_shots", []), *options.lipsync_patch_shots}),
    }
    document = apply_patch(data.spec.model_dump(mode="json"), patch)
    kind = str(selection.get("kind", "edit"))
    origin = ORIGIN_OF_KIND.get(kind, VersionOrigin.EDIT)
    spec = VideoSpec.model_validate({**document, "version_id": str(new_version_id)})
    what = instruction or ", ".join(sorted({op.op for op in ops})) or kind
    report = derived_plan_report(
        svc,
        parent_report,
        spec,
        refs,
        proposal.coverage_report,
        note=f"Derived from version {parent_number} ({origin.value}): {what}"[:500],
        detail={"source_version_id": str(version_id), "edit_proposal_id": str(proposal_id)},
        assumptions=list(impact.get("assumptions") or []),
    )
    result = await create_derived_version(
        svc,
        org_id,
        source_version_id=version_id,
        new_version_id=new_version_id,
        document=document,
        origin=origin,
        requested_by=requested_by,
        build=build,
        planned_routes=planned_routes(proposal.graph),
        plan_report=report,
    )
    async with svc.db.transaction() as session:
        row = await session.get_one(EditProposal, proposal_id)
        row.status = "applied"
        row.result_version_id = new_version_id
        await session.flush()
        for effect in side_effects:
            if effect.get("kind") == "memory_feedback":
                await _memory_feedback(session, org_id, data, effect, proposal_id, svc.bundle.vocab)
    return result


async def _memory_feedback(
    session: Any, org_id: UUID, data: VersionData, effect: dict[str, Any], proposal_id: UUID, vocab: Any
) -> None:
    """`memory_feedback` proposes a Creator Memory item; the user confirms it (§18.4)."""
    from ce_memory.store import propose_item

    member = next((c for c in data.spec.cast if c.key == effect.get("character_key")), None)
    creator = data.refs.creators.get(member.creator_version_id) if member else None
    if creator is None:
        return
    await propose_item(
        session,
        org_id,
        vocab=vocab,
        creator_id=creator.creator_id,
        kind=str(effect["memory_kind"]),
        value=dict(effect.get("value") or {}),
        text=str(effect.get("text") or ""),
        source={"type": "user_edit", "version_id": str(data.version_id), "edit_proposal_id": str(proposal_id)},
    )


async def create_derived_version(
    svc: ExecServices,
    org_id: UUID,
    *,
    source_version_id: UUID,
    new_version_id: UUID,
    document: dict[str, Any],
    origin: VersionOrigin,
    requested_by: UUID | None,
    build: dict[str, Any] | None = None,
    planned_routes: dict[str, Any] | None = None,
    plan_report: PlanReport | None = None,
) -> dict[str, Any]:
    """`ce_db.versions.insert_derived_version` plus the plan report and the `version.updated`
    event; returns the version and the queued job (the workflow runs it)."""
    report_id: UUID | str | None = "source"
    stored: tuple[str, int, str] | None = None
    if plan_report is not None:
        stored = await _store_report(svc, plan_report)
    async with svc.db.transaction() as session:
        if stored is not None:
            sha, size, key = stored
            report_id = await rec.register_artifact(
                session,
                org_id,
                sha256=sha,
                kind=ArtifactKind.PLAN_REPORT.value,
                mime="application/json",
                size=size,
                storage_key=key,
            )
        version, job = await insert_derived_version(
            session,
            org_id,
            source_version_id=source_version_id,
            new_version_id=new_version_id,
            document=document,
            origin=origin,
            requested_by=requested_by,
            build=build,
            planned_routes=planned_routes,
            plan_report_artifact_id=report_id,
        )
        state = version.state
        kind = job.kind
        job_id = job.id
        project_id = (await session.get_one(Video, version.video_id)).project_id
    await svc.publish(
        org_id,
        EventType.VERSION_UPDATED,
        {"version_id": str(new_version_id), "state": state, "origin": origin.value},
        project_id=project_id,
    )
    return {"version_id": str(new_version_id), "next_job_id": str(job_id), "next_kind": kind, "state": state}
