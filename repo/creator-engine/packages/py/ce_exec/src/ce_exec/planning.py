"""Planning jobs (§9, §13): `PlanVideoWorkflow` runs the Director and persists the planned version;
`PrevizWorkflow` builds the previz subset and replaces the estimated timings in the plan report
with measured ones before the version reaches `previz_ready`.

The plan job's input is `{request, video_id, version_id, origin, parent_version_id, reuse_snapshots}`;
the version id is allocated by the API (it is in the `202` response) and the row is written once
planning succeeded, because the spec is an immutable identity column (§12.8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_build.refs import SnapshotRef
from ce_core.behavior.plan_report import Finding, PlanReport, StateTiming, event_timings
from ce_core.canonical import canonical_json
from ce_core.enums import ArtifactKind, JobKind, VersionOrigin, VersionState
from ce_core.spec.anchors import WordSpan
from ce_core.spec.videospec import VideoSpec
from ce_db import execution as rec
from ce_db.models.assets import Artifact, GenerationJob
from ce_db.models.assets import ExecutionNode as NodeRow
from ce_db.models.videos import Video, VideoVersion
from ce_director import Director, DirectorDeps, PlanningError, PlanRequest
from ce_director.store import brand_default, load_context, load_facts, persist_plan, pinned_snapshots
from ce_llm import PromptLibrary, prompts_root, provider_from_settings
from ce_obs import get_logger
from ce_obs.events import EventType
from ce_render.timeline import SegmentAudio, build_timeline
from ce_research import GuardedFetcher

from ce_exec.context import ExecServices
from ce_exec.embeddings import embed_texts
from ce_exec.refs_loader import load_refs

__all__ = ["complete_previz", "read_plan_report", "run_plan"]

_log = get_logger("ce.exec.planning")


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


async def read_plan_report(svc: ExecServices, session: Any, org_id: UUID, version: VideoVersion) -> PlanReport | None:
    if version.plan_report_artifact_id is None:
        return None
    artifact = await session.get(Artifact, version.plan_report_artifact_id)
    if artifact is None or artifact.org_id != org_id:
        return None
    return PlanReport.model_validate_json(await svc.content.read_bytes(artifact.sha256))


async def _store_report(svc: ExecServices, report: PlanReport) -> tuple[str, int, str]:
    raw = canonical_json(report.model_dump(mode="json")).encode("utf-8")
    sha = await svc.content.put_bytes(raw, mime="application/json")
    return sha, len(raw), svc.content.key(sha)


async def run_plan(svc: ExecServices, org_id: UUID, job_id: UUID) -> dict[str, Any]:
    """Director stages 1–11 → a `planned` version and a queued previz job (returned)."""
    async with svc.db.transaction() as session:
        job = await rec.set_job(session, org_id, job_id, status="running", progress=0.05)
        inp = dict(job.input or {})
        requested_by = job.requested_by
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job))
    await svc.refresh_catalog()  # the Director's route preview sees recent promotions (stage 8)
    request = PlanRequest.model_validate(inp["request"])
    video_id, version_id = UUID(inp["video_id"]), UUID(inp["version_id"])
    origin = VersionOrigin(inp.get("origin", "plan"))
    parent_id = UUID(inp["parent_version_id"]) if inp.get("parent_version_id") else None
    async with svc.db.session() as session:
        pinned: dict[UUID, tuple[UUID, list[dict[str, object]]]] = {}
        parent = await session.get(VideoVersion, parent_id) if parent_id is not None else None
        if parent is not None and parent.org_id != org_id:
            parent = None
        if parent is not None and inp.get("reuse_snapshots", True):
            pinned = await pinned_snapshots(session, org_id, parent.spec)
        ctx = await load_context(
            session, org_id, video_id=video_id, version_id=version_id, now=datetime.now(UTC), pinned=pinned
        )
        video = await session.get(Video, video_id)
        ctx.brand = await brand_default(session, org_id, project_id=video.project_id if video else None, parent=parent)
        if request.sources:  # persistent research sources (Phase 12): facts are data (I10)
            ctx.facts = await load_facts(
                session, org_id, project_id=video.project_id if video else None, source_ids=request.sources
            )
            ctx.source_ids = sorted({f.source_id for f in ctx.facts}, key=str)

    async def embed(texts: Any, language: str | None) -> tuple[list[list[float]], str] | None:
        embedded = await embed_texts(svc, list(texts), language=language)
        return (embedded.vectors, embedded.model) if embedded is not None else None

    async def refs_for(spec: VideoSpec, snapshots: dict[UUID, SnapshotRef]) -> Any:
        async with svc.db.session() as s:
            return await load_refs(s, org_id, spec, extra_snapshots=snapshots)

    deps = DirectorDeps(
        bundle=svc.bundle,
        catalog=svc.catalog,
        prompts=PromptLibrary(prompts_root()),
        provider=provider_from_settings(svc.settings),
        refs_for=refs_for,  # type: ignore[arg-type]
        fetcher=GuardedFetcher(svc.bundle.app.research.fetch),
        allow_template=svc.settings.app_env in ("dev", "test"),
        embed=embed,
    )
    try:
        outcome = await Director(deps).plan(request, ctx)
    except (PlanningError, LookupError) as exc:
        message = str(exc)[:2000]
        _log.warning("planning failed", job_id=str(job_id), error=message[:300])
        async with svc.db.transaction() as session:
            job = await rec.set_job(
                session,
                org_id,
                job_id,
                status="failed",
                progress=1.0,
                error={"code": "planning_failed", "message": message},
            )
        await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job))
        return {"status": "failed", "version_id": None, "previz_job_id": None}
    sha, size, key = await _store_report(svc, outcome.report)
    async with svc.db.transaction() as session:
        version = await persist_plan(
            session,
            org_id,
            outcome,
            report_sha=sha,
            report_bytes=size,
            report_key=key,
            created_by=requested_by,
            origin=origin,
            parent_version_id=parent_id,
        )
        blocking = sum(1 for f in outcome.report.findings if f.severity == "blocking")
        job = await rec.set_job(
            session,
            org_id,
            job_id,
            status="succeeded",
            progress=1.0,
            video_version_id=version.id,
            output={"version_id": str(version.id), "planner": outcome.planner, "blocking_findings": blocking},
        )
        previz = GenerationJob(
            org_id=org_id,
            kind=JobKind.PREVIZ.value,
            status="queued",
            target_type="video_version",
            target_id=version.id,
            video_version_id=version.id,
            requested_by=requested_by,
            input={"plan_job_id": str(job_id)},
        )
        session.add(previz)
        await session.flush()
        previz.temporal_workflow_id = f"previz-{previz.id}"
        project_id = (await session.get(Video, video_id)).project_id  # type: ignore[union-attr]
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job), project_id=project_id)
    await svc.publish(
        org_id, EventType.VERSION_UPDATED, {"version_id": str(version_id), "state": "planned"}, project_id=project_id
    )
    return {"status": "succeeded", "version_id": str(version_id), "previz_job_id": str(previz.id)}


def _measured_timings(
    spec: VideoSpec, report: PlanReport, words: dict[str, list[tuple[float, float]]]
) -> list[StateTiming]:
    requested = {t.state_key: t.requested_start_s for t in report.state_timings}
    out: list[StateTiming] = []
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        if scene.acting is None:
            continue
        spans = [(s, s.span) for s in scene.acting.states if isinstance(s.span, WordSpan)]
        spans.sort(key=lambda x: words[x[1].start.segment_key][x[1].start.word][0])
        for index, (state, span) in enumerate(spans):
            start = words[span.start.segment_key][span.start.word][0]
            end = (
                words[spans[index + 1][1].start.segment_key][spans[index + 1][1].start.word][0]
                if index + 1 < len(spans)
                else words[span.end.segment_key][span.end.word][1]
            )
            asked = requested.get(state.key)
            out.append(
                StateTiming(
                    state_key=state.key,
                    start_s=round(start, 3),
                    end_s=round(end, 3),
                    requested_start_s=asked,
                    drift_s=round(start - asked, 3) if asked is not None else None,
                )
            )
    return out


async def complete_previz(
    svc: ExecServices,
    org_id: UUID,
    job_id: UUID,
    version_id: UUID,
    statuses: dict[str, str],
    outputs: dict[str, str],
    *,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Measured durations and state timings replace the estimates (§13 previz); the version moves
    to `previz_ready`, or `failed` when a previz node failed."""
    data = await svc.version(org_id, version_id, fresh=True)
    failed = sorted(k for k, s in statuses.items() if s in ("failed", "skipped"))
    async with svc.db.session() as session:
        version = await session.get_one(VideoVersion, version_id)
        report = await read_plan_report(svc, session, org_id, version)
        job_row = await session.get_one(GenerationJob, job_id)
        estimate = float((job_row.input or {}).get("generation_estimate_usd", 0.0))
    state = VersionState.CANCELLED if cancelled else VersionState.FAILED if failed else VersionState.PREVIZ_READY
    values: dict[str, Any] = {}
    if report is not None and state == VersionState.PREVIZ_READY:
        cfg = svc.bundle.app.render.timeline
        segments: dict[str, SegmentAudio] = {}
        for key, sha in outputs.items():
            if key.startswith("align.segment:"):
                out = await svc.docs.output(sha)
                segments[key.split(":", 1)[1]] = SegmentAudio(
                    duration_s=float(out.data["duration_s"]),
                    words=tuple((float(a), float(b)) for a, b in out.data["words"]),
                )
        timeline = build_timeline(data.spec, segments, lead_s=cfg.lead_s, gap_s=cfg.segment_gap_s, tail_s=cfg.tail_s)
        timings = _measured_timings(data.spec, report, {k: list(v) for k, v in timeline.word_times.items()})
        findings = [f for f in report.findings if not (f.kind == "timing" and f.detail.get("state_key"))]
        for timing in timings:
            if timing.drift_s is not None and abs(timing.drift_s) > 1.0:
                findings.append(
                    Finding(
                        kind="timing",
                        severity="info",
                        message=f"{timing.state_key} starts {timing.drift_s:+.1f} s from the requested "
                        f"{timing.requested_start_s:g} s (measured; time is word-anchored, ADR 0004)",
                        detail={"state_key": timing.state_key},
                    )
                )
        for key in sorted(k for k, v in statuses.items() if v == "needs_review" and k.startswith("asr.verify:")):
            segment_key = key.split(":", 1)[1]
            findings.append(
                Finding(
                    kind="other",
                    severity="warning",
                    message=f"Segment {segment_key} still differs from the script after every retry "
                    "(exact-script verification, §21); listen before approving",
                    refs=[f"/script/segments[{segment_key}]/text"],
                    detail={"check": "exact_script", "segment_key": segment_key},
                )
            )
        target = float(data.spec.meta.target_duration_s)
        tolerance = svc.bundle.app.spec.duration_tolerance
        if target and abs(timeline.total_s - target) / target > tolerance:
            findings.append(
                Finding(
                    kind="timing",
                    severity="warning",
                    message=f"Measured duration {timeline.total_s:.1f} s is outside ±{tolerance:.0%} "
                    f"of the {target:g} s target",
                    refs=["/meta/target_duration_s"],
                    detail={"measured_s": round(timeline.total_s, 2)},
                )
            )
        report = report.model_copy(
            update={
                "version_id": version_id,  # restore, branch and duplicate start from the source's report
                "timing_source": "measured",
                "estimated_duration_s": round(timeline.total_s, 2),
                "state_timings": timings,
                "event_timings": event_timings(data.spec, timeline.word_times),
                "findings": findings,
                "cost_estimate_usd": round(estimate, 6),
            }
        )
        sha, size, key = await _store_report(svc, report)
        async with svc.db.transaction() as session:
            values["plan_report_artifact_id"] = await rec.register_artifact(
                session,
                org_id,
                sha256=sha,
                kind=ArtifactKind.PLAN_REPORT.value,
                mime="application/json",
                size=size,
                storage_key=key,
            )
    values["cost_estimate_usd"] = round(estimate, 6)
    async with svc.db.transaction() as session:
        if failed or cancelled:
            await session.execute(
                sa.update(NodeRow)
                .where(
                    NodeRow.org_id == org_id,
                    NodeRow.job_id == job_id,
                    NodeRow.status.in_(("pending", "queued", "running")),
                )
                .values(status="cancelled" if cancelled else "skipped")
            )
        job = await rec.set_job(
            session,
            org_id,
            job_id,
            status="cancelled" if cancelled else "failed" if failed else "succeeded",
            progress=1.0,
            error={"failed_nodes": failed[:50]} if failed else None,
        )
        await rec.set_version_state(session, org_id, version_id, state, **values)
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job), project_id=data.project_id)
    await svc.publish(
        org_id,
        EventType.VERSION_UPDATED,
        {"version_id": str(version_id), "state": state.value},
        project_id=data.project_id,
    )
    if state == VersionState.PREVIZ_READY:
        await svc.publish(org_id, EventType.PREVIZ_READY, {"version_id": str(version_id)}, project_id=data.project_id)
    return {"state": state.value, "failed": failed}
