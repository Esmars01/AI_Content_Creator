"""Studio jobs (Phase 10, ADR 0055): identity packs, wardrobe references, voice design and tests,
world plates and fingerprints, and the Creator Test.

Each studio workflow is the same deterministic loop (`ce_orchestrator.workflows.StudioLoop`):

    step = studio_stage(kind, stage, data)       # CPU activity: reads the database, decides
    outputs = [call_model(c) for c in step.calls] # model calls, in parallel
    data = step.data + {"outputs": outputs}; stage = step.next   # until step.done

and the decisions live here, in plain async functions per job kind (`STAGES`), testable without
Temporal. **Model calls** route through the router like build nodes; `cpu_inproc` adapters run in
the orchestrator, every other family is dispatched to the scheduler as a `gpu_tasks` row on an
execution node of the job (no video version; migration 0003), so studio work runs on the fleet,
is retried, costed and visible in the job like any build node.

Outputs that the user chooses from (face candidates, expansions, wardrobe references, plates) become
**assets** of the org (`kind: image`, tagged with the job), because the studio endpoints (`:choose`,
`:review`, plate choices) take asset ids.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_contracts.capabilities import capability as capability_spec
from ce_contracts.common import ArtifactRef
from ce_core.errors import CEError
from ce_db import execution as rec
from ce_db import queue
from ce_db.models.assets import Asset, JobAttempt
from ce_db.models.assets import ExecutionNode as NodeRow
from ce_obs import get_logger
from ce_storage import content_key
from ce_storage.content import StorageRunContext
from pydantic import BaseModel, Field

from ce_exec.context import ExecServices
from ce_exec.jobs import _set

__all__ = [
    "STAGES",
    "CallBegun",
    "ModelCall",
    "StudioCallInput",
    "StudioContext",
    "StudioDispatchInput",
    "StudioError",
    "StudioFailInput",
    "StudioRecordInput",
    "StudioStageInput",
    "StudioStep",
    "asset_from_ref",
    "begin_call",
    "dispatch_call",
    "fail_call",
    "fail_job",
    "record_call",
    "refs_in",
    "run_stage",
    "stage",
]

_log = get_logger("ce.exec.studio")


class StudioError(CEError):
    code = "studio_failed"
    status = 409
    title = "The studio job cannot continue"


class ModelCall(BaseModel):
    """One capability call of a studio job."""

    key: str = Field(description="unique within the job: the execution node key")
    capability: str
    request: dict[str, Any]
    seed: int = 0
    language: str | None = None
    height: int | None = None
    width: int | None = None
    adapter_id: str | None = Field(default=None, description="pinned adapter; else routed")
    prefer_mock: bool = Field(default=False, description="the input came from a mock engine (D84)")
    routing_profile: str = "draft"


class CallBegun(BaseModel):
    key: str
    capability: str
    adapter_id: str
    model_key: str
    done: bool = False
    output: dict[str, Any] = Field(default_factory=dict)
    node_id: str | None = None
    attempt_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    est_seconds: float = 1.0
    vram_gb: float = 0.0


class StudioContext(BaseModel):
    """The argument of every studio workflow (the API starts them with it)."""

    kind: str
    org_id: str
    job_id: str
    target_id: str
    args: dict[str, Any] = Field(default_factory=dict)
    user_id: str | None = None
    model_timeout_s: float = 1800.0
    cpu_timeout_s: float = 900.0


class StudioStageInput(BaseModel):
    ctx: StudioContext
    stage: str
    data: dict[str, Any] = Field(default_factory=dict)


class StudioCallInput(BaseModel):
    ctx: StudioContext
    call: ModelCall


class StudioDispatchInput(BaseModel):
    ctx: StudioContext
    begun: CallBegun


class StudioRecordInput(BaseModel):
    ctx: StudioContext
    begun: CallBegun
    worker: dict[str, Any]


class StudioFailInput(BaseModel):
    ctx: StudioContext
    error: str
    node_id: str | None = None


class StudioStep(BaseModel):
    done: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    calls: list[ModelCall] = Field(default_factory=list)
    next: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    progress: float | None = None
    build: dict[str, Any] | None = Field(
        default=None, description="{version_id, job_id}: run GenerateVersionWorkflow as a child, then `next`"
    )


Stage = Callable[[ExecServices, StudioContext, dict[str, Any]], Awaitable[StudioStep]]
STAGES: dict[tuple[str, str], Stage] = {}


def stage(kind: str, name: str) -> Callable[[Stage], Stage]:
    def register(fn: Stage) -> Stage:
        STAGES[(kind, name)] = fn
        return fn

    return register


async def run_stage(svc: ExecServices, ctx: StudioContext, name: str, data: dict[str, Any]) -> StudioStep:
    import ce_exec.bench_job
    import ce_exec.consistency_job
    import ce_exec.creator_test
    import ce_exec.critique_job
    import ce_exec.memory_job
    import ce_exec.packaging_job
    import ce_exec.research_job
    import ce_exec.studio_jobs  # noqa: F401

    fn = STAGES.get((ctx.kind, name))
    if fn is None:
        raise StudioError(f"no stage {name!r} for {ctx.kind}")
    if name == "start":
        await _set(svc, UUID(ctx.org_id), UUID(ctx.job_id), status="running", progress=0.05)
    step = await fn(svc, ctx, data)
    if step.done:
        await _set(
            svc,
            UUID(ctx.org_id),
            UUID(ctx.job_id),
            {"result": step.result},
            status="succeeded",
            progress=1.0,
            output=step.result,
        )
    elif step.progress is not None:
        await _set(svc, UUID(ctx.org_id), UUID(ctx.job_id), progress=step.progress)
    return step


async def fail_job(svc: ExecServices, ctx: StudioContext, error: str, code: str = "studio_failed") -> None:
    await _set(svc, UUID(ctx.org_id), UUID(ctx.job_id), status="failed", error={"code": code, "message": error[:2000]})


# ---------------------------------------------------------------------- refs and assets
def refs_in(value: Any) -> list[ArtifactRef]:
    """Every ArtifactRef in a request or result JSON (the worker's inputs, the outputs to register)."""
    found: list[ArtifactRef] = []
    if isinstance(value, dict):
        if isinstance(value.get("sha256"), str) and "kind" in value and len(value["sha256"]) == 64:
            found.append(ArtifactRef.model_validate(value))
        else:
            for item in value.values():
                found += refs_in(item)
    elif isinstance(value, list):
        for item in value:
            found += refs_in(item)
    return found


async def ref_of_asset(
    svc: ExecServices, session: Any, org_id: UUID, asset_id: UUID, *, kind: str, role: str
) -> ArtifactRef:
    """An org asset brought into the content store, as a request input."""
    asset = (
        await session.execute(sa.select(Asset).where(Asset.org_id == org_id, Asset.id == asset_id))
    ).scalar_one_or_none()
    if asset is None or asset.status != "ready":
        raise StudioError(f"asset {asset_id} is not a ready asset of this organization")
    await svc.content.adopt(svc.settings.s3_bucket_assets, asset.storage_key, asset.sha256, mime=asset.mime)
    return ArtifactRef(
        sha256=asset.sha256,
        kind=kind,
        mime=asset.mime,
        bytes=int(asset.bytes),
        role=role,
        meta={"asset_id": str(asset.id), **({"mock": True} if "mock" in (asset.tags or []) else {})},
    )


async def asset_from_ref(
    svc: ExecServices,
    session: Any,
    org_id: UUID,
    ref: dict[str, Any] | ArtifactRef,
    *,
    tags: list[str],
    rights: dict[str, Any],
    kind: str = "image",
    user_id: UUID | None = None,
) -> Asset:
    """A generated output as an asset of the org (deduplicated by sha256 within the org)."""
    artifact = ref if isinstance(ref, ArtifactRef) else ArtifactRef.model_validate(ref)
    existing = (
        (
            await session.execute(
                sa.select(Asset).where(Asset.org_id == org_id, Asset.sha256 == artifact.sha256, Asset.status == "ready")
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    key = f"generated/{org_id}/{artifact.sha256}"
    with tempfile.TemporaryDirectory(prefix="ce-studio-") as tmp:
        path = await svc.content.fetch(artifact.sha256, Path(tmp) / "blob")
        size = path.stat().st_size
        probe: dict[str, Any] = {}
        if artifact.mime.startswith("image/"):
            from PIL import Image

            with Image.open(path) as image:
                probe = {"width": image.width, "height": image.height}
        await svc.storage.put(svc.settings.s3_bucket_assets, key, path, content_type=artifact.mime)
    asset = Asset(
        org_id=org_id,
        kind=kind,
        storage_key=key,
        mime=artifact.mime,
        bytes=size,
        sha256=artifact.sha256,
        probe=probe,
        rights={"source": "generated", **rights},
        status="ready",
        tags=sorted(set(tags) | ({"mock"} if artifact.meta.get("mock") else set())),
        created_by=user_id,
    )
    session.add(asset)
    await session.flush()
    return asset


# ---------------------------------------------------------------------- model calls
def _route(svc: ExecServices, call: ModelCall) -> Any:
    from ce_router import RouteRequest, route

    if call.adapter_id:
        manifest = svc.catalog.manifests.get(call.adapter_id)
        if manifest is None:
            raise StudioError(f"adapter {call.adapter_id} is not installed")
        model = manifest.models[0] if manifest.models else None
        from ce_core.build import RouteDecision

        return RouteDecision(
            adapter_id=manifest.id,
            model_id=model.key if model else manifest.id,
            revision=model.source.revision if model else manifest.version,
            reason="pinned by the studio job",
        )
    return route(
        RouteRequest(
            capability=call.capability,
            routing_profile=call.routing_profile,
            language=call.language,
            height=call.height,
            width=call.width,
            prefer_mock=call.prefer_mock,
        ),
        svc.catalog,
    )


async def begin_call(svc: ExecServices, ctx: StudioContext, call: ModelCall) -> CallBegun:
    """Routes the call; runs `cpu_inproc` adapters here, else records the node and its attempt for
    dispatch to the scheduler."""
    decision = _route(svc, call)
    manifest = svc.catalog.manifests[decision.adapter_id]
    spec = capability_spec(call.capability)
    request = spec.request.model_validate(call.request)
    org_id, job_id = UUID(ctx.org_id), UUID(ctx.job_id)
    async with svc.db.transaction() as session:
        node = (
            await session.execute(
                sa.select(NodeRow).where(
                    NodeRow.org_id == org_id, NodeRow.job_id == job_id, NodeRow.node_key == call.key
                )
            )
        ).scalar_one_or_none()
        if node is None:
            node = NodeRow(org_id=org_id, job_id=job_id, version_id=None, node_key=call.key, node_kind=call.capability)
            session.add(node)
        node.route = decision.model_dump(mode="json")
        node.status = "running" if manifest.runtime.family == "cpu_inproc" else "queued"
        node.effective_seed = call.seed
        await session.flush()
        attempt = await rec.new_attempt(
            session,
            org_id,
            node.id,
            reason="initial",
            seed=call.seed,
            route=decision.identity(),
            started_at=svc.clock(),
        )
        node.attempts = attempt.attempt_no
        node_id, attempt_id = node.id, attempt.id
    begun = CallBegun(
        key=call.key,
        capability=call.capability,
        adapter_id=decision.adapter_id,
        model_key=decision.model_id,
        node_id=str(node_id),
        attempt_id=str(attempt_id),
        vram_gb=float(manifest.runtime.min_vram_gb or 0.0),
        est_seconds=float(manifest.pricing.get("seconds_per_unit", 1.0) or 1.0),
    )
    if manifest.runtime.family == "cpu_inproc":
        adapter = await svc.adapter(decision.adapter_id)
        run = StorageRunContext(svc.content, svc.scratch("studio"), seed=call.seed)
        try:
            result = await adapter.run(call.capability, request, run)
        finally:
            import shutil

            shutil.rmtree(run.scratch_dir, ignore_errors=True)
        worker = {"result": result.model_dump(mode="json"), "outputs": [], "busy_seconds": 0.0}
        begun.output = await record_call(svc, ctx, begun, worker)
        begun.done = True
        return begun
    begun.payload = {
        "adapter_id": decision.adapter_id,
        "capability": call.capability,
        "seed": call.seed,
        "request": request.model_dump(mode="json"),
        "inputs": sorted({r.sha256 for r in refs_in(request.model_dump(mode="json"))}),
        "labels": {"node": call.key, "job": ctx.job_id, "studio": ctx.kind},
        "node_key": call.key,
        "job_id": ctx.job_id,
        "infra_retries": 0,
        "work_units": 1.0,
    }
    return begun


async def dispatch_call(svc: ExecServices, ctx: StudioContext, begun: CallBegun, task_token: str) -> None:
    """Queues the call for the fleet; the workflow's activity completes when a worker finishes it."""
    for sha in begun.payload.get("inputs", []):
        if not await svc.content.has(sha):
            raise StudioError(f"input {sha} is not in the content store")
    measured = await svc.seconds_per_unit(begun.adapter_id)
    async with svc.db.transaction() as session:
        await queue.enqueue(
            session,
            org_id=UUID(ctx.org_id),
            node_id=UUID(begun.node_id or ""),
            attempt_id=UUID(begun.attempt_id or ""),
            capability=begun.capability,
            model_key=begun.model_key,
            priority=int(svc.bundle.app.build.gpu_task_priority),
            vram_gb=begun.vram_gb,
            est_seconds=measured or begun.est_seconds,
            constraints={"adapter_id": begun.adapter_id},
            payload={**begun.payload, "task_token": task_token},
        )


async def record_call(
    svc: ExecServices, ctx: StudioContext, begun: CallBegun, worker: dict[str, Any]
) -> dict[str, Any]:
    """Validates the result, registers its output artifacts and marks the node succeeded."""
    spec = capability_spec(begun.capability)
    result = spec.result.model_validate(worker.get("result") or {})
    data = result.model_dump(mode="json")
    media = {o["sha256"]: o for o in worker.get("outputs", [])}
    org_id = UUID(ctx.org_id)
    artifact_ids: list[UUID] = []
    async with svc.db.transaction() as session:
        node_id = UUID(begun.node_id or "")
        ids: dict[str, str] = {}
        for ref in refs_in(data):
            info = media.get(ref.sha256, {})
            artifact_id = await rec.register_artifact(
                session,
                org_id,
                sha256=ref.sha256,
                kind=ref.kind if ref.kind in ("image", "video", "audio") else "other",
                mime=ref.mime,
                size=ref.bytes or int(info.get("bytes", 0)),
                storage_key=str(info.get("storage_key") or content_key(ref.sha256)),
                media={"role": ref.role, **ref.meta},
                produced_by_node_id=node_id,
            )
            ids[ref.sha256] = str(artifact_id)
            artifact_ids.append(artifact_id)
        data = _fill_ids(data, ids)
        node = await session.get_one(NodeRow, node_id)
        node.status = "succeeded"
        node.artifact_ids = list(dict.fromkeys(artifact_ids))
        attempt = await session.get_one(JobAttempt, UUID(begun.attempt_id or ""))
        if attempt.status == "running":  # the scheduler finishes dispatched attempts
            attempt.status, attempt.ended_at = "succeeded", svc.clock()
    mock = bool(svc.catalog.manifests[begun.adapter_id].mock)
    return {"key": begun.key, "adapter_id": begun.adapter_id, "mock": mock, "result": data, "node_id": begun.node_id}


def _fill_ids(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        if value.get("sha256") in ids and "kind" in value:
            return {**value, "artifact_id": ids[value["sha256"]]}
        return {k: _fill_ids(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_ids(v, ids) for v in value]
    return value


async def fail_call(svc: ExecServices, ctx: StudioContext, node_id: str | None, error: str) -> None:
    if not node_id:
        return
    async with svc.db.transaction() as session:
        await session.execute(
            sa.update(NodeRow)
            .where(NodeRow.org_id == UUID(ctx.org_id), NodeRow.id == UUID(node_id))
            .values(status="failed")
        )
    _log.warning("studio call failed", job_id=ctx.job_id, error=error[:300])


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
