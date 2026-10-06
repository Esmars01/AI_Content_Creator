"""`BenchmarkWorkflow` (§24, ADR 0056): a candidate engine on the golden evaluation set next to the
production default of each capability.

Stages (the studio loop, ADR 0055):
- `benchmark/start`: the cases of `eval/cases.yaml` (which extends the smoke set) the engine
  declares; their media brought into the content store; one model call per case × seed for the
  candidate (pinned) and, for the pairwise capabilities, for the baseline — the adapter the router
  picks for the capability with the candidate excluded (production engines only). Calls run on the
  fleet like every model call (GPU families on sandbox pools);
- `benchmark/store`: the automatic checks of every candidate output (`ce_worker.validation`), the
  behavior measurements (`measure`), the blind pairs (candidate vs baseline, same case and seed) for
  human rating, the `model_benchmarks` row with verdict `pending` (or `fail` on failed checks), and
  bench-sourced `model_behavior_profiles` rows with the measurements.

The verdict becomes `pass` / `fail` when the pairs are rated (`ce_qc.bench.verdict`); `pass` sets the
model's `validation: bench_passed`.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_contracts.capabilities import capability as capability_spec
from ce_contracts.common import ArtifactRef
from ce_db.models.behavior import ModelBehaviorProfile
from ce_db.models.platform import BenchmarkPair, Model, ModelBenchmark, Plugin
from ce_qc.bench import verdict
from ce_router import RouteRequest, RoutingError, route
from ce_storage.content import StorageRunContext
from ce_worker.validation import SmokeSet, applicable_cases, load_smoke_set, measure, run_checks

from ce_exec.context import ExecServices
from ce_exec.studio import ModelCall, StudioContext, StudioError, StudioStep, stage

__all__ = ["benchmark_start", "benchmark_store", "eval_set"]


def eval_set(svc: ExecServices) -> SmokeSet:
    path = Path(svc.bundle.app.qc.benchmark.eval_set)
    if not path.is_absolute():
        path = svc.bundle.root.parent / path
    if not path.is_file():
        raise StudioError(f"the evaluation set {path} is not installed here")
    evalset = load_smoke_set(path)
    bad = evalset.verify_media()
    if bad:
        raise StudioError(f"evaluation media do not match their pinned sha256: {bad}")
    return evalset


def _media_names(value: Any) -> set[str]:
    if isinstance(value, str) and value.startswith("$") and not value.startswith("$ref:"):
        return {value[1:]}
    if isinstance(value, list):
        return {n for v in value for n in _media_names(v)}
    if isinstance(value, dict):
        return {n for v in value.values() for n in _media_names(v)}
    return set()


def _resolve(value: Any, refs: dict[str, dict[str, Any]], evalset: SmokeSet) -> Any:
    import json

    if isinstance(value, str):
        if value.startswith("$ref:"):
            return evalset.reference_text[value[5:]]
        if value.startswith("$"):
            return refs[value[1:]]
        if value.startswith("@"):
            return json.loads((evalset.root / value[1:]).read_text(encoding="utf-8"))
        return value
    if isinstance(value, list):
        return [_resolve(v, refs, evalset) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, refs, evalset) for k, v in value.items()}
    return value


@stage("benchmark", "start")
async def benchmark_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    model_id = UUID(ctx.target_id)
    async with svc.db.session() as session:
        model = await session.get(Model, model_id)
        plugin_key = (
            (await session.execute(sa.select(Plugin.plugin_key).where(Plugin.id == model.plugin_id))).scalar()
            if model is not None and model.plugin_id is not None
            else None
        )
    catalog = svc.catalog
    if plugin_key is None or plugin_key not in catalog.manifests:
        raise StudioError("the model's plugin is not installed here")
    manifest = catalog.manifests[plugin_key]
    evalset = eval_set(svc)
    cases, skipped, uncovered = applicable_cases(manifest, evalset)
    if not cases:
        raise StudioError(f"no case of {evalset.version} applies to {plugin_key}")
    scratch = svc.scratch("bench")
    run_ctx = StorageRunContext(svc.content, scratch)
    refs: dict[str, dict[str, Any]] = {}
    try:
        for name in sorted({n for c in cases for n in _media_names(c.request)}):
            entry = evalset.media[name]
            ref = await run_ctx.write_artifact(evalset.media_path(name), str(entry.get("kind", "other")), role=name)
            refs[name] = ref.model_dump(mode="json")
    finally:
        run_ctx.cleanup()
    seeds = int(svc.bundle.app.qc.benchmark.seeds)
    baselines: dict[str, str | None] = {}
    for capability in sorted({c.capability for c in cases if c.capability in evalset.pairwise}):
        try:
            decision = route(
                RouteRequest(capability=capability, routing_profile="draft", exclude=frozenset({plugin_key})), catalog
            )
            baselines[capability] = decision.adapter_id
        except RoutingError:
            baselines[capability] = None
    calls: list[ModelCall] = []
    for case in cases:
        request = _resolve(case.request, refs, evalset)
        if "text" in request and "words" in capability_spec(case.capability).request.model_fields:
            request.setdefault("words", str(request["text"]).split())
        sides = [("a", plugin_key)]
        baseline = baselines.get(case.capability)
        if baseline:
            sides.append(("b", baseline))
        for seed in range(seeds):
            for side, adapter in sides:
                calls.append(
                    ModelCall(
                        key=f"bench:{case.id}:{seed}:{side}",
                        capability=case.capability,
                        request=request,
                        seed=seed,
                        language=case.language,
                        adapter_id=adapter,
                        prefer_mock=bool(catalog.manifests[adapter].mock),
                    )
                )
    return StudioStep(
        calls=calls,
        next="store",
        progress=0.3,
        data={
            "adapter_id": plugin_key,
            "eval_set": evalset.version,
            "cases": [c.id for c in cases],
            "skipped": skipped,
            "uncovered": uncovered,
            "baselines": baselines,
        },
    )


@stage("benchmark", "store")
async def benchmark_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    model_id = UUID(ctx.target_id)
    evalset = eval_set(svc)
    cases = {c.id: c for c in evalset.cases}
    outputs = {o["key"]: o for o in data.get("outputs", [])}
    checks: list[dict[str, Any]] = []
    measures: dict[str, list[dict[str, Any]]] = {}
    pairs: list[tuple[str, str, str]] = []  # (item_key, candidate output, baseline output)
    scratch = svc.scratch("bench")
    run_ctx = StorageRunContext(svc.content, scratch)

    async def no_roundtrip(layer: str, request: Any) -> Any:
        return None

    analyzers: dict[str, Any] = {}
    for capability in ("face.landmarks",):  # the measurements of `measure` (recorded, never pass/fail)
        try:
            chosen = route(RouteRequest(capability=capability, routing_profile="draft"), svc.catalog)
            analyzers[capability] = await svc.adapter(chosen.adapter_id)
        except (RoutingError, LookupError):
            continue

    try:
        for key, call in sorted(outputs.items()):
            _, case_id, seed, side = key.split(":")
            case = cases[case_id]
            result = capability_spec(case.capability).result.model_validate(call.get("result") or {})
            if side == "a":
                verdicts = await run_checks(case, result, run_ctx, evalset, no_roundtrip)
                checks.append(
                    {
                        "case": case_id,
                        "seed": int(seed),
                        "side": side,
                        "passed": all(v.passed for v in verdicts if not v.name.startswith("watermark")),
                        "checks": [v.as_dict() for v in verdicts],
                        "mock": bool(call.get("mock")),
                    }
                )
                if case.measure:
                    request = (
                        capability_spec(case.capability).request.model_validate(
                            {k: v for k, v in dict(call.get("request") or {}).items()}
                        )
                        if call.get("request")
                        else None
                    )
                    measured = await measure(case.measure, case, request, result, run_ctx, analyzers)
                    measures.setdefault(case_id, []).append({"seed": int(seed), **measured})
            partner = outputs.get(f"bench:{case_id}:{seed}:b")
            if side == "a" and partner is not None and case.capability in evalset.pairwise:
                media_a = _primary_ref(call.get("result") or {})
                media_b = _primary_ref(partner.get("result") or {})
                if media_a and media_b:
                    pairs.append((f"{case_id}:{seed}", media_a, media_b))
    finally:
        run_ctx.cleanup()
    from ce_db.models.assets import Artifact

    async with svc.db.transaction() as session:
        artifact_ids: dict[str, UUID] = {}
        for _, a, b in pairs:
            for sha in (a, b):
                if sha not in artifact_ids:
                    found = (
                        await session.execute(sa.select(Artifact.id).where(Artifact.sha256 == sha).limit(1))
                    ).scalar_one_or_none()
                    if found is not None:
                        artifact_ids[sha] = found
        pair_rows = [(k, a, b) for k, a, b in pairs if a in artifact_ids and b in artifact_ids]
        status = verdict(checks, [k for k, _, _ in pair_rows], {}, svc.bundle.app.qc.benchmark)
        bench = ModelBenchmark(
            model_id=model_id,
            eval_set_version=str(data["eval_set"]),
            metrics={
                "adapter_id": data["adapter_id"],
                "checks": checks,
                "check_pass_rate": status["check_pass_rate"],
                "baselines": data.get("baselines", {}),
                "skipped": data.get("skipped", []),
                "uncovered": data.get("uncovered", []),
                "seeds": int(svc.bundle.app.qc.benchmark.seeds),
                "job_id": ctx.job_id,
                "mock": any(c.get("mock") for c in checks),
            },
            behavior_profile={"measures": measures},
            human_scores={"pairs": len(pair_rows), "rated": 0, "status": status},
            verdict="fail" if status["verdict"] == "fail" else "pending",
        )
        session.add(bench)
        await session.flush()
        for item_key, a, b in pair_rows:
            session.add(
                BenchmarkPair(
                    benchmark_id=bench.id,
                    item_key=item_key,
                    a_artifact_id=artifact_ids[a],
                    b_artifact_id=artifact_ids[b],
                )
            )
        manifest = svc.catalog.manifests.get(str(data["adapter_id"]))
        revision = (manifest.primary_model.source.revision if manifest and manifest.primary_model else "unknown") or "-"
        for case_id, rows in measures.items():
            for dimension in sorted({k for r in rows for k in r if k != "seed"}):
                stmt = sa.select(ModelBehaviorProfile).where(
                    ModelBehaviorProfile.adapter_id == str(data["adapter_id"]),
                    ModelBehaviorProfile.translator_version == "bench",
                    ModelBehaviorProfile.revision == revision,
                    ModelBehaviorProfile.dimension == f"bench:{dimension}",
                    ModelBehaviorProfile.language == (cases[case_id].language or "und"),
                    ModelBehaviorProfile.source == "bench",
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                measured = {
                    "case": case_id,
                    "runs": [r.get(dimension) for r in rows],
                    "eval_set": data["eval_set"],
                    "benchmark_id": str(bench.id),
                    "note": "bench measurements; success rates come from judged behavior fixtures",
                }
                if row is None:
                    session.add(
                        ModelBehaviorProfile(
                            model_id=model_id,
                            adapter_id=str(data["adapter_id"]),
                            translator_version="bench",
                            revision=revision,
                            dimension=f"bench:{dimension}",
                            language=cases[case_id].language or "und",
                            source="bench",
                            measured=measured,
                        )
                    )
                else:
                    row.measured = measured
        benchmark_id = str(bench.id)
    pass_rate = statistics.fmean(1.0 if c["passed"] else 0.0 for c in checks) if checks else 0.0
    return StudioStep(
        done=True,
        result={"benchmark_id": benchmark_id, "pairs": len(pair_rows), "check_pass_rate": round(pass_rate, 4)},
    )


def _primary_ref(result: dict[str, Any]) -> str | None:
    """The sha256 of a result's main media (video, image, audio, media) for the blind pair."""
    for field in ("video", "image", "audio", "media"):
        value = result.get(field)
        if isinstance(value, dict) and value.get("sha256"):
            return str(ArtifactRef.model_validate(value).sha256)
    return None
