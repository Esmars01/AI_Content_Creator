"""QC gate reports, the Creative Director critique, consistency runs and the human rating queue
(Phase 11, §20, §26, §30; ADR 0056).

- `GET /v1/versions/{id}/qc`: the version's QC report — every shot gate with its ladder history,
  every take's checks (metric verdicts keyed by adapter, the VLM judge, Performance QA), render
  checks and the coverage triad (requested / compiled / observed);
- critiques: run, list, read, and turn a finding into an edit proposal (applied only by the user);
- consistency: run for a version, read its reports;
- ratings: the human evaluation queue (§20 "same person?" / "in character?" on sampled reports, and
  the low-reliability checks waiting for calibration, §16.2) and rating submission.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_build.kinds import KINDS
from ce_core.edit import parse_operations
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_db.calibration import load_proxy_calibrations, store_proxy_calibrations
from ce_db.models.assets import Artifact
from ce_db.models.behavior import BehaviorObservation, HumanRating, QCReport
from ce_db.models.creators import ConsistencyReport
from ce_db.models.videos import Critique, Render, Take, VideoVersion
from ce_obs import get_logger
from ce_qc.calibration import WORLD_QC_ANALYZER, RatedCheck, calibrate, summarize
from ce_storage.content import ContentStore
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import Field, ValidationError

from ce_api.common import audit
from ce_api.deps import DbSession, PlatformAdmin, Reader, ServicesDep, Writer
from ce_api.jobs import start_studio_job
from ce_api.routers.behavior import _documents
from ce_api.routers.edits import EditAccepted, _propose, _start
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped

router = APIRouter(tags=["qc"])
_log = get_logger("ce.api.qc")

BUILT = ("ready", "needs_review", "approved", "partial")


class JobAccepted(Out):
    job_id: UUID
    status: str = "queued"


# ====================================================================== QC report (§26)
class QCReportOut(Out):
    id: UUID
    target_type: str
    target_id: UUID
    verdict: str
    checks: dict[str, Any]
    created_at: datetime


class VersionQCOut(Out):
    version_id: UUID
    state: str
    flags: list[str]
    gate: dict[str, Any] = Field(description="shots by verdict, retries spent, flagged nodes")
    shots: list[QCReportOut]
    takes: list[QCReportOut]
    world: list[QCReportOut] = Field(
        description="world continuity per shot (qc.world) and across shots (qc.continuity)"
    )
    renders: list[dict[str, Any]]
    coverage: list[dict[str, Any]] = Field(description="requested / compiled / observed per CBS item")


@router.get("/v1/versions/{version_id}/qc", response_model=VersionQCOut)
async def version_qc(version_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> VersionQCOut:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    reports = (
        (
            await session.execute(
                sa.select(QCReport)
                .where(QCReport.org_id == principal.org_id, QCReport.version_id == version.id)
                .order_by(QCReport.created_at)
            )
        )
        .scalars()
        .all()
    )
    takes = {
        t.qc_report_id: t
        for t in (await session.execute(sa.select(Take).where(Take.version_id == version.id))).scalars()
        if t.qc_report_id
    }
    take_out = []
    for r in reports:
        if r.target_type != "take":
            continue
        take = takes.get(r.id)
        checks = {
            **r.checks,
            **({"shot_key": take.shot_key, "take_key": take.take_key, "selected": take.selected} if take else {}),
        }
        take_out.append(
            QCReportOut(
                id=r.id,
                target_type=r.target_type,
                target_id=r.target_id,
                verdict=r.verdict,
                checks=checks,
                created_at=r.created_at,
            )
        )
    shots = [QCReportOut.model_validate(r) for r in reports if r.target_type == "shot"]
    retries = sum(1 for s in shots for h in s.checks.get("ladder", []) if h.get("outcome") == "run")
    renders = [
        {"node_key": key, **{k: data.get(k) for k in ("checks", "passed", "integrated_lufs", "true_peak_dbtp")}}
        for key, data in (await _documents(session, services, principal.org_id, version.id, "qc.render:")).items()
        if "/" not in key
    ]
    coverage: list[dict[str, Any]] = []
    for data in (await _documents(session, services, principal.org_id, version.id, "behavior.coverage:")).values():
        coverage = list(dict(data.get("report") or {}).get("entries", []))
    world = [QCReportOut.model_validate(r) for r in reports if r.target_type == "node"]
    gate = {
        "shots": {v: sum(1 for s in shots if s.verdict == v) for v in ("pass", "warn", "fail")},
        "retries": retries,
        "flagged_shots": sorted(str(s.checks.get("shot_key")) for s in shots if s.verdict == "fail"),
        "renders_failing": [r["node_key"] for r in renders if r.get("passed") is False],
    }
    return VersionQCOut(
        version_id=version.id,
        state=version.state,
        flags=list(version.flags or []),
        gate=gate,
        shots=shots,
        takes=take_out,
        world=world,
        renders=renders,
        coverage=coverage,
    )


# ====================================================================== critique (§26)
class CritiqueOut(Out):
    id: UUID
    version_id: UUID
    render_id: UUID | None
    job_id: UUID | None
    scores: dict[str, Any]
    findings: list[Any]
    created_at: datetime


@router.post("/v1/versions/{version_id}:critique", response_model=JobAccepted, status_code=202)
async def run_critique(
    version_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    """The Creative Director's critique of a rendered version (`CritiqueWorkflow`)."""
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if version.state not in BUILT:
        raise ConflictError(f"a critique needs a rendered version (this one is {version.state})")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.CRITIQUE, target_type="video_version", target_id=version.id,
    )  # fmt: skip
    job.video_version_id = version.id
    await audit(session, principal, "critique.run", "video_version", version.id, request=request)
    return JobAccepted(job_id=job.id)


@router.get("/v1/versions/{version_id}/critiques", response_model=list[CritiqueOut])
async def list_critiques(version_id: UUID, principal: Reader, session: DbSession) -> list[CritiqueOut]:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(Critique)
            .where(Critique.org_id == principal.org_id, Critique.version_id == version.id)
            .order_by(Critique.created_at.desc())
        )
    ).scalars()
    return [CritiqueOut.model_validate(r) for r in rows]


@router.get("/v1/critiques/{critique_id}", response_model=CritiqueOut)
async def get_critique(critique_id: UUID, principal: Reader, session: DbSession) -> CritiqueOut:
    return CritiqueOut.model_validate(await get_scoped(session, Critique, principal.ctx, critique_id, "critique"))


@router.post("/v1/critiques/{critique_id}/findings/{finding_id}:propose", response_model=EditAccepted, status_code=202)
async def propose_finding(
    critique_id: UUID,
    finding_id: str,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> EditAccepted:
    """Turns a finding's operations into an edit proposal (`from_critique_finding`); the proposal is
    applied, like any edit, only when the user applies it (§26, §28)."""
    row = await get_scoped(session, Critique, principal.ctx, critique_id, "critique")
    finding = next((f for f in row.findings or [] if str(f.get("id")) == finding_id), None)
    if finding is None:
        raise NotFoundError(f"finding {finding_id} not found in this critique")
    raw = list(finding.get("proposed_ops") or [])
    if not raw:
        raise ConflictError("this finding has no operation to propose; it needs a script or Director change")
    try:
        operations = parse_operations(raw)
    except ValidationError as exc:
        issues = [Issue("invalid_operation", e["msg"]) for e in exc.errors()[:10]]
        raise InvalidInputError("the finding's operations are not valid edit operations", issues=issues) from exc
    version = await get_scoped(session, VideoVersion, principal.ctx, row.version_id, "version")
    proposal, job, _ = await _propose(
        session,
        principal,
        version,
        kind="from_critique_finding",
        instruction=str(finding.get("issue", ""))[:2000],
        operations=operations,
        editor={"critique_id": str(row.id), "finding_id": finding_id},
    )
    meta = dict((row.scores or {}).get("_meta") or {})
    meta["applied"] = [*meta.get("applied", []), {"finding_id": finding_id, "edit_proposal_id": str(proposal.id)}]
    row.scores = {**(row.scores or {}), "_meta": meta}
    await audit(session, principal, "critique.propose", "critique", row.id, request=request,
                after={"finding_id": finding_id, "edit_proposal_id": str(proposal.id)})  # fmt: skip
    accepted = EditAccepted(edit_proposal_id=proposal.id, job_id=job.id)
    _start(background, services, principal, job)
    return accepted


# ====================================================================== consistency (§20)
class ConsistencyReportOut(Out):
    id: UUID
    creator_id: UUID
    creator_version_id: UUID
    version_id: UUID
    metrics: dict[str, Any]
    verdict: str
    deviations: list[Any]
    created_at: datetime


@router.post("/v1/versions/{version_id}/consistency:run", response_model=JobAccepted, status_code=202)
async def run_consistency(
    version_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if version.state not in BUILT:
        raise ConflictError(f"consistency runs on built versions (this one is {version.state})")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.CONSISTENCY, target_type="video_version", target_id=version.id,
    )  # fmt: skip
    job.video_version_id = version.id
    await audit(session, principal, "consistency.run", "video_version", version.id, request=request)
    return JobAccepted(job_id=job.id)


@router.get("/v1/versions/{version_id}/consistency", response_model=list[ConsistencyReportOut])
async def version_consistency(version_id: UUID, principal: Reader, session: DbSession) -> list[ConsistencyReportOut]:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(ConsistencyReport).where(
                ConsistencyReport.org_id == principal.org_id, ConsistencyReport.version_id == version.id
            )
        )
    ).scalars()
    return [ConsistencyReportOut.model_validate(r) for r in rows]


# ====================================================================== human ratings (§16.8, §20)
QUESTIONS = {
    "consistency": ("same_person", "in_character"),
    "take": ("behavior_item",),
}


class RatingItem(Out):
    target_type: Literal["consistency", "take", "render"]
    target_id: UUID
    question_key: str
    prompt: str
    context: dict[str, Any]


class RatingBody(Body):
    model_config = examples(
        [
            {"target_type": "consistency", "target_id": "0192f0a0-0000-7000-8000-000000000001",
             "question_key": "same_person", "rating": {"value": 4}},
            {"target_type": "take", "target_id": "0192f0a0-0000-7000-8000-000000000002",
             "question_key": "behavior:/scenes[scn_hook]/acting/events[ev_1]|gaze", "rating": {"observed": True}},
        ]
    )  # fmt: skip
    target_type: Literal["consistency", "take", "render"]
    target_id: UUID
    question_key: str = Field(min_length=1, max_length=300)
    rating: dict[str, Any] = Field(description="{value: 1–5} for consistency questions; {observed: bool} for checks")


class RatingOut(Out):
    id: UUID
    target_type: str
    target_id: UUID
    question_key: str
    rating: dict[str, Any]
    rater_user_id: UUID
    created_at: datetime


def _validate_rating(body: RatingBody) -> None:
    if body.target_type == "consistency":
        if body.question_key not in QUESTIONS["consistency"]:
            raise InvalidInputError("unknown question", issues=[Issue("question_key", "same_person or in_character")])
        value = body.rating.get("value")
        if not isinstance(value, int) or not 1 <= value <= 5:
            raise InvalidInputError("rating.value must be 1–5", issues=[Issue("rating", "an integer 1–5")])
    else:
        prefix = "behavior:" if body.target_type == "take" else "world:"
        if not body.question_key.startswith(prefix) or not isinstance(body.rating.get("observed"), bool):
            raise InvalidInputError(
                f"a check rating is {{observed: true|false}} on `{prefix}…`",
                issues=[Issue("rating", "observed must be a boolean")],
            )


@router.get("/v1/ratings/queue", response_model=list[RatingItem])
async def rating_queue(principal: Reader, session: DbSession, limit: int = 50) -> list[RatingItem]:
    """What waits for this user's rating: sampled consistency reports (§20) and take observations of
    low-reliability checks (VLM window questions, relation checks; §16.2 calibration)."""
    limit = max(1, min(limit, 200))
    rated = {
        (r.target_id, r.question_key)
        for r in (
            await session.execute(
                sa.select(HumanRating).where(
                    HumanRating.org_id == principal.org_id, HumanRating.rater_user_id == principal.user_id
                )
            )
        ).scalars()
    }
    items: list[RatingItem] = []
    reports = (
        await session.execute(
            sa.select(ConsistencyReport)
            .where(ConsistencyReport.org_id == principal.org_id)
            .order_by(ConsistencyReport.created_at.desc())
            .limit(200)
        )
    ).scalars()
    prompts = {
        "same_person": "Is this the same person as in the creator's earlier videos? (1 = clearly not, 5 = clearly yes)",
        "in_character": "Does the creator behave in character? (1 = not at all, 5 = fully)",
    }
    for report in reports:
        if not (report.metrics or {}).get("rating_requested"):
            continue
        for question in QUESTIONS["consistency"]:
            if (report.id, question) not in rated:
                items.append(
                    RatingItem(
                        target_type="consistency", target_id=report.id, question_key=question,
                        prompt=prompts[question],
                        context={"version_id": str(report.version_id), "creator_id": str(report.creator_id)},
                    )
                )  # fmt: skip
    observations = (
        await session.execute(
            sa.select(BehaviorObservation)
            .where(
                BehaviorObservation.org_id == principal.org_id,
                BehaviorObservation.level_scope == "take",
                BehaviorObservation.verdict.in_(("CONFIRMED", "PARTIAL", "NOT_OBSERVED", "CONTRADICTED")),
                BehaviorObservation.requested["observation_reliability"].astext == "low",
            )
            .order_by(BehaviorObservation.created_at.desc())
            .limit(200)
        )
    ).scalars()
    for obs in observations:
        key = f"behavior:{obs.item_ref}|{obs.dimension}"
        if obs.take_id is None or (obs.take_id, key) in rated:
            continue
        items.append(
            RatingItem(
                target_type="take",
                target_id=obs.take_id,
                question_key=key,
                prompt=f"In this take, is the requested {obs.dimension} visible? "
                f"(requested: {dict(obs.requested).get('value', obs.item_ref)})",
                context={"version_id": str(obs.version_id), "automatic_verdict": obs.verdict, "method": obs.method},
            )
        )
        rated.add((obs.take_id, key))
    world_reports = (
        await session.execute(
            sa.select(QCReport)
            .where(
                QCReport.org_id == principal.org_id,
                QCReport.target_type == "node",
                QCReport.checks["kind"].astext == "qc.world",
            )
            .order_by(QCReport.created_at.desc())
            .limit(100)
        )
    ).scalars()
    renders: dict[UUID, UUID | None] = {}
    for world_report in world_reports:
        if world_report.version_id is None:
            continue
        if world_report.version_id not in renders:
            renders[world_report.version_id] = (
                await session.execute(
                    sa.select(Render.id)
                    .where(Render.org_id == principal.org_id, Render.version_id == world_report.version_id,
                           Render.is_proxy.is_(False))
                    .limit(1)
                )
            ).scalar_one_or_none()  # fmt: skip
        render_id = renders[world_report.version_id]
        if render_id is None:
            continue
        for check in world_report.checks.get("checks", []):
            if check.get("reliability") != "low" or check.get("passed") is None:
                continue
            key = f"world:{check['check']}:{world_report.checks.get('shot_key')}"
            if (render_id, key) in rated:
                continue
            items.append(
                RatingItem(
                    target_type="render",
                    target_id=render_id,
                    question_key=key,
                    prompt=WORLD_PROMPTS.get(str(check["check"]), "Does the world look as designed in this shot?")
                    + f" (shot {world_report.checks.get('shot_key')})",
                    context={"version_id": str(world_report.version_id), "automatic_passed": check.get("passed"),
                             "details": {k: v for k, v in check.items() if k not in ("check", "gate")}},
                )
            )  # fmt: skip
            rated.add((render_id, key))
    return items[:limit]


WORLD_PROMPTS = {
    "world_lighting": "Does the lighting match the world (colour temperature, brightness, key light on the expected "
    "side)?",
    "world_elements": "Are the room's signature elements visible where they belong?",
}


@router.post("/v1/ratings", response_model=RatingOut, status_code=201)
async def submit_rating(body: RatingBody, request: Request, principal: Writer, session: DbSession) -> RatingOut:
    _validate_rating(body)
    model = {"consistency": ConsistencyReport, "take": Take, "render": Render}[body.target_type]
    await get_scoped(session, model, principal.ctx, body.target_id, body.target_type)
    row = HumanRating(
        org_id=principal.org_id,
        target_type=body.target_type,
        target_id=body.target_id,
        question_key=body.question_key,
        rating=body.rating,
        rater_user_id=principal.user_id,
    )
    session.add(row)
    await session.flush()
    await audit(session, principal, "rating.create", body.target_type, body.target_id, request=request,
                after=body.model_dump(mode="json"))  # fmt: skip
    return RatingOut.model_validate(row)


# ====================================================================== calibration (§16.2, §19.6)
class CalibrationRowOut(Out):
    check: str
    adapter_id: str
    revision: str
    n: int | None
    precision: float | None
    recall: float | None
    f1: float | None
    reliability: str | None
    analyzer: str | None


class CalibrationResult(Out):
    rows: list[CalibrationRowOut]
    ratings_used: int
    skipped: dict[str, int] = Field(description="ratings that could not be paired, by reason")


@router.post("/v1/admin/qc/calibrations:compute", response_model=CalibrationResult)
async def compute_calibrations(
    request: Request, principal: PlatformAdmin, session: DbSession, services: ServicesDep
) -> CalibrationResult:
    """Calibrates the low-reliability checks against human ratings (platform data, every org): the
    behavior proxies per analyzer revision and the world checks; QC then uses the measured
    reliability (ADR 0045, ADR 0056)."""
    ratings = (
        await session.execute(sa.select(HumanRating).where(HumanRating.target_type.in_(("take", "render"))))
    ).scalars().all()  # fmt: skip
    vocab = services.vocab
    rated: list[RatedCheck] = []
    skipped: dict[str, int] = {}
    analyzers_of: dict[UUID, list[dict[str, Any]]] = {}
    store = ContentStore(services.storage, services.settings.s3_bucket_artifacts)

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for rating in ratings:
        human = rating.rating.get("observed")
        if not isinstance(human, bool):
            skip("not a check rating")
            continue
        if rating.target_type == "take" and rating.question_key.startswith("behavior:"):
            item_ref, _, dimension = rating.question_key.removeprefix("behavior:").rpartition("|")
            obs = (
                await session.execute(
                    sa.select(BehaviorObservation)
                    .where(BehaviorObservation.take_id == rating.target_id, BehaviorObservation.item_ref == item_ref,
                           BehaviorObservation.dimension == dimension, BehaviorObservation.level_scope == "take")
                    .limit(1)
                )
            ).scalar_one_or_none()  # fmt: skip
            proxy = vocab.proxies.get(obs.method) if obs is not None else None
            if obs is None or proxy is None:
                skip("no automatic observation with a known proxy")
                continue
            if rating.target_id not in analyzers_of:
                take = await session.get(Take, rating.target_id)
                analyzers_of[rating.target_id] = []
                if take is not None and take.observed_behavior_artifact_id is not None:
                    artifact = await session.get(Artifact, take.observed_behavior_artifact_id)
                    if artifact is not None:
                        try:
                            raw = json.loads(await store.read_bytes(artifact.sha256))
                            observed = dict(dict(raw.get("data") or {}).get("observed") or {})
                            analyzers_of[rating.target_id] = list(observed.get("analyzers", []))
                        except Exception as exc:  # the observation document is gone: the rating cannot pair
                            _log.warning("observation document unreadable", error=str(exc)[:200])
            analyzer = next((a for a in analyzers_of[rating.target_id] if a.get("capability") == proxy.analyzer), None)
            if analyzer is None:
                skip("analyzer revision unknown")
                continue
            rated.append(
                RatedCheck(obs.method, proxy.analyzer, str(analyzer.get("adapter_id")), str(analyzer.get("revision")),
                           obs.verdict == "CONFIRMED", human)
            )  # fmt: skip
        elif rating.target_type == "render" and rating.question_key.startswith("world:"):
            _, check, shot_key = rating.question_key.split(":", 2)
            render = await session.get(Render, rating.target_id)
            report = None
            if render is not None:
                report = (
                    await session.execute(
                        sa.select(QCReport)
                        .where(QCReport.version_id == render.version_id, QCReport.target_type == "node",
                               QCReport.checks["kind"].astext == "qc.world",
                               QCReport.checks["shot_key"].astext == shot_key)
                        .limit(1)
                    )
                ).scalar_one_or_none()  # fmt: skip
            result = next(
                (c for c in (report.checks.get("checks", []) if report else []) if c.get("check") == check), None
            )
            if result is None or result.get("passed") is None:
                skip("no automatic world check")
                continue
            revision = KINDS["qc.world"].impl_version
            rated.append(
                RatedCheck(check, WORLD_QC_ANALYZER, WORLD_QC_ANALYZER, revision, bool(result["passed"]), human)
            )
        else:
            skip("unknown question")
    policy = services.effective.bundle.qc_behavior
    cfg = policy.calibration if policy else None
    rows = calibrate(rated, high=cfg.high, medium=cfg.medium, min_n=cfg.min_n) if cfg else calibrate(rated)
    await store_proxy_calibrations(session, rows)
    await audit(session, principal, "qc.calibrate", "platform", None, request=request,
                after={"rows": len(rows), "ratings": len(rated)})  # fmt: skip
    return CalibrationResult(
        rows=[CalibrationRowOut.model_validate(r) for r in summarize(rows)], ratings_used=len(rated), skipped=skipped
    )


@router.get("/v1/admin/qc/calibrations", response_model=list[CalibrationRowOut])
async def list_calibrations(principal: PlatformAdmin, session: DbSession) -> list[CalibrationRowOut]:
    rows = await load_proxy_calibrations(session)
    return [
        CalibrationRowOut(
            check=r["dimension"],
            adapter_id=r["adapter_id"],
            revision=r["revision"],
            **{k: r["measured"].get(k) for k in ("n", "precision", "recall", "f1", "reliability", "analyzer")},
        )
        for r in rows
    ]
