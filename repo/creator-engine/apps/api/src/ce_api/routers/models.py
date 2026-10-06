"""The model registry (§24, §30): read endpoints for org members, promotion, disabling,
calibration and evidence recording for platform admins.

Rows come from the orchestrator's registry sync (installed manifests → `plugins`/`models`,
`ce_db.registry`). Evidence (`validation`) is recorded by the smoke and bench scripts run on GPU
hosts; promotion needs a verified license closure, `validation ≥ smoke_passed` and a written note
(audited, ADR-style), and records `promotion_basis` (`smoke` until benchmarked: the
"smoke-promoted" badge). Nothing here runs a model; no endpoint claims GPU validation by itself."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

import sqlalchemy as sa
from ce_contracts.manifest import LicenseBlock
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_db.models.assets import Artifact
from ce_db.models.behavior import HumanRating, ModelBehaviorProfile
from ce_db.models.platform import BenchmarkPair, Model, ModelBenchmark, Plugin
from ce_db.registry import (
    KNOB_DIMENSION_PREFIX,
    disable_model,
    promote_model,
    record_validation,
    store_knob_calibration,
)
from ce_policy.license import evaluate
from ce_qc.bench import blind_sides, resolve_preference, verdict
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import Field

from ce_api.common import audit
from ce_api.deps import DbSession, PlatformAdmin, Reader, ServicesDep
from ce_api.jobs import create_job, start_job, start_studio_job
from ce_api.schemas import Body, Out, examples

router = APIRouter(tags=["models"])


class LicenseSummary(Out):
    name: str
    commercial_use: bool
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    url: str = ""


class ModelOut(Out):
    id: UUID
    model_key: str
    plugin_key: str | None
    display_name: str
    capabilities: list[str]
    source_uri: str
    revision: str
    status: str
    validation: str
    promotion_basis: str | None
    smoke_promoted: bool = Field(description="promoted on smoke evidence only (no benchmark yet)")
    vram_min_gb: float
    vram_rec_gb: float
    allowed_envs: list[str]
    license: LicenseSummary
    updated_at: datetime


class KnobCalibrationOut(Out):
    knob: str
    adapter_id: str
    translator_version: str
    revision: str
    calibrated: bool
    rho: float | None = None
    points: list[list[float]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class BenchmarkOut(Out):
    id: UUID
    eval_set_version: str
    verdict: str = Field(description="`pending` until the benchmark's blind pairs are rated (§24)")
    human_scores: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any]
    behavior_profile: dict[str, Any]
    created_at: datetime


class ModelDetailOut(ModelOut):
    dependencies: list[dict[str, Any]]
    obligations: list[Any]
    languages: dict[str, Any]
    evidence: dict[str, Any] = Field(description="smoke/bench reports recorded for this model (quality_scores)")
    knob_calibrations: list[KnobCalibrationOut]
    benchmarks: list[BenchmarkOut] = Field(default_factory=list)


class PromoteBody(Body):
    note: str = Field(min_length=20, max_length=20_000, description="why this model is ready (stored in the audit log)")

    model_config = examples(
        [{"note": "Example only: after bench_passed on <gpu class>, with the run report linked in GPU_VALIDATION.md."}]
    )


class DisableBody(Body):
    reason: str = Field(min_length=3, max_length=2000)

    model_config = examples([{"reason": "Lip drift on long clips; disabled until the next revision."}])


class ValidationBody(Body):
    validation: Literal["smoke_passed", "bench_passed", "failed"]
    report: dict[str, Any] = Field(description="the run report (scripts/smoke or scripts/bench JSON)")

    model_config = examples(
        [{"validation": "smoke_passed", "report": {"example": True, "cases": 3, "passed": 3, "gpu": "<gpu class>"}}]
    )


class CalibrationReportBody(Body):
    report: dict[str, Any] = Field(
        description="a `scripts/calibrate` report: adapter_id, translator_version, revision, knobs"
    )

    model_config = examples(
        [
            {
                "report": {
                    "adapter_id": "infinitetalk",
                    "translator_version": "infinitetalk_v1",
                    "revision": "d59847e",
                    "knobs": {},
                }
            }
        ]
    )


class BenchmarkBody(Body):
    report: dict[str, Any] = Field(description="a `scripts/bench` report (kind bench, backend real)")

    model_config = examples(
        [
            {
                "report": {
                    "kind": "bench",
                    "backend": "real",
                    "adapter_id": "infinitetalk",
                    "eval_set_version": "smoke-v1",
                    "timings": {"_all": {"seconds_per_unit_p50": 28.4}},
                    "cases": [],
                }
            }
        ]
    )


class CalibrationAccepted(Out):
    model_id: UUID
    job_id: UUID
    status: str = "queued"


def _require_real_run(report: dict[str, Any], plugin_key: str | None, *, kind: str) -> None:
    """Rule 5 at the API: a report from a CPU stand-in (`backend: test`), for another adapter or of
    another kind is never evidence (`ce_worker.validation.assert_recordable` checks the same)."""
    issues = []
    if report.get("backend") != "real":
        issues.append(Issue("report.backend", f"{report.get('backend')!r}: only real-backend runs are evidence"))
    if report.get("adapter_id") != plugin_key:
        issues.append(Issue("report.adapter_id", f"{report.get('adapter_id')!r} is not {plugin_key!r}"))
    if report.get("kind") != kind:
        issues.append(Issue("report.kind", f"{report.get('kind')!r}: expected {kind!r}"))
    if issues:
        raise InvalidInputError("the report cannot be recorded", issues=issues)


def _license(data: dict[str, Any]) -> LicenseSummary:
    return LicenseSummary(
        name=str(data.get("name", "")),
        commercial_use=bool(data.get("commercial_use", False)),
        conditions=list(data.get("conditions", [])),
        url=str(data.get("url", "")),
    )


def _out(model: Model, plugin_key: str | None) -> dict[str, Any]:
    return {
        "id": model.id,
        "model_key": model.model_key,
        "plugin_key": plugin_key,
        "display_name": model.display_name,
        "capabilities": list(model.capabilities),
        "source_uri": model.source_uri,
        "revision": model.revision,
        "status": model.status,
        "validation": model.validation,
        "promotion_basis": model.promotion_basis,
        "smoke_promoted": model.promotion_basis == "smoke",
        "vram_min_gb": model.vram_min_gb,
        "vram_rec_gb": model.vram_rec_gb,
        "allowed_envs": list(model.allowed_envs),
        "license": _license(dict(model.license or {})),
        "updated_at": model.updated_at,
    }


async def _get(session: DbSession, model_id: UUID) -> tuple[Model, str | None]:
    row = (
        await session.execute(
            sa.select(Model, Plugin.plugin_key)
            .outerjoin(Plugin, Plugin.id == Model.plugin_id)
            .where(Model.id == model_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("model not found", table="models")
    return row[0], row[1]


@router.get("/v1/models", response_model=list[ModelOut])
async def list_models(
    principal: Reader, session: DbSession, capability: str | None = None, status: str | None = None
) -> list[ModelOut]:
    query = (
        sa.select(Model, Plugin.plugin_key).outerjoin(Plugin, Plugin.id == Model.plugin_id).order_by(Model.model_key)
    )
    if capability:
        query = query.where(Model.capabilities.contains([capability]))
    if status:
        query = query.where(Model.status == status)
    return [ModelOut(**_out(m, key)) for m, key in (await session.execute(query)).all()]


@router.get("/v1/models/{model_id}", response_model=ModelDetailOut)
async def get_model(model_id: UUID, principal: Reader, session: DbSession) -> ModelDetailOut:
    model, plugin_key = await _get(session, model_id)
    knobs = []
    if plugin_key:
        rows = (
            await session.execute(
                sa.select(ModelBehaviorProfile)
                .where(
                    ModelBehaviorProfile.adapter_id == plugin_key,
                    ModelBehaviorProfile.dimension.startswith(KNOB_DIMENSION_PREFIX),
                )
                .order_by(ModelBehaviorProfile.dimension, ModelBehaviorProfile.revision)
            )
        ).scalars()
        for r in rows:
            curve = dict(r.knob_calibration or {})
            knobs.append(
                KnobCalibrationOut(
                    knob=r.dimension[len(KNOB_DIMENSION_PREFIX) :],
                    adapter_id=r.adapter_id,
                    translator_version=r.translator_version,
                    revision=r.revision,
                    calibrated=bool(curve.get("calibrated")),
                    rho=curve.get("rho"),
                    points=[list(map(float, p)) for p in curve.get("points", [])],
                    reasons=list(curve.get("reasons", [])),
                )
            )
    benchmarks = (
        await session.execute(
            sa.select(ModelBenchmark).where(ModelBenchmark.model_id == model.id).order_by(ModelBenchmark.created_at)
        )
    ).scalars()
    return ModelDetailOut(
        **_out(model, plugin_key),
        benchmarks=[_benchmark_out(b) for b in benchmarks],
        dependencies=list(model.dependencies or []),
        obligations=list(model.obligations or []),
        languages=dict(model.languages or {}),
        evidence=dict(model.quality_scores or {}),
        knob_calibrations=knobs,
    )


def _license_reasons(model: Model, services: Any) -> list[str]:
    """The production license decision over the model and every dependency (§24 license closure)."""
    from ce_router import operator_from_config

    settings = services.settings
    operator = operator_from_config(
        services.effective.bundle,
        jurisdiction=settings.operator_jurisdiction,
        revenue_band=settings.operator_revenue_band,
    )
    closure = [(model.model_key, LicenseBlock.model_validate(model.license))]
    for dep in model.dependencies or []:
        closure.append((str(dep.get("ref")), LicenseBlock.model_validate(dep["license"])))
    decision = evaluate(closure, operator, sandbox=False)
    return list(decision.reasons)


@router.post("/v1/admin/models/{model_id}:promote", response_model=ModelOut)
async def promote(
    model_id: UUID,
    body: PromoteBody,
    principal: PlatformAdmin,
    request: Request,
    session: DbSession,
    services: ServicesDep,
) -> ModelOut:
    model, plugin_key = await _get(session, model_id)
    before = {"status": model.status, "validation": model.validation, "promotion_basis": model.promotion_basis}
    reasons = _license_reasons(model, services)
    try:
        model = await promote_model(
            session,
            model_id,
            note=body.note,
            license_ok=not reasons,
            require_bench=services.effective.bundle.app.qc.benchmark.require_bench_for_promotion,
        )
    except ValueError as exc:
        issues = [Issue("license", r) for r in reasons] or [Issue("validation", str(exc))]
        raise ConflictError(str(exc), issues=issues) from exc
    await audit(
        session, principal, "model.promote", "model", model_id, request=request, before=before,
        after={
            "status": model.status, "promotion_basis": model.promotion_basis,
            "validation": model.validation, "note": body.note,
        },
    )  # fmt: skip
    return ModelOut(**_out(model, plugin_key))


@router.post("/v1/admin/models/{model_id}:disable", response_model=ModelOut)
async def disable(
    model_id: UUID, body: DisableBody, principal: PlatformAdmin, request: Request, session: DbSession
) -> ModelOut:
    model, plugin_key = await _get(session, model_id)
    before = {"status": model.status}
    model = await disable_model(session, model_id)
    await audit(
        session, principal, "model.disable", "model", model_id, request=request, before=before,
        after={"status": model.status, "reason": body.reason},
    )  # fmt: skip
    return ModelOut(**_out(model, plugin_key))


@router.post("/v1/admin/models/{model_id}/validations", response_model=ModelOut)
async def record_evidence(
    model_id: UUID, body: ValidationBody, principal: PlatformAdmin, request: Request, session: DbSession
) -> ModelOut:
    """Records a smoke or bench run's verdict (`scripts/smoke/run.py --record`). Rule 5: the report
    must come from a run on real weights; the endpoint stores what the run claims, with the report."""
    model, plugin_key = await _get(session, model_id)
    if not body.report:
        raise InvalidInputError("a validation needs its run report", issues=[Issue("report", "empty")])
    _require_real_run(body.report, plugin_key, kind="smoke" if body.validation != "bench_passed" else "bench")
    if body.validation == "smoke_passed" and (
        body.report.get("verdict") != "smoke_passed"
        or not body.report.get("cases")
        or not all(c.get("passed") for c in body.report["cases"])
    ):
        raise InvalidInputError(
            "the report does not show a passed smoke run",
            issues=[Issue("report.verdict", str(body.report.get("verdict")))],
        )
    if body.validation == "bench_passed":
        raise ConflictError(
            "bench_passed is set by the benchmark runner with human ratings, not by a recorded report",
            issues=[Issue("validation", "bench_passed")],
        )
    before = {"validation": model.validation}
    model = await record_validation(session, model.model_key, body.validation, body.report)
    await audit(
        session, principal, "model.validation", "model", model_id, request=request, before=before,
        after={"validation": model.validation, "report": body.report},
    )  # fmt: skip
    return ModelOut(**_out(model, plugin_key))


@router.post("/v1/admin/models/{model_id}/calibrations", response_model=ModelDetailOut)
async def record_calibration(
    model_id: UUID, body: CalibrationReportBody, principal: PlatformAdmin, request: Request, session: DbSession
) -> ModelDetailOut:
    """Stores a calibration report produced on a GPU host (`scripts/calibrate/run.py --record`)."""
    model, plugin_key = await _get(session, model_id)
    report = body.report
    _require_real_run(report, plugin_key, kind="calibrate")
    for name, curve in dict(report.get("knobs", {})).items():
        if "points" not in curve:
            continue
        await store_knob_calibration(
            session, adapter_id=str(plugin_key), translator_version=str(report.get("translator_version", "none")),
            revision=str(report.get("revision", model.revision)), knob=name, curve=dict(curve), model_id=model_id,
        )  # fmt: skip
    await audit(session, principal, "model.calibration", "model", model_id, request=request, after={"report": report})
    return await get_model(model_id, principal, session)


@router.post("/v1/admin/models/{model_id}/benchmarks", response_model=BenchmarkOut, status_code=201)
async def record_benchmark(
    model_id: UUID, body: BenchmarkBody, principal: PlatformAdmin, request: Request, session: DbSession
) -> BenchmarkOut:
    """Stores a `scripts/bench` run (timings, VRAM, measured behavior on the smoke set) as a
    `model_benchmarks` row with verdict `pending`: pass/fail and `bench_passed` come from the
    benchmark runner (`:benchmark`) with human ratings, never from a recorded report."""
    model, plugin_key = await _get(session, model_id)
    report = body.report
    _require_real_run(report, plugin_key, kind="bench")
    measured = {c["case_id"]: c.get("measurements", {}) for c in report.get("cases", []) if c.get("measurements")}
    row = ModelBenchmark(
        model_id=model.id,
        eval_set_version=str(report.get("eval_set_version", "unknown")),
        metrics={"timings": report.get("timings", {}), "load_seconds": report.get("load_seconds"),
                 "summary": report.get("summary", {}), "host": report.get("host", {}),
                 "git_commit": report.get("git_commit")},
        behavior_profile=measured,
        verdict="pending",
    )  # fmt: skip
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit(
        session, principal, "model.benchmark", "model", model_id, request=request,
        after={"benchmark_id": str(row.id), "eval_set_version": row.eval_set_version},
    )  # fmt: skip
    return _benchmark_out(row)


def _benchmark_out(row: ModelBenchmark) -> BenchmarkOut:
    return BenchmarkOut(
        id=row.id,
        eval_set_version=row.eval_set_version,
        verdict=row.verdict,
        metrics=dict(row.metrics or {}),
        behavior_profile=dict(row.behavior_profile or {}),
        human_scores=dict(row.human_scores or {}),
        created_at=row.created_at,
    )


@router.post("/v1/admin/models/{model_id}:calibrate", response_model=CalibrationAccepted, status_code=202)
async def calibrate(
    model_id: UUID, principal: PlatformAdmin, session: DbSession, services: ServicesDep, background: BackgroundTasks
) -> CalibrationAccepted:
    """Starts `CalibrationWorkflow` (§15.7) for the model's knobs; the job reports the curves (or
    `needs_gpu_host` for GPU families, which calibrate through `scripts/calibrate` on their hosts)."""
    model, plugin_key = await _get(session, model_id)
    if plugin_key is None:
        raise ConflictError("the model has no installed plugin")
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.CALIBRATION,
        target_type="model",
        target_id=model.id,
        requested_by=principal.user_id,
    )
    background.add_task(
        start_job,
        services,
        principal.org_id,
        job.id,
        JobKind.CALIBRATION,
        {"org_id": str(principal.org_id), "job_id": str(job.id), "model_id": str(model.id)},
    )
    return CalibrationAccepted(model_id=model.id, job_id=job.id)


# ====================================================================== benchmark runner (§24, Phase 11)
class BenchmarkAccepted(Out):
    model_id: UUID
    job_id: UUID
    status: str = "queued"


class BenchmarkPairOut(Out):
    id: UUID
    item_key: str
    left_url: str
    right_url: str
    left_mime: str
    right_mime: str
    rated: bool


class RateBody(Body):
    preferred: Literal["left", "right", "tie"]
    note: str = Field(default="", max_length=2000)

    model_config = examples([{"preferred": "left"}])


@router.post("/v1/admin/models/{model_id}:benchmark", response_model=BenchmarkAccepted, status_code=202)
async def benchmark(
    model_id: UUID,
    request: Request,
    principal: PlatformAdmin,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> BenchmarkAccepted:
    """Runs the golden evaluation set on the model next to the production default (`BenchmarkWorkflow`):
    automatic checks, behavior measurements and blind pairs for human rating (§24)."""
    model, plugin_key = await _get(session, model_id)
    if plugin_key is None:
        raise ConflictError("the model has no installed plugin")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.BENCHMARK, target_type="model", target_id=model.id,
    )  # fmt: skip
    await audit(session, principal, "model.benchmark_run", "model", model_id, request=request,
                after={"job_id": str(job.id)})  # fmt: skip
    return BenchmarkAccepted(model_id=model.id, job_id=job.id)


async def _benchmark(session: Any, benchmark_id: UUID) -> ModelBenchmark:
    row = await session.get(ModelBenchmark, benchmark_id)
    if row is None:
        raise NotFoundError("benchmark not found")
    return cast(ModelBenchmark, row)


@router.get("/v1/admin/benchmarks/{benchmark_id}", response_model=BenchmarkOut)
async def get_benchmark(benchmark_id: UUID, principal: PlatformAdmin, session: DbSession) -> BenchmarkOut:
    return _benchmark_out(await _benchmark(session, benchmark_id))


async def _link(session: Any, services: Any, artifact_id: UUID) -> tuple[str, str]:
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise NotFoundError("artifact not found")
    signed = await services.storage.presign_get(
        services.settings.s3_bucket_artifacts, artifact.storage_key,
        ttl_s=services.config.storage.presign_ttl_s,
    )  # fmt: skip
    return str(signed.url), str(artifact.mime)


@router.get("/v1/admin/benchmarks/{benchmark_id}/pairs", response_model=list[BenchmarkPairOut])
async def benchmark_pairs(
    benchmark_id: UUID, principal: PlatformAdmin, session: DbSession, services: ServicesDep
) -> list[BenchmarkPairOut]:
    """The blind pairs for this rater: which output is on the left is decided per pair and rater, so
    the candidate is never identifiable by its side (§24)."""
    bench = await _benchmark(session, benchmark_id)
    pairs = (
        await session.execute(sa.select(BenchmarkPair).where(BenchmarkPair.benchmark_id == bench.id))
    ).scalars().all()  # fmt: skip
    mine = {
        r.target_id
        for r in (
            await session.execute(
                sa.select(HumanRating).where(
                    HumanRating.target_type == "benchmark_pair", HumanRating.rater_user_id == principal.user_id
                )
            )
        ).scalars()
    }
    out = []
    for pair in sorted(pairs, key=lambda p: p.item_key):
        left, right = blind_sides(str(pair.id), str(principal.user_id))
        ids = {"a": pair.a_artifact_id, "b": pair.b_artifact_id}
        left_url, left_mime = await _link(session, services, ids[left])
        right_url, right_mime = await _link(session, services, ids[right])
        out.append(
            BenchmarkPairOut(
                id=pair.id, item_key=pair.item_key, left_url=left_url, right_url=right_url,
                left_mime=left_mime, right_mime=right_mime, rated=pair.id in mine,
            )
        )  # fmt: skip
    return out


@router.post("/v1/admin/benchmarks/{benchmark_id}/pairs/{pair_id}:rate", response_model=BenchmarkOut)
async def rate_pair(
    benchmark_id: UUID,
    pair_id: UUID,
    body: RateBody,
    request: Request,
    principal: PlatformAdmin,
    session: DbSession,
    services: ServicesDep,
) -> BenchmarkOut:
    """Records a blind preference, then re-decides the benchmark (`ce_qc.bench.verdict`): a pass sets
    the model's `validation: bench_passed` (§24)."""
    bench = await _benchmark(session, benchmark_id)
    pair = await session.get(BenchmarkPair, pair_id)
    if pair is None or pair.benchmark_id != bench.id:
        raise NotFoundError("pair not found")
    if bench.verdict != "pending":
        raise ConflictError(f"the benchmark is decided ({bench.verdict})")
    choice = resolve_preference(str(pair.id), str(principal.user_id), body.preferred)
    await session.execute(
        sa.delete(HumanRating).where(
            HumanRating.target_type == "benchmark_pair", HumanRating.target_id == pair.id,
            HumanRating.rater_user_id == principal.user_id,
        )
    )  # fmt: skip
    session.add(
        HumanRating(
            org_id=principal.org_id, target_type="benchmark_pair", target_id=pair.id, question_key="preference",
            rating={"choice": choice, "shown": body.preferred, "note": body.note}, rater_user_id=principal.user_id,
        )
    )  # fmt: skip
    await session.flush()
    pair_ids = [
        str(p) for p in (
            await session.execute(sa.select(BenchmarkPair.id).where(BenchmarkPair.benchmark_id == bench.id))
        ).scalars()
    ]  # fmt: skip
    votes: dict[str, list[str]] = {}
    for rating in (
        await session.execute(
            sa.select(HumanRating).where(
                HumanRating.target_type == "benchmark_pair", HumanRating.target_id.in_([UUID(p) for p in pair_ids])
            )
        )
    ).scalars():
        votes.setdefault(str(rating.target_id), []).append(str(rating.rating.get("choice")))
    decided = verdict(
        list(dict(bench.metrics or {}).get("checks", [])), pair_ids, votes, services.effective.bundle.app.qc.benchmark
    )
    bench.human_scores = {**dict(bench.human_scores or {}), "rated": sum(1 for p in pair_ids if votes.get(p)),
                          "status": decided}  # fmt: skip
    if decided["verdict"] != "pending":
        bench.verdict = decided["verdict"]
        if decided["verdict"] == "pass":
            model = await session.get_one(Model, bench.model_id)
            before = model.validation
            model.validation = "bench_passed"
            if model.status == "production":
                model.promotion_basis = "bench"
            await audit(session, principal, "model.validation", "model", model.id, request=request,
                        before={"validation": before},
                        after={"validation": "bench_passed", "benchmark_id": str(bench.id)})  # fmt: skip
    await session.flush()
    return _benchmark_out(bench)
