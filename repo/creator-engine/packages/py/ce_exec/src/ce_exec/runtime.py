"""Build execution bookkeeping (§12): planning a version, the cache check, running or dispatching a
node, recording its output (artifacts, cache entry, BuildManifest rows, takes, renders) and
closing the build. Temporal activities call these with ids only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import shutil
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_behavior.approximations import StaleApproximation, reproposal_ops, stale_approximations
from ce_build import (
    BuildOptions,
    ParentBuild,
    attempt_seed,
    build_graph,
    cache_key,
    manifest_from_planned,
    planned_routes,
    records_for,
)
from ce_contracts.capabilities import capability as capability_spec
from ce_core.build import ExecutionGraph, ExecutionNode, RouteDecision
from ce_core.canonical import content_digest
from ce_core.enums import VersionFlag, VersionState
from ce_core.spec.paths import SpecPath, SpecPathError
from ce_core.spec.videospec import OutputPreset
from ce_db import execution as rec
from ce_db.calibration import load_proxy_calibrations
from ce_db.models.assets import Artifact, GenerationJob
from ce_db.models.assets import ExecutionNode as NodeRow
from ce_db.models.behavior import QCReport
from ce_db.models.platform import CostLedger
from ce_db.models.videos import EditProposal, VideoVersion
from ce_memory.store import write_usage_events
from ce_obs import get_logger
from ce_obs.events import EventType
from ce_obs.metrics import NODE_SECONDS, NODES, RENDER_SECONDS
from ce_obs.tracing import get_tracer
from ce_router import RouterCatalog, fallback_route
from ce_storage import content_key
from ce_storage.content import StorageRunContext
from pydantic import BaseModel, Field

from ce_exec import behavior_records
from ce_exec.context import ExecServices, VersionData
from ce_exec.executors import run_local
from ce_exec.noderun import NodeRun
from ce_exec.outputs import DOC_MIME, NodeOutput, output_kind
from ce_exec.parents import evaluator_for, load_parent
from ce_exec.requests import build_request
from ce_exec.results import output_from_result

__all__ = [
    "BeginResult",
    "FinishResult",
    "LocalInput",
    "NodeInfo",
    "NodeRef",
    "PlanResult",
    "begin_node",
    "complete_build",
    "dispatch_payload",
    "fail_node",
    "finalize_model",
    "plan_version",
    "run_node_local",
]

_log = get_logger("ce.exec")
Queue = Literal["orchestrator", "render", "gpu"]


class NodeInfo(BaseModel):
    key: str
    kind: str
    queue: Queue
    deps: list[str]
    scene_key: str | None = None
    group: str = "scene"
    shot_key: str | None = Field(default=None, description="the shot of a scene node (QC gate grouping, §26)")


class PlanResult(BaseModel):
    graph_sha: str
    qc_retries_per_node: int = Field(default=1, ge=0, description="the quality tier's QC retry budget per node (§26)")
    nodes: list[NodeInfo]
    pre: list[str] = Field(description="video-level nodes that run before the scenes")
    scenes: dict[str, list[str]]
    post: list[str]
    manifest: bool = True


class NodeRef(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    graph_sha: str
    node_key: str
    upstream: dict[str, str] = Field(default_factory=dict)
    manifest: bool = True
    qc_retry: int = Field(
        default=0,
        ge=0,
        description="QC retry number (exact-script loop, QC gate): >0 bypasses the cache and uses an attempt seed",
    )
    route_override: str | None = Field(
        default=None, description="QC ladder fallback: run the node on this fallback adapter of its route (§26)"
    )
    defer_manifest: bool = Field(
        default=False,
        description="a node inside a QC loop: its BuildManifest rows are written when the gate accepts an attempt",
    )


class BeginResult(BaseModel):
    status: Literal["cached", "local", "dispatch"]
    cache_key: str
    node_id: str
    sha: str | None = None
    attempt_id: str | None = None
    request_sha: str | None = None
    seed: int | None = Field(default=None, description="the seed this attempt runs with (attempt seed on QC retries)")
    passed: bool | None = Field(default=None, description="verification verdict of a cached check node")


class FinishResult(BaseModel):
    sha: str
    status: str = "succeeded"
    passed: bool | None = Field(default=None, description="verification verdict (asr.verify)")


VERDICT_KINDS = frozenset({"asr.verify", "behavior.coverage", "qc.render"})


def _verdict(node: ExecutionNode, output: NodeOutput) -> bool | None:
    if node.kind not in VERDICT_KINDS:
        return None
    value = output.data.get("passed")
    return None if value is None else bool(value)


class LocalInput(BaseModel):
    """`run_local_node` input (orchestrator and render worker register the same activity)."""

    ref: NodeRef
    begin: BeginResult


# ---------------------------------------------------------------------- planning


def _queue(node: ExecutionNode, svc: ExecServices) -> Queue:
    if node.executor == "render":
        return "render"
    if node.executor == "model" and node.route is not None:
        manifest = svc.catalog.manifests.get(node.route.adapter_id)
        if manifest is not None and manifest.runtime.family == "cpu_inproc":
            return "orchestrator"
        return "gpu"
    return "orchestrator"


def _partition(
    graph: ExecutionGraph, wanted: set[str] | None = None
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    by_key = graph.by_key()
    keys = [n.key for n in graph.nodes if wanted is None or n.key in wanted]
    scene_nodes: dict[str, list[str]] = {}
    for key in keys:
        node = by_key[key]
        if node.scene_key is not None:
            scene_nodes.setdefault(node.scene_key, []).append(key)
    in_scene = {k for v in scene_nodes.values() for k in v}
    pre: list[str] = []
    pre_set: set[str] = set()
    for key in keys:  # topological order
        node = by_key[key]
        if key in in_scene:
            continue
        if all(d in pre_set or (wanted is not None and d not in wanted) for d in node.deps) and not any(
            d in in_scene for d in node.deps
        ):
            dependents_in_scene = any(key in by_key[s].deps for s in in_scene)
            if dependents_in_scene or not node.deps:
                pre.append(key)
                pre_set.add(key)
    post = [k for k in keys if k not in in_scene and k not in pre_set]
    return pre, scene_nodes, post


def _ancestors(graph: ExecutionGraph, roots: set[str]) -> set[str]:
    by_key = graph.by_key()
    out: set[str] = set()
    stack = list(roots)
    while stack:
        key = stack.pop()
        if key in out:
            continue
        out.add(key)
        stack.extend(by_key[key].deps)
    return out


async def _parent(
    svc: ExecServices, data: VersionData, planned: dict[str, Any], options: BuildOptions
) -> tuple[ParentBuild | None, BuildOptions, Any]:
    """The parent build to pin routes to, the options, and the behavior evaluator (derived
    versions whose parent generated: unchanged behavior outputs keep downstream clean, §12.9)."""
    if data.parent_version_id is not None:
        loaded = await load_parent(svc, data.org_id, data.parent_version_id, options)
        if loaded is not None:
            pb, parent = loaded
            return pb, options, evaluator_for(svc, parent, data)
    if planned:
        empty = ExecutionGraph(spec_content_digest=data.spec.content_digest(), routing_profile="draft", nodes=[])
        replay = BuildOptions(
            routing_profile=options.routing_profile,
            provenance_mode=options.provenance_mode,
            sandbox=options.sandbox,
            replay=True,
            planned=options.planned,
            calibration_sha=options.calibration_sha,
        )
        return ParentBuild(empty, manifest_from_planned(planned)), replay, None
    return None, options, None


async def plan_version(
    svc: ExecServices,
    org_id: UUID,
    job_id: UUID,
    version_id: UUID,
    *,
    preset_ids: list[str] | None = None,
    previz: bool = False,
) -> PlanResult:
    """Builds the execution graph (pinned to the parent's routes), stores it, creates the node rows
    and moves the version to `generating`. With `preset_ids` (RenderWorkflow) only the nodes those
    renders need are planned, and nothing is written to the frozen BuildManifest. With `previz`
    (PrevizWorkflow, §13) only TTS, verification, alignment, the world plates and one keyframe per
    talking shot (and what they need) are planned, outside the BuildManifest; their content-only
    cache keys make generation after approval reuse them, and the version moves to `previz_running`."""
    data = await svc.version(org_id, version_id, fresh=True)
    spec = data.spec
    if preset_ids:
        primary = spec.render.outputs[:1]
        extra = [
            OutputPreset(preset_id=p, aspect=svc.bundle.render_preset(p).aspect)  # type: ignore[union-attr]
            for p in preset_ids
            if p not in {o.preset_id for o in primary}
        ]
        spec = spec.model_copy(update={"render": spec.render.model_copy(update={"outputs": [*primary, *extra]})})
    async with svc.db.session() as session:
        version = await session.get_one(VideoVersion, version_id)
        planned = dict(version.planned_routes or {})
        calibrations = await load_proxy_calibrations(session)
    # Measured proxy calibrations (§16.2) as one document: judgement nodes key on its sha256.
    calibration_sha = await svc.docs.put_json({"proxy_calibrations": calibrations}) if calibrations else None
    options = BuildOptions(
        provenance_mode=svc.provenance_mode,
        planned={k: RouteDecision.model_validate(v) for k, v in planned.items()},
        calibration_sha=calibration_sha,
    )
    options = replace(options, **(await _build_directives(svc, org_id, job_id)))
    parent, options, evaluate = await _parent(svc, data, planned, options)
    catalog = data.catalog or svc.catalog  # the registry snapshot pinned to this version (I5)
    graph = build_graph(spec, data.refs, svc.bundle, catalog, parent=parent, options=options, evaluate=evaluate)
    partial = bool(preset_ids) or previz
    stale = [] if partial else stale_approximations(spec, {n.key: n.route for n in graph.nodes if n.route})
    graph_sha = await svc.docs.put_json(graph.model_dump(mode="json"))
    wanted: set[str] | None = None
    if preset_ids:
        roots = {n.key for n in graph.nodes if n.key.split(":", 1)[-1] in preset_ids and n.group == "video"}
        wanted = _ancestors(graph, roots)
    elif previz:
        wanted = _ancestors(graph, {n.key for n in graph.nodes if n.kind in PREVIZ_KINDS})
    pre, scenes, post = _partition(graph, wanted)
    nodes = [
        NodeInfo(
            key=n.key,
            kind=n.kind,
            queue=_queue(n, svc),
            deps=list(n.deps),
            scene_key=n.scene_key,
            group=n.group,
            shot_key=n.shot_key,
        )
        for n in graph.nodes
        if wanted is None or n.key in wanted
    ]
    async with svc.db.transaction() as session:
        for node in graph.nodes:
            if wanted is not None and node.key not in wanted:
                continue
            await rec.upsert_node(
                session,
                org_id,
                job_id=job_id,
                version_id=version_id,
                node_key=node.key,
                node_kind=node.kind,
                scene_key=node.scene_key,
                shot_key=node.shot_key,
                chunk_index=node.chunk,
                take_index=node.take,
                status="pending",
                route=node.route.model_dump(mode="json") if node.route else None,
            )
        estimate = sum(n.estimate.get("usd", 0.0) for n in graph.nodes if wanted is None or n.key in wanted)
        full_estimate = sum(n.estimate.get("usd", 0.0) for n in graph.nodes)
        job = await rec.set_job(
            session,
            org_id,
            job_id,
            status="running",
            progress=0.02,
            input={
                **(await _job_input(session, org_id, job_id)),
                "graph_sha": graph_sha,
                "total_nodes": len(nodes),
                "generation_estimate_usd": round(full_estimate, 6),
            },
            cost_estimate_usd=round(estimate, 6),
        )
        target = VersionState.PREVIZ_RUNNING if previz else VersionState.GENERATING
        if not preset_ids and VersionState(data.state) != target:
            await rec.set_version_state(session, org_id, version_id, target)
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job), project_id=data.project_id)
    if not preset_ids:
        await svc.publish(
            org_id,
            EventType.VERSION_UPDATED,
            {"version_id": str(version_id), "state": target.value},
            project_id=data.project_id,
        )
    if stale:
        await _propose_reproposal(svc, data, job_id, stale, graph)
    tier = svc.bundle.qc_tiers.get(str(spec.meta.quality_tier))
    return PlanResult(
        graph_sha=graph_sha,
        qc_retries_per_node=tier.budgets.retries_per_node if tier is not None else 1,
        nodes=nodes,
        pre=pre,
        scenes=scenes,
        post=post,
        manifest=not partial,
    )


async def _build_directives(svc: ExecServices, org_id: UUID, job_id: UUID) -> dict[str, Any]:
    """Build options an edit recorded on the generation job (§12.4 `reroute`, lip-sync patches)."""
    async with svc.db.session() as session:
        job_input = await _job_input(session, org_id, job_id)
    build = dict(job_input.get("build") or {})
    out: dict[str, Any] = {}
    if build.get("force_reroute"):
        out["force_reroute"] = frozenset(str(k) for k in build["force_reroute"])
    if build.get("lipsync_patch_shots"):
        out["lipsync_patch_shots"] = frozenset(str(k) for k in build["lipsync_patch_shots"])
    return out


async def _propose_reproposal(
    svc: ExecServices, data: VersionData, job_id: UUID, stale: list[StaleApproximation], graph: ExecutionGraph
) -> None:
    """Compiler approximations planned for another route (§15.7, I1): flag the version
    `approximations_stale` and record one proposal (typed operations, computed like any edit) that
    removes them, with the coverage delta (the planned approximation vs what the current engine
    declares for the item). Nothing in the spec changes until the user applies it."""
    from ce_exec.editing import build_proposal

    org_id, version_id = data.org_id, data.version_id
    async with svc.db.transaction() as session:
        version = await session.get_one(VideoVersion, version_id)
        if VersionFlag.APPROXIMATIONS_STALE.value not in (version.flags or []):
            version.flags = [*(version.flags or []), VersionFlag.APPROXIMATIONS_STALE.value]
        open_rows = (
            await session.execute(
                sa.select(sa.func.count()).where(
                    EditProposal.org_id == org_id,
                    EditProposal.version_id == version_id,
                    EditProposal.status.in_(("proposing", "proposed")),
                    EditProposal.selection["kind"].astext == "approximations_stale",
                )
            )
        ).scalar_one()
    if open_rows:
        return
    routes = {n.key: n.route for n in graph.nodes if n.route}
    delta = []
    for item in stale:
        route = routes.get(item.route_key)
        manifest = svc.catalog.manifests.get(route.adapter_id) if route else None
        delta.append(
            {
                "element": item.element_path,
                "approximates": item.approximates,
                "planned": {"route_digest": item.planned_route_digest, "method": "compiler_approximation"},
                "current": {
                    "route_digest": item.current_route_digest,
                    "adapter_id": route.adapter_id if route else None,
                    "declared": _declared_for(manifest, data.spec, item.approximates, svc),
                },
            }
        )
    ops = reproposal_ops(stale, routes)
    proposal, _, _ = await build_proposal(svc, data, ops, actor="system")
    impact = {**proposal.impact, "issues": [dataclasses.asdict(i) for i in proposal.issues], "planner": "system"}
    async with svc.db.transaction() as session:
        row = EditProposal(
            org_id=org_id,
            version_id=version_id,
            job_id=job_id,
            instruction="Compiler approximations were planned for another engine route; remove or re-plan them.",
            selection={"kind": "approximations_stale", "elements": [i.element_path for i in stale]},
            ops=proposal.ops_json(),
            patch=list(proposal.patch.get("ops", [])),
            impact=impact,
            coverage_delta={**proposal.coverage_delta, "stale": delta},
            alternatives=proposal.alternatives,
            status="proposed" if proposal.status == "proposed" else "failed",
        )
        session.add(row)
        await session.flush()
        proposal_id = row.id
    await svc.publish(
        org_id,
        EventType.EDIT_PROPOSED,
        {"edit_proposal_id": str(proposal_id), "version_id": str(version_id), "status": row.status},
        project_id=data.project_id,
    )


def _declared_for(manifest: Any, spec: Any, item_ref: str, svc: ExecServices) -> dict[str, Any] | None:
    """What the routed engine declares for the dimension of the approximated acting item."""
    if manifest is None or manifest.behavior_matrix is None:
        return None
    dimension = _item_dimension(spec, item_ref, svc)
    if dimension is None:
        return None
    control = manifest.behavior_matrix.control(dimension)
    return {"dimension": dimension, "control": control.control, "temporal_precision": control.temporal_precision}


def _item_dimension(spec: Any, item_ref: str, svc: ExecServices) -> str | None:
    try:
        segs = SpecPath.parse(item_ref).segments
    except SpecPathError:
        return None
    if len(segs) >= 3 and segs[0].field == "scenes" and segs[1].field == "acting":
        scene = next((sc for sc in spec.scenes if sc.key == segs[0].selector), None)
        if scene is None or scene.acting is None:
            return None
        if segs[2].field == "events":
            event = next((e for e in scene.acting.events if e.key == segs[2].selector), None)
            definition = svc.bundle.vocab.events.get(event.type) if event is not None else None
            return definition.dimension if definition is not None else None
        if segs[2].field == "states":
            return "emotion_visual" if segs[-1].field == "emotion" else segs[-1].field
    return None


async def _job_input(session: Any, org_id: UUID, job_id: UUID) -> dict[str, Any]:
    job = (
        await session.execute(
            sa.select(GenerationJob.input).where(GenerationJob.org_id == org_id, GenerationJob.id == job_id)
        )
    ).scalar_one()
    return dict(job or {})


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


# ---------------------------------------------------------------------- nodes


def with_fallback(node: ExecutionNode, adapter_id: str, catalog: RouterCatalog) -> ExecutionNode:
    """The node on a fallback adapter of its route (QC ladder, §26). The route's identity is part of
    the cache key, so the fallback's output is cached under its own key, never the planned one."""
    if node.route is None:
        raise ValueError(f"{node.key} has no route to fall back from")
    return node.model_copy(update={"route": fallback_route(node.route, adapter_id, catalog)})


async def _context(
    svc: ExecServices, ref: NodeRef
) -> tuple[VersionData, ExecutionGraph, ExecutionNode, dict[str, NodeOutput]]:
    data = await svc.version(UUID(ref.org_id), UUID(ref.version_id))
    graph = await svc.graph(ref.graph_sha)
    node = graph.node(ref.node_key)
    if ref.route_override:
        node = with_fallback(node, ref.route_override, data.catalog or svc.catalog)
    missing = [d for d in node.deps if d not in ref.upstream]
    if missing:
        raise ValueError(f"{node.key}: upstream outputs missing for {missing}")
    upstream = {d: await svc.docs.output(ref.upstream[d]) for d in node.deps}
    return data, graph, node, upstream


async def _blobs_present(svc: ExecServices, sha: str) -> bool:
    """A cache hit is usable only while its document and every blob it references are stored
    (an empty or restored bucket, or a missed GC reference, must cause a rebuild, never a failure)."""
    try:
        output = await svc.docs.output(sha)
    except Exception:  # missing or unreadable document
        return False
    for ref in output.refs.values():
        if not await svc.content.has(ref.sha256):
            return False
    return True


def _cbs_digest(node: ExecutionNode, upstream: dict[str, NodeOutput]) -> str | None:
    if not node.reads_requests:
        return None
    digests = {
        k: str(v.data.get("cbs_content_digest", "")) for k, v in upstream.items() if k.startswith("behavior.resolve:")
    }
    if not digests:
        return content_digest({})
    return next(iter(digests.values())) if len(digests) == 1 else content_digest(digests)


def key_upstream(node: ExecutionNode, shas: dict[str, str], upstream: dict[str, NodeOutput]) -> dict[str, str]:
    """Upstream hashes for a cache key (§12.2): generation nodes depend on the **generation digest**
    of the behavior outputs they consume (the compiled output they read), so an edit that leaves it
    unchanged keeps them cached; nodes that read requests depend on the whole documents."""
    if node.reads_requests:
        return dict(shas)
    out: dict[str, str] = {}
    for dep, sha in shas.items():
        output = upstream.get(dep)
        digest = output.data.get("generation_digest") if output is not None else None
        out[dep] = f"generation:{digest}" if isinstance(digest, str) else sha
    return out


async def _reused(svc: ExecServices, org_id: UUID, sha: str, seed: int | None) -> rec.CacheHit | None:
    """A node pinned to an earlier output by content (`params.reuse`, the voice lock) is a cache
    hit on that output: nothing runs, and the build reports it as `cached` (§12.7)."""
    async with svc.db.session() as session:
        artifact_id = (
            (
                await session.execute(
                    sa.select(Artifact.id).where(Artifact.org_id == org_id, Artifact.sha256 == sha).limit(1)
                )
            )
            .scalars()
            .first()
        )
    if artifact_id is None or not await _blobs_present(svc, sha):
        return None  # the executor re-emits it (`_reuse_output`)
    return rec.CacheHit(artifact_id, sha, seed)


async def begin_node(svc: ExecServices, ref: NodeRef) -> BeginResult:
    data, graph, node, upstream = await _context(svc, ref)
    org_id, job_id, version_id = UUID(ref.org_id), UUID(ref.job_id), UUID(ref.version_id)
    key = cache_key(node, key_upstream(node, ref.upstream, upstream), _cbs_digest(node, upstream))
    seed = (
        attempt_seed(node.seed_base or 0, ref.qc_retry)
        if ref.qc_retry and node.seed_base is not None
        else node.seed_base
    )
    async with svc.db.session() as session:
        # A QC retry must produce a new attempt: the cache entry it will replace is not a hit.
        hit = None if ref.qc_retry else await rec.cache_lookup(session, org_id, key)
    if hit is not None and not await _blobs_present(svc, hit.sha256):
        _log.warning("cache entry points at missing blobs; rebuilding", node=node.key, sha=hit.sha256)
        async with svc.db.transaction() as session:
            await rec.cache_drop(session, org_id, key)
        hit = None
    if hit is None and node.params.get("reuse") is not None:
        hit = await _reused(svc, org_id, str(node.params["reuse"]), node.seed_base)
    async with svc.db.transaction() as session:
        row = await rec.upsert_node(
            session,
            org_id,
            job_id=job_id,
            version_id=version_id,
            node_key=node.key,
            node_kind=node.kind,
            cache_key=key,
            status="cached" if hit else "queued",
            effective_seed=hit.effective_seed if hit else seed,
            route=node.route.model_dump(mode="json") if node.route else None,
        )
        node_id = row.id
    if hit is not None:
        output = await svc.docs.output(hit.sha256)
        doc_id, media_ids = await _register_refs(svc, org_id, output, hit.sha256, node_id, None)
        await _record(
            svc, ref, node, data, output, node_id, doc_id, media_ids, effective_seed=hit.effective_seed, status="cached"
        )
        NODES.labels(node.kind, "cached").inc()
        return BeginResult(
            status="cached", cache_key=key, node_id=str(node_id), sha=hit.sha256, passed=_verdict(node, output)
        )
    if _queue(node, svc) != "gpu":
        return BeginResult(status="local", cache_key=key, node_id=str(node_id), seed=seed)
    run = NodeRun(
        svc,
        data,
        graph,
        node,
        upstream,
        ref.upstream,
        job_id,
        StorageRunContext(svc.content, svc.scratch("prep"), seed=seed or 0),
    )
    try:
        prepared = await build_request(run)
    finally:
        shutil.rmtree(run.ctx.scratch_dir, ignore_errors=True)
    route = node.route
    assert route is not None
    manifest = svc.catalog.manifests[route.adapter_id]
    doc = {
        "capability": node.capability,
        "adapter_id": route.adapter_id,
        "model_key": route.model_id,
        "seed": seed or 0,
        "request": prepared.request.model_dump(mode="json"),
        "inputs": [r.sha256 for r in prepared.inputs],
        "labels": {**prepared.request.labels, "node": node.key}
        if hasattr(prepared.request, "labels")
        else {"node": node.key},
        "extra": prepared.extra,
        "est_seconds": float(node.estimate.get("seconds", 1.0)),
        "vram_gb": float(manifest.runtime.min_vram_gb or 0.0),
    }
    request_sha = await svc.docs.put_json(doc)
    async with svc.db.transaction() as session:
        attempt = await rec.new_attempt(
            session,
            org_id,
            node_id,
            reason="fallback" if ref.route_override else ("qc_retry" if ref.qc_retry else "initial"),
            seed=seed,
            route=route.identity(),
            started_at=svc.clock(),
        )
        await rec.upsert_node(
            session,
            org_id,
            job_id=job_id,
            version_id=version_id,
            node_key=node.key,
            node_kind=node.kind,
            status="queued",
            attempts=attempt.attempt_no,
        )
    await _node_event(svc, ref, data, node, "queued")
    return BeginResult(
        status="dispatch",
        cache_key=key,
        node_id=str(node_id),
        attempt_id=str(attempt.id),
        request_sha=request_sha,
        seed=seed,
    )


async def dispatch_payload(svc: ExecServices, ref: NodeRef, begin: BeginResult) -> dict[str, Any]:
    """The `gpu_tasks` row fields for a prepared model node (the dispatch activity adds the token)."""
    import json

    doc = json.loads(await svc.docs.raw(begin.request_sha or ""))
    # work units = the planning estimate ÷ the manifest's seconds per unit; with enough completed
    # attempts the fleet's backlog uses the measured speed instead (the request document, its
    # digest and the plan stay as planned)
    manifest = svc.catalog.manifests.get(doc["adapter_id"])
    per_unit = float(manifest.pricing.get("seconds_per_unit", 1.0) or 1.0) if manifest else 1.0
    work_units = round(float(doc["est_seconds"]) / per_unit, 4)
    measured = await svc.seconds_per_unit(doc["adapter_id"])
    est_seconds = round(measured * work_units, 3) if measured else doc["est_seconds"]
    return {
        "capability": doc["capability"],
        "model_key": doc["model_key"],
        "vram_gb": doc["vram_gb"],
        "est_seconds": est_seconds,
        "priority": svc.bundle.app.build.gpu_task_priority,
        "constraints": {"adapter_id": doc["adapter_id"]},
        "payload": {
            "adapter_id": doc["adapter_id"],
            "capability": doc["capability"],
            "seed": doc["seed"],
            "request": doc["request"],
            "inputs": doc["inputs"],
            "labels": doc["labels"],
            "node_key": ref.node_key,
            "version_id": ref.version_id,
            "job_id": ref.job_id,
            "infra_retries": 0,
            "work_units": work_units,
        },
    }


_tracer = get_tracer("ce.exec")


async def run_node_local(svc: ExecServices, ref: NodeRef, begin: BeginResult) -> FinishResult:
    data, graph, node, upstream = await _context(svc, ref)
    started = time.monotonic()
    seed = begin.seed if begin.seed is not None else node.seed_base
    ctx = StorageRunContext(svc.content, svc.scratch(node.kind.replace(".", "_")), seed=seed or 0)
    run = NodeRun(svc, data, graph, node, upstream, ref.upstream, UUID(ref.job_id), ctx, qc_retry=ref.qc_retry)
    await _set_status(svc, ref, begin, "running")
    span_attributes = {"ce.node.kind": node.kind, "ce.node.key": node.key, "ce.job_id": ref.job_id}
    with _tracer.start_as_current_span(f"node {node.kind}", attributes=span_attributes):
        try:
            output = await run_local(run)
        finally:
            ctx.cleanup()
    sha = await svc.docs.put_output(output)
    await _finish(svc, ref, begin, node, data, output, sha, cache=True)
    elapsed = time.monotonic() - started
    NODE_SECONDS.labels(node.kind).observe(elapsed)
    if node.kind.startswith("render."):
        RENDER_SECONDS.labels(node.kind).observe(elapsed)
    NODES.labels(node.kind, "executed").inc()
    return FinishResult(sha=sha, passed=_verdict(node, output))


async def finalize_model(svc: ExecServices, ref: NodeRef, begin: BeginResult, worker: dict[str, Any]) -> FinishResult:
    """Records a model node from the scheduler's completion (`result` + verified `outputs`)."""
    import json

    data, graph, node, upstream = await _context(svc, ref)
    doc = json.loads(await svc.docs.raw(begin.request_sha or ""))
    run = NodeRun(
        svc,
        data,
        graph,
        node,
        upstream,
        ref.upstream,
        UUID(ref.job_id),
        StorageRunContext(svc.content, svc.scratch("fin")),
    )
    try:
        result = capability_spec(str(doc["capability"])).result.model_validate(worker["result"])
        output = output_from_result(run, str(doc["capability"]), result, dict(doc.get("extra", {})))
    finally:
        run.ctx.cleanup()
    media = {o["sha256"]: o for o in worker.get("outputs", [])}
    sha = await svc.docs.put_output(output)
    await _finish(svc, ref, begin, node, data, output, sha, cache=True, media=media)
    NODES.labels(node.kind, "executed").inc()
    return FinishResult(sha=sha, passed=_verdict(node, output))


async def fail_node(
    svc: ExecServices, ref: NodeRef, node_id: str | None, error: str, *, status: str = "failed"
) -> None:
    data = await svc.version(UUID(ref.org_id), UUID(ref.version_id))
    graph = await svc.graph(ref.graph_sha)
    node = graph.node(ref.node_key)
    async with svc.db.transaction() as session:
        await rec.upsert_node(
            session,
            UUID(ref.org_id),
            job_id=UUID(ref.job_id),
            version_id=UUID(ref.version_id),
            node_key=node.key,
            node_kind=node.kind,
            status=status,
        )
    NODES.labels(node.kind, status).inc()
    _log.warning("node failed", node=node.key, status=status, error=error[:500])
    await _node_event(svc, ref, data, node, status, error=error[:500])


async def _set_status(svc: ExecServices, ref: NodeRef, begin: BeginResult, status: str) -> None:
    async with svc.db.transaction() as session:
        await session.execute(
            sa.update(NodeRow)
            .where(NodeRow.org_id == UUID(ref.org_id), NodeRow.id == UUID(begin.node_id))
            .values(status=status)
        )


async def _register_refs(
    svc: ExecServices,
    org_id: UUID,
    output: NodeOutput,
    sha: str,
    node_id: UUID,
    media: dict[str, dict[str, Any]] | None,
) -> tuple[UUID, dict[str, UUID]]:
    ids: dict[str, UUID] = {}
    async with svc.db.transaction() as session:
        for role, ref in output.refs.items():
            info = (media or {}).get(ref.sha256, {})
            ids[role] = await rec.register_artifact(
                session,
                org_id,
                sha256=ref.sha256,
                kind=ref.kind if ref.kind in _KINDS else "other",
                mime=ref.mime,
                size=ref.bytes or int(info.get("bytes", 0)),
                storage_key=str(info.get("storage_key") or content_key(ref.sha256)),
                media={"role": role, **ref.meta},
                produced_by_node_id=node_id,
            )
        raw = output.encode()
        doc_id = await rec.register_artifact(
            session,
            org_id,
            sha256=sha,
            kind=output_kind(output.node_kind),
            mime=DOC_MIME,
            size=len(raw),
            storage_key=content_key(sha),
            media={"node_kind": output.node_kind},
            produced_by_node_id=node_id,
        )
    return doc_id, ids


_KINDS = frozenset(
    {
        "video",
        "audio",
        "image",
        "captions",
        "alignment",
        "cbs",
        "compiled_behavior",
        "keyframe_state",
        "observed_behavior",
        "coverage_report",
        "plan_report",
        "screen_analysis",
        "world_fingerprints",
        "voice_conditioning",
        "logs",
        "other",
    }
)


async def _finish(
    svc: ExecServices,
    ref: NodeRef,
    begin: BeginResult,
    node: ExecutionNode,
    data: VersionData,
    output: NodeOutput,
    sha: str,
    *,
    cache: bool,
    media: dict[str, dict[str, Any]] | None = None,
) -> None:
    org_id = UUID(ref.org_id)
    node_id = UUID(begin.node_id)
    doc_id, media_ids = await _register_refs(svc, org_id, output, sha, node_id, media)
    seed = begin.seed if begin.seed is not None else node.seed_base
    if cache:  # a QC retry's accepted artifact replaces the entry under the same key (§12.5, D28)
        async with svc.db.transaction() as session:
            await rec.cache_store(session, org_id, begin.cache_key, doc_id, seed)
    await _record(svc, ref, node, data, output, node_id, doc_id, media_ids, effective_seed=seed, status="succeeded")


async def _record(
    svc: ExecServices,
    ref: NodeRef,
    node: ExecutionNode,
    data: VersionData,
    output: NodeOutput,
    node_id: UUID,
    doc_id: UUID,
    media: dict[str, UUID],
    *,
    effective_seed: int | None,
    status: str,
) -> None:
    """Node row, BuildManifest rows, artifact references, takes and renders (cached or executed)."""
    org_id, version_id = UUID(ref.org_id), UUID(ref.version_id)
    media_ids = list(dict.fromkeys(media.values()))
    async with svc.db.transaction() as session:
        await rec.upsert_node(
            session,
            org_id,
            job_id=UUID(ref.job_id),
            version_id=version_id,
            node_key=node.key,
            node_kind=node.kind,
            status=status,
            artifact_ids=[doc_id, *media_ids],
            effective_seed=effective_seed,
        )
        if ref.manifest and not ref.defer_manifest:  # deferred: `ce_exec.qcgate.accept_outputs` writes them
            rows = [r.row() for r in records_for(node, artifact_id=str(doc_id), effective_seed=effective_seed)]
            for row in rows:
                if row["artifact_id"]:
                    row["artifact_id"] = UUID(row["artifact_id"])
            await rec.insert_manifest_rows(session, org_id, version_id, rows)
            await rec.add_artifact_refs(
                session, org_id, [doc_id, *media_ids], ref_type="build_manifest", ref_id=str(version_id)
            )
        await _bookkeep(session, svc, ref, node, data, output, doc_id, media)
    await _node_event(svc, ref, data, node, status)


async def _bookkeep(
    session: Any,
    svc: ExecServices,
    ref: NodeRef,
    node: ExecutionNode,
    data: VersionData,
    output: NodeOutput,
    doc_id: UUID,
    media: dict[str, UUID],
) -> None:
    org_id, version_id = UUID(ref.org_id), UUID(ref.version_id)
    if node.kind in ("avatar.render", "video.broll") and node.shot_key and node.take:
        take = await rec.upsert_take(
            session, org_id, version_id=version_id, shot_key=node.shot_key, take_index=node.take
        )
        video = media.get("video")
        if video is not None and video not in (take.artifact_ids or []):
            take.artifact_ids = [*(take.artifact_ids or []), video]
        if node.chunk in (None, 1):
            take.effective_seed = node.seed_base
        await rec.add_artifact_refs(session, org_id, [video] if video else [], ref_type="take", ref_id=str(take.id))
    elif node.kind == "behavior.observe" and node.shot_key and node.take:
        await rec.upsert_take(
            session,
            org_id,
            version_id=version_id,
            shot_key=node.shot_key,
            take_index=node.take,
            observed_behavior_artifact_id=doc_id,
            behavior_signature=dict(output.data.get("observed", {})).get("signature"),
        )
    elif node.kind == "qc.shot" and node.shot_key and node.take:
        graph = await svc.graph(ref.graph_sha)
        controls = await _requested_controls(svc, ref)
        await behavior_records.qc_shot_records(session, svc, org_id, data, graph, node, output, controls)
    elif node.kind in ("qc.world", "qc.continuity"):  # world continuity QC in the version's QC report (§19.6)
        await session.execute(
            sa.delete(QCReport).where(
                QCReport.org_id == org_id,
                QCReport.version_id == version_id,
                QCReport.target_type == "node",
                QCReport.target_id == doc_id,
            )
        )
        data_out = {k: v for k, v in output.data.items() if k != "background"}
        warned = bool(output.data.get("warnings")) or any(
            p.get("score") is not None and p["score"] < 0.5 for p in output.data.get("pairs", [])
        )
        session.add(
            QCReport(
                org_id=org_id,
                version_id=version_id,
                target_type="node",
                target_id=doc_id,
                checks={"kind": node.kind, "node_key": node.key, "shot_key": node.shot_key, **data_out},
                verdict="warn" if warned else "pass",
                thresholds_digest=svc.bundle.digests.get("qc/world.yaml", "") or "none",
            )
        )
    elif node.kind == "behavior.coverage":
        graph = await svc.graph(ref.graph_sha)
        controls = await _requested_controls(svc, ref)
        adapters = await behavior_records.coverage_records(session, svc, org_id, data, graph, output, controls)
        await behavior_records.refresh_profiles(session, svc, adapters)
        await svc.publish(
            org_id,
            EventType.COVERAGE_UPDATED,
            {"version_id": str(version_id), "summary": dict(output.data.get("summary", {}))},
            project_id=data.project_id,
        )
    elif node.kind in ("post.expression", "post.camera") and node.shot_key and "selected_take" in output.data:
        selected = int(output.data["selected_take"])
        scores = {int(k): float(v) for k, v in dict(output.data.get("scores", {})).items()}
        ranked = sorted(scores, key=lambda t: (-scores[t], t))
        for take_index in sorted({selected, *scores}):
            await rec.upsert_take(
                session,
                org_id,
                version_id=version_id,
                shot_key=node.shot_key,
                take_index=take_index,
                selected=take_index == selected,
                rank=ranked.index(take_index) + 1 if take_index in ranked else None,
            )
    elif node.kind == "provenance.sign" and ref.node_key.split(":", 1)[1]:
        preset_id = ref.node_key.split(":", 1)[1]
        preset = svc.bundle.render_preset(preset_id)
        manifest_doc: dict[str, Any] | None = None
        if "manifest" in output.refs:
            import json

            with contextlib.suppress(Exception):
                manifest_doc = json.loads(await svc.content.read_bytes(output.refs["manifest"].sha256))
        render = await rec.upsert_render(
            session,
            org_id,
            version_id=version_id,
            preset_id=preset_id,
            is_proxy=False,
            aspect=preset.aspect if preset else str(data.spec.meta.primary_aspect),
            artifact_id=media.get("media"),
            c2pa_manifest=manifest_doc,
            watermark_payload_id=output.data.get("payload_id"),
            consent_ids=list(data.spec.provenance.consent_ids),
            provenance_mode=str(output.data.get("mode", svc.provenance_mode)),
            status="ready",
            cache_key=None,
        )
        await rec.add_artifact_refs(
            session, org_id, [m for m in media.values()], ref_type="render", ref_id=str(render.id)
        )
    elif node.kind in ("captions.build", "captions.translate"):
        # §27: the burned ASS plus SRT/VTT for platform upload; translations await review (Phase 12)
        translated = node.kind == "captions.translate"
        language = (
            ref.node_key.split(":", 1)[1] if translated else str(output.data.get("language") or data.spec.meta.language)
        )
        for fmt in ("ass", "srt", "vtt"):
            if media.get(fmt) is not None:
                await rec.upsert_caption(  # the build manifest keeps the file alive (§12.10)
                    session,
                    org_id,
                    version_id=version_id,
                    language=language,
                    format=fmt,
                    style_id=data.spec.captions.style_id,
                    artifact_id=media[fmt],
                    review_state="pending" if translated else "n/a",
                    route=(
                        {
                            "adapter_id": node.route.adapter_id,
                            "model_id": node.route.model_id,
                            "revision": node.route.revision,
                        }
                        if translated and node.route is not None
                        else None
                    ),
                )
    elif node.kind == "render.proxy":
        primary = data.spec.render.outputs[0] if data.spec.render.outputs else None
        render = await rec.upsert_render(
            session,
            org_id,
            version_id=version_id,
            preset_id=primary.preset_id if primary else "proxy",
            is_proxy=True,
            aspect=primary.aspect if primary else str(data.spec.meta.primary_aspect),
            artifact_id=media.get("video"),
            provenance_mode=svc.provenance_mode,
            status="ready",
        )
        await rec.add_artifact_refs(session, org_id, list(media.values()), ref_type="render", ref_id=str(render.id))


async def _requested_controls(svc: ExecServices, ref: NodeRef) -> dict[tuple[str, str], dict[str, Any]]:
    """The requested controls of the CBS documents a node read (for observation rows)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, sha in ref.upstream.items():
        if key.startswith("behavior.resolve:"):
            doc = await svc.docs.output(sha)
            for control in dict(doc.data.get("content", {})).get("requested_controls", []):
                out[(control["item_ref"], control["dimension"])] = control
    return out


async def _node_event(
    svc: ExecServices, ref: NodeRef, data: VersionData, node: ExecutionNode, status: str, *, error: str | None = None
) -> None:
    payload = {
        "job_id": ref.job_id,
        "version_id": ref.version_id,
        "node_key": node.key,
        "kind": node.kind,
        "status": status,
        **({"error": error} if error else {}),
    }
    await svc.publish(data.org_id, EventType.NODE_UPDATED, payload, project_id=data.project_id)
    if status in ("succeeded", "cached", "failed", "skipped"):
        async with svc.db.transaction() as session:
            done = (
                await session.execute(
                    sa.select(sa.func.count()).where(
                        NodeRow.org_id == data.org_id,
                        NodeRow.job_id == UUID(ref.job_id),
                        NodeRow.status.in_(("succeeded", "cached", "failed", "skipped")),
                    )
                )
            ).scalar_one()
            job_input = await _job_input(session, data.org_id, UUID(ref.job_id))
            total = max(1, int(job_input.get("total_nodes", 1)))
            job = await rec.set_job(
                session, data.org_id, UUID(ref.job_id), progress=round(min(0.99, 0.02 + 0.97 * done / total), 4)
            )
        await svc.publish(data.org_id, EventType.JOB_UPDATED, _job_event(job), project_id=data.project_id)
        if node.kind == "provenance.sign" and status in ("succeeded", "cached"):
            await svc.publish(
                data.org_id,
                EventType.RENDER_READY,
                {"version_id": ref.version_id, "preset_id": node.key.split(":", 1)[1]},
                project_id=data.project_id,
            )


# ---------------------------------------------------------------------- closing


async def complete_build(
    svc: ExecServices,
    org_id: UUID,
    job_id: UUID,
    version_id: UUID,
    statuses: dict[str, str],
    *,
    cancelled: bool = False,
    render_only: bool = False,
) -> dict[str, Any]:
    """Final job and version state (§12.8): `ready` when every node completed, `partial` when some
    failed but a render exists, `failed` otherwise, `cancelled` on cancellation."""
    data = await svc.version(org_id, version_id)
    failed = sorted(k for k, s in statuses.items() if s in ("failed", "skipped"))
    review = sorted(k for k, s in statuses.items() if s == "needs_review")
    rendered = any(k.startswith("provenance.sign:") and s in ("succeeded", "cached") for k, s in statuses.items())
    if cancelled:
        state, job_status = VersionState.CANCELLED, "cancelled"
    elif not failed and review:  # flagged by a check (exact-script loop out of retries, §21)
        state, job_status = VersionState.NEEDS_REVIEW, "succeeded"
    elif not failed:
        state, job_status = VersionState.READY, "succeeded"
    elif rendered:
        state, job_status = VersionState.PARTIAL, "partial"
    else:
        state, job_status = VersionState.FAILED, "failed"
    hook_vector, hook_model = None, None
    signatures: dict[str, list[dict[str, Any]]] = {}
    if state == VersionState.READY and not render_only:
        hook_vector, hook_model = await _hook_embedding(svc, data)
        signatures = await _selected_signatures(svc, org_id, version_id, data)
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
        cost = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(CostLedger.amount_usd), 0)).where(
                    CostLedger.org_id == org_id, CostLedger.version_id == version_id
                )
            )
        ).scalar_one()
        job = await rec.set_job(
            session,
            org_id,
            job_id,
            status=job_status,
            progress=1.0,
            cost_actual_usd=cost,
            error={"failed_nodes": failed[:50]} if failed else None,
        )
        if not render_only:
            await rec.set_version_state(session, org_id, version_id, state, cost_actual_usd=cost)
            if state == VersionState.READY:  # the repetition guard reads these from Phase 4 on (§18.3)
                creators = {m.key: data.refs.creator(m.creator_version_id).creator_id for m in data.spec.cast}
                await write_usage_events(
                    session,
                    org_id,
                    video_id=data.video_id,
                    version_id=version_id,
                    spec=data.spec,
                    creators=creators,
                    behavior_signatures=signatures,
                    hook_embedding=hook_vector,
                    hook_embedding_model=hook_model,
                )
    await svc.publish(org_id, EventType.JOB_UPDATED, _job_event(job), project_id=data.project_id)
    if not render_only:
        await svc.publish(
            org_id,
            EventType.VERSION_UPDATED,
            {"version_id": str(version_id), "state": state.value},
            project_id=data.project_id,
        )
    return {"state": state.value, "job_status": job_status, "failed": failed, "needs_review": review}


async def _hook_embedding(svc: ExecServices, data: VersionData) -> tuple[list[float] | None, str | None]:
    """The selected hook embedded for the usage log (Phase 12): an index for the repetition guard;
    a failure never fails the build (the event is written without it)."""
    from ce_exec.embeddings import embed_texts

    brief = data.spec.brief
    if brief is None or not brief.selected_hook_key:
        return None, None
    hook = next((h.text for h in brief.hook_candidates if h.key == brief.selected_hook_key), None)
    if not hook:
        return None, None
    try:
        embedded = await embed_texts(svc, [hook], language=str(data.spec.meta.language), catalog=data.catalog)
    except Exception as exc:
        _log.warning("hook embedding failed; usage event written without it", error=str(exc)[:200])
        return None, None
    return (embedded.vectors[0], embedded.model) if embedded else (None, None)


async def _selected_signatures(
    svc: ExecServices, org_id: UUID, version_id: UUID, data: VersionData
) -> dict[str, list[dict[str, Any]]]:
    """character key → behavior signatures of the selected takes (§18.3), from `takes`."""
    from ce_db.models.videos import Take

    by_shot = {sh.key: sh.character_key for _, sh in data.spec.shots() if sh.character_key}
    async with svc.db.session() as session:
        rows = (
            await session.execute(
                sa.select(Take.shot_key, Take.take_key, Take.behavior_signature).where(
                    Take.org_id == org_id, Take.version_id == version_id, Take.selected.is_(True)
                )
            )
        ).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for shot_key, take_key, signature in rows:
        character = by_shot.get(str(shot_key))
        if character and signature:
            out.setdefault(str(character), []).append({"shot_key": shot_key, "take_key": take_key, **dict(signature)})
    return out


PREVIZ_KINDS = frozenset({"tts.segment", "asr.verify", "align.segment", "world.plate", "image.keyframe"})


def planned_for(graph: ExecutionGraph) -> dict[str, Any]:
    return planned_routes(graph)


def utc(dt: datetime) -> str:
    return dt.isoformat()
