"""The QC gate (§26, ADR 0056): the decision ladder per failed shot, executed by the build workflow.

A shot's takes are judged by their `qc.shot` nodes (metric thresholds keyed by adapter, the VLM
judge, Performance QA). When no take passes, `shot_gate` names the rung of the ladder and the nodes
to re-run: the failing take's generators (or the target Performance QA names, §15.7) and every
node between them and the take's `qc.shot` — never anything else, so only failed nodes re-run.
The workflow runs them (attempt seed, or the fallback route), asks again, and when the gate passes
or the ladder ends, `accept_outputs` records the accepted attempt: BuildManifest rows (deferred for
nodes inside a QC loop), the cache, take artifacts, the shot's QC report with the ladder history,
and `qc_rejected` on every attempt that lost (which also evicts it from the cache, §12.2).

Budgets come from the tier (`config/qc/{tier}.yaml`): retries per node and per version, in count
and in USD, counted from the version's `job_attempts` (QC retries and fallbacks, the exact-script
loop included).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_build.manifest import records_for
from ce_build.seeds import attempt_seed
from ce_core.build import ExecutionGraph, ExecutionNode
from ce_db import execution as rec
from ce_db.models.assets import Artifact, JobAttempt
from ce_db.models.assets import ExecutionNode as NodeRow
from ce_db.models.behavior import ModelBehaviorProfile, QCReport
from ce_qc.ladder import Budgets, LadderInput, Usage, next_rung
from pydantic import BaseModel, Field

from ce_exec.context import ExecServices
from ce_exec.outputs import NodeOutput

__all__ = ["AcceptInput", "GateDecision", "GateInput", "accept_outputs", "gate_members", "shot_gate"]

GENERATORS = frozenset({"avatar.render", "video.broll"})
GATED_KINDS = frozenset({"image.keyframe", "avatar.render", "video.broll", "behavior.observe", "qc.shot"})


class GateInput(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    graph_sha: str
    shot_key: str
    qc: dict[str, str] = Field(description="the shot's qc.shot node keys → output sha256 of the current attempt")
    history: list[dict[str, Any]] = Field(default_factory=list, description="ladder steps of this gate so far")
    attempt: int = Field(default=0, ge=0, description="re-runs made so far (the next re-run is attempt + 1)")


class GateDecision(BaseModel):
    action: Literal["pass", "retry", "fallback", "needs_review"]
    step: str
    reason: str = ""
    take: int | None = Field(default=None, description="the take the ladder works on")
    targets: list[str] = Field(default_factory=list, description="nodes re-run with the attempt seed or fallback")
    rerun: list[str] = Field(default_factory=list, description="every node to re-run, in dependency order")
    qc_retry: int = 0
    route_override: str | None = Field(default=None, description="fallback adapter for `targets` with a route")
    score: float | None = Field(default=None, description="the best take score of the current attempt")
    passed_takes: list[int] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[dict[str, Any]] = Field(default_factory=list)


class AcceptInput(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    graph_sha: str
    accepted: dict[str, str] = Field(description="node key → accepted output sha256")
    rejected: dict[str, list[str]] = Field(default_factory=dict, description="node key → losing output sha256s")
    retries: dict[str, int] = Field(default_factory=dict, description="node key → qc_retry of the accepted attempt")
    routes: dict[str, str] = Field(
        default_factory=dict, description="node key → fallback adapter of the accepted attempt"
    )
    shot_key: str | None = None
    verdict: Literal["pass", "warn", "fail"] | None = None
    report: dict[str, Any] | None = Field(default=None, description="the shot gate report (ladder history)")


def gate_members(graph_nodes: Iterable[tuple[str, str, list[str], str | None]]) -> dict[str, set[str]]:
    """Per shot: the nodes inside its QC loop — the shot's `qc.shot` nodes and their ancestors of a
    gated kind in the same shot (generators, keyframe, observation). Input rows: (key, kind, deps,
    shot_key). Deterministic; used by the workflow."""
    rows = {key: (kind, deps, shot) for key, kind, deps, shot in graph_nodes}
    members: dict[str, set[str]] = {}
    for key, (kind, _, shot) in rows.items():
        if kind != "qc.shot" or shot is None:
            continue
        found = members.setdefault(shot, set())
        stack = [key]
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            for dep in rows[current][1]:
                dep_kind, _, dep_shot = rows.get(dep, ("", [], None))
                if dep_kind in GATED_KINDS and dep_shot == shot:
                    stack.append(dep)
    return members


def _take_failures(output: NodeOutput) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(metric/judge failures, Performance QA retries) of one take's qc.shot output."""
    data = output.data
    failures: list[dict[str, Any]] = []
    for capability, verdict in sorted(dict(data.get("verdicts", {})).items()):
        if verdict.get("passed") is False and not verdict.get("advisory"):
            failures.append(
                {"check": capability, "adapter_id": verdict.get("adapter_id"), "reason": verdict.get("reason")}
            )
    judge = dict(data.get("vlm_judge") or {})
    if judge.get("gating") and judge.get("critical"):
        failures.append({"check": "vlm_judge", "adapter_id": judge.get("adapter_id"), "reason": "critical defects"})
    decision = dict(dict(data.get("behavior") or {}).get("decision") or {})
    retries = list(decision.get("retries", [])) if decision.get("action") == "retry" else []
    return failures, retries


def _descendants(graph: ExecutionGraph, roots: set[str]) -> set[str]:
    dependents: dict[str, list[str]] = {}
    for node in graph.nodes:
        for dep in node.deps:
            dependents.setdefault(dep, []).append(node.key)
    out: set[str] = set()
    stack = list(roots)
    while stack:
        key = stack.pop()
        for child in dependents.get(key, []):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def _ancestors(by_key: dict[str, ExecutionNode], roots: set[str]) -> set[str]:
    out: set[str] = set()
    stack = list(roots)
    while stack:
        key = stack.pop()
        for dep in by_key[key].deps:
            if dep not in out:
                out.add(dep)
                stack.append(dep)
    return out


def _rerun_set(
    graph: ExecutionGraph, shot_key: str, qc_keys: set[str], targets: set[str]
) -> tuple[list[str], str | None]:
    """Targets plus every node between them and the shot's qc.shot nodes, in graph order — or the
    reason the re-run would touch a node outside the shot's QC loop."""
    by_key = graph.by_key()
    feeding = _ancestors(by_key, qc_keys) | qc_keys
    rerun = targets | (_descendants(graph, targets) & feeding)
    for key in sorted(rerun):
        for child in _descendants(graph, {key}):
            if child in rerun:
                continue
            node = by_key[child]
            waits = set(node.deps) & qc_keys or _ancestors(by_key, {child}) & qc_keys
            if not waits and node.group == "scene":
                return [], f"re-running {key} would invalidate {child}, which does not wait for the shot's QC"
    order = [n.key for n in graph.nodes if n.key in rerun]
    return order, None


async def _usage(svc: ExecServices, org_id: UUID, version_id: UUID) -> Usage:
    async with svc.db.session() as session:
        count, usd = (
            await session.execute(
                sa.select(sa.func.count(JobAttempt.id), sa.func.coalesce(sa.func.sum(JobAttempt.cost_usd), 0))
                .join(NodeRow, (NodeRow.id == JobAttempt.node_id) & (NodeRow.org_id == JobAttempt.org_id))
                .where(
                    JobAttempt.org_id == org_id,
                    NodeRow.version_id == version_id,
                    JobAttempt.reason.in_(("qc_retry", "fallback")),
                )
            )
        ).one()
    return Usage(version_retries=int(count), version_usd=float(usd))


async def _profile_favors(
    svc: ExecServices, planned: str, fallback: str, dimensions: set[str], language: str
) -> tuple[bool, str]:
    """`fallback_route_if_measured_better` (§16.5): the fallback's measured success rate beats the
    planned engine's on every failing dimension (bench/production profiles; mock ones only with
    MOCK_GPU)."""
    sources = ["bench", "production", *(["mock"] if svc.settings.mock_gpu else [])]
    async with svc.db.session() as session:
        rows = (
            await session.execute(
                sa.select(ModelBehaviorProfile).where(
                    ModelBehaviorProfile.adapter_id.in_((planned, fallback)),
                    ModelBehaviorProfile.dimension.in_(sorted(dimensions)),
                    ModelBehaviorProfile.source.in_(sources),
                    ModelBehaviorProfile.language.in_((language, "und")),
                )
            )
        ).scalars()
        rates: dict[tuple[str, str], float] = {}
        for row in rows:
            rate = row.measured.get("success_rate")
            if isinstance(rate, (int, float)):
                key = (row.adapter_id, row.dimension)
                rates[key] = max(rates.get(key, 0.0), float(rate))
    worse = []
    for dimension in sorted(dimensions):
        theirs, ours = rates.get((fallback, dimension)), rates.get((planned, dimension))
        if theirs is None:
            worse.append(f"{dimension}: no measured profile for {fallback}")
        elif ours is not None and theirs <= ours:
            worse.append(f"{dimension}: {fallback} {theirs:.2f} ≤ {planned} {ours:.2f}")
    if worse:
        return False, "the measured profiles do not favor the fallback (" + "; ".join(worse) + ")"
    return True, ""


def _targets_for(
    by_key: dict[str, ExecutionNode],
    shot_key: str,
    take: int,
    failures: list[dict[str, Any]],
    retries: list[dict[str, Any]],
) -> tuple[set[str], str | None]:
    generators = {n.key for n in by_key.values() if n.kind in GENERATORS and n.shot_key == shot_key and n.take == take}
    targets: set[str] = set(generators) if failures else set()
    for retry in retries:
        kinds = [str(k) for k in retry.get("rerun", [])]
        if not kinds:
            return set(), f"{retry.get('item_ref')} ({retry.get('dimension')}): no node can regenerate it"
        for kind in kinds:
            if kind == "avatar.render":
                targets |= generators
            elif kind == "image.keyframe" and f"image.keyframe:{shot_key}" in by_key:
                targets.add(f"image.keyframe:{shot_key}")
            else:  # tts.segment (the whole voice), post.expression, render.final, needs_review
                return set(), (
                    f"{retry.get('item_ref')} ({retry.get('dimension')}): the retry target {kind} is outside the "
                    "shot's QC loop"
                )
    return targets, None


async def shot_gate(svc: ExecServices, inp: GateInput) -> GateDecision:
    org_id, version_id = UUID(inp.org_id), UUID(inp.version_id)
    data = await svc.version(org_id, version_id)
    graph = await svc.graph(inp.graph_sha)
    by_key = graph.by_key()
    takes: dict[int, tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for key, sha in sorted(inp.qc.items()):
        output = await svc.docs.output(sha)
        failures, retries = _take_failures(output)
        takes[int(by_key[key].take or 1)] = (float(output.data.get("score", 0.0) or 0.0), failures, retries)
    passed = sorted(t for t, (_, f, r) in takes.items() if not f and not r)
    best = max(takes, key=lambda t: (takes[t][0], -t))
    score = takes[best][0]
    if passed:
        return GateDecision(action="pass", step="pass", take=best, score=score, passed_takes=passed)
    _, failures, retries = takes[best]
    report_failures = [*failures, *({"check": "performance_qa", **r} for r in retries)]
    tier = svc.bundle.qc_tiers.get(str(data.spec.meta.quality_tier))
    if tier is None:
        return GateDecision(action="needs_review", step="needs_review", reason="no QC tier configured", take=best)
    targets, why_not = _targets_for(by_key, inp.shot_key, best, failures, retries)
    rerun: list[str] = []
    if targets:
        rerun, why_not = _rerun_set(graph, inp.shot_key, set(inp.qc), targets)
    catalog = data.catalog or svc.catalog
    tried = {str(h.get("adapter_id")) for h in inp.history if h.get("step") == "fallback_route"}
    routed = [by_key[k] for k in sorted(targets) if by_key[k].route is not None and by_key[k].kind in GENERATORS]
    fallback, fallback_reason = None, "no fallback route"
    if routed:
        planned = routed[0].route
        assert planned is not None
        options = [a for a in planned.fallbacks if a in catalog.manifests and a not in tried]
        if options:
            fallback = options[0]
            if retries and not failures:  # Performance QA: only when the measured profile is better
                dims = {str(r.get("dimension")) for r in retries}
                ok, fallback_reason = await _profile_favors(
                    svc, planned.adapter_id, fallback, dims, str(data.spec.meta.language)
                )
                fallback = fallback if ok else None
        elif planned.fallbacks:
            fallback_reason = "every fallback route was tried or is not installed"
    cheaper = None
    cheaper_reason = "no cheaper fix for " + ", ".join(sorted({str(f["check"]) for f in report_failures}))
    if failures and all(f["check"] == "qc.lipsync" for f in failures) and not retries:
        cheaper = {"op": "lipsync_patch", "shot_key": inp.shot_key, "note": "a lip-sync patch instead of a re-render"}
    est = sum(float(by_key[k].estimate.get("usd", 0.0) or 0.0) for k in targets)
    rung = next_rung(
        LadderInput(
            ladder=list(tier.ladder),
            budgets=Budgets(
                tier.budgets.retries_per_node, tier.budgets.retries_per_version, float(tier.budgets.usd_per_version)
            ),
            usage=await _usage(svc, org_id, version_id),
            history=inp.history,
            est_usd=est,
            retryable=bool(rerun),
            not_retryable_reason=why_not,
            fallback=fallback,
            fallback_reason=fallback_reason,
            cheaper_fix=cheaper,
            cheaper_fix_reason=cheaper_reason,
        )
    )
    common: dict[str, Any] = {
        "take": best,
        "score": score,
        "failures": report_failures,
        "notes": rung.notes,
        "reason": rung.reason,
        "step": rung.step,
    }
    if rung.action == "needs_review":
        return GateDecision(action="needs_review", **common)
    return GateDecision(
        action="fallback" if rung.action == "fallback" else "retry",
        targets=sorted(targets),
        rerun=rerun,
        qc_retry=inp.attempt + 1,
        route_override=rung.adapter_id,
        **common,
    )


async def _doc_artifact(session: Any, org_id: UUID, sha: str) -> UUID | None:
    return (
        await session.execute(sa.select(Artifact.id).where(Artifact.org_id == org_id, Artifact.sha256 == sha).limit(1))
    ).scalar_one_or_none()


async def accept_outputs(svc: ExecServices, inp: AcceptInput) -> dict[str, Any]:
    """Records the accepted attempts of a QC loop; idempotent (manifest rows are insert-only)."""
    from ce_exec.runtime import with_fallback

    org_id, version_id, job_id = UUID(inp.org_id), UUID(inp.version_id), UUID(inp.job_id)
    data = await svc.version(org_id, version_id)
    graph = await svc.graph(inp.graph_sha)
    by_key = graph.by_key()
    catalog = data.catalog or svc.catalog
    outputs = {key: await svc.docs.output(sha) for key, sha in inp.accepted.items()}
    keep = set(inp.accepted.values()) | {r.sha256 for o in outputs.values() for r in o.refs.values()}
    rejected_shas: set[str] = set()
    for shas in inp.rejected.values():
        for sha in shas:
            if sha in keep:
                continue
            rejected_shas.add(sha)
            with contextlib.suppress(Exception):  # an unreadable document: its own row is still marked
                rejected_shas |= {r.sha256 for r in (await svc.docs.output(sha)).refs.values()} - keep
    takes: dict[tuple[str, int], list[UUID]] = {}
    async with svc.db.transaction() as session:
        for key in [n.key for n in graph.nodes if n.key in inp.accepted]:
            node = by_key[key]
            if key in inp.routes:
                node = with_fallback(node, inp.routes[key], catalog)
            output = outputs[key]
            doc_id = await _doc_artifact(session, org_id, inp.accepted[key])
            media = [a for a in [await _doc_artifact(session, org_id, r.sha256) for r in output.refs.values()] if a]
            retry = inp.retries.get(key, 0)
            seed = attempt_seed(node.seed_base, retry) if retry and node.seed_base is not None else node.seed_base
            if doc_id is None:
                continue
            rows = [r.row() for r in records_for(node, artifact_id=str(doc_id), effective_seed=seed)]
            for row in rows:
                if row["artifact_id"]:
                    row["artifact_id"] = UUID(row["artifact_id"])
            await rec.insert_manifest_rows(session, org_id, version_id, rows)
            await rec.upsert_node(  # the node row names the accepted attempt (seed, route, artifacts)
                session,
                org_id,
                job_id=job_id,
                version_id=version_id,
                node_key=key,
                node_kind=node.kind,
                effective_seed=seed,
                route=node.route.model_dump(mode="json") if node.route else None,
                artifact_ids=list(dict.fromkeys([doc_id, *media])),
            )
            await rec.add_artifact_refs(
                session, org_id, [doc_id, *media], ref_type="build_manifest", ref_id=str(version_id)
            )
            await session.execute(
                sa.update(Artifact)
                .where(Artifact.org_id == org_id, Artifact.id.in_([doc_id, *media]))
                .values(qc_state="accepted")
            )
            if node.kind in GENERATORS and node.shot_key and node.take and "video" in output.refs:
                video = await _doc_artifact(session, org_id, output.refs["video"].sha256)
                if video is not None:
                    takes.setdefault((node.shot_key, node.take), []).append(video)
            if node.kind == "qc.shot" and node.shot_key and node.take:
                await _refresh_take_report(session, svc, org_id, version_id, node, output)
        for (shot_key, take_index), videos in takes.items():
            await rec.upsert_take(
                session, org_id, version_id=version_id, shot_key=shot_key, take_index=take_index, artifact_ids=videos
            )
        if rejected_shas:
            await session.execute(
                sa.update(Artifact)
                .where(Artifact.org_id == org_id, Artifact.sha256.in_(sorted(rejected_shas)))
                .values(qc_state="qc_rejected")
            )
        if inp.shot_key and inp.report is not None and inp.verdict is not None:
            await _shot_report(session, svc, org_id, version_id, job_id, inp)
    return {"accepted": len(inp.accepted), "rejected": len(rejected_shas)}


async def _refresh_take_report(
    session: Any, svc: ExecServices, org_id: UUID, version_id: UUID, node: ExecutionNode, output: NodeOutput
) -> None:
    """The take's QC report describes the accepted attempt (it was written for the first one)."""
    take = await rec.upsert_take(
        session, org_id, version_id=version_id, shot_key=node.shot_key or "", take_index=node.take or 1
    )
    if take.qc_report_id is None:
        return
    report = await session.get(QCReport, take.qc_report_id)
    if report is None:
        return
    report.checks = take_checks(output)
    report.verdict = take_verdict(output)


def take_checks(output: NodeOutput) -> dict[str, Any]:
    data = output.data
    return {
        "metrics": data.get("metrics", {}),
        "verdicts": data.get("verdicts", {}),
        "vlm_judge": data.get("vlm_judge"),
        "metric_score": data.get("metric_score"),
        "score": data.get("score"),
        "passed": data.get("passed"),
        "behavior": data.get("behavior"),
        "gate": "shot",
    }


def take_verdict(output: NodeOutput) -> Literal["pass", "warn", "fail"]:
    failures, retries = _take_failures(output)
    if failures or retries:
        return "fail"
    decision = dict(dict(output.data.get("behavior") or {}).get("decision") or {})
    advisory = any(v.get("passed") is False for v in dict(output.data.get("verdicts", {})).values())
    judge = dict(output.data.get("vlm_judge") or {})
    if decision.get("action") in ("warn", "render_defect") or advisory or judge.get("defects"):
        return "warn"
    return "pass"


async def _shot_report(
    session: Any, svc: ExecServices, org_id: UUID, version_id: UUID, job_id: UUID, inp: AcceptInput
) -> None:
    qc_key = next((k for k in inp.accepted if k.startswith("qc.shot:")), None)
    if qc_key is None:
        return
    node_id = (
        await session.execute(
            sa.select(NodeRow.id).where(NodeRow.org_id == org_id, NodeRow.job_id == job_id, NodeRow.node_key == qc_key)
        )
    ).scalar_one_or_none()
    if node_id is None:
        return
    await session.execute(
        sa.delete(QCReport).where(
            QCReport.org_id == org_id,
            QCReport.version_id == version_id,
            QCReport.target_type == "shot",
            QCReport.target_id == node_id,
        )
    )
    tier_digest = svc.bundle.digests.get(
        f"qc/{(await svc.version(org_id, version_id)).spec.meta.quality_tier}.yaml", ""
    )
    session.add(
        QCReport(
            org_id=org_id,
            version_id=version_id,
            target_type="shot",
            target_id=node_id,
            checks={"shot_key": inp.shot_key, **(inp.report or {})},
            verdict=inp.verdict,
            thresholds_digest=tier_digest or "none",
        )
    )
    status = {"pass": "passed", "warn": "passed", "fail": "needs_review"}[inp.verdict or "pass"]
    await session.execute(
        sa.update(NodeRow)
        .where(
            NodeRow.org_id == org_id,
            NodeRow.job_id == job_id,
            NodeRow.shot_key == inp.shot_key,
            NodeRow.node_kind == "qc.shot",
        )
        .values(qc_status=status)
    )
