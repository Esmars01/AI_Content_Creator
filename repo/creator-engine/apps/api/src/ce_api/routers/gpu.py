"""GPU fleet (§25, §30; Phase 9): pools, workers and offers for org members; providers, provisioning,
stopping, enrollment and the live queue for platform admins.

Provider plugins live in the scheduler (the fleet's only authority), so pools, offers, provisioning
and stopping are answered by its internal fleet endpoints (`ce_api.scheduler`); provider rows,
workers, enrollment tokens and the queue are read and written here. Credentials are references
(`env:NAME`, `file:/path`), never values: a provider `config` carrying a secret-looking key is
refused, and no response contains a secret or a token hash. Enabling paid provisioning on a provider
(`config.allow_paid`) is the owner's spending approval (§41) and is audited as such.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import RuntimeFamily
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError, UpstreamUnavailableError
from ce_db import fleet as fleet_db
from ce_db.models.assets import GpuTask
from ce_db.models.platform import GpuProvider, GpuWorker, WorkerEnrollmentToken
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import Field

from ce_api.common import audit
from ce_api.deps import DbSession, PlatformAdmin, Reader, ServicesDep
from ce_api.scheduler import SchedulerClient
from ce_api.schemas import Body, Out, examples

router = APIRouter(tags=["gpu"])

SECRET_KEYS = ("api_key", "apikey", "token", "secret", "password", "private_key", "credential")
CREDENTIALS_REF = r"^(env:[A-Za-z_][A-Za-z0-9_]*|file:/[^\s]+)$"


def _client(services: Any) -> SchedulerClient:
    client: SchedulerClient | None = services.scheduler
    if client is None:
        raise UpstreamUnavailableError("no scheduler is configured (SCHEDULER_INTERNAL_URL)")
    return client


# ------------------------------------------------------------------ read models
class PoolOut(Out):
    id: str
    gpu_classes: list[str]
    providers: list[str]
    providers_available: list[str] = Field(description="the pool's providers the scheduler has configured")
    families: list[str]
    min: int
    max: int
    idle_timeout_s: int
    target_latency_s: int
    spot_ok: bool
    regions: list[str]
    enabled: bool
    autoscale: bool
    idle_action: str
    min_driver_version: str | None = None
    backlog_seconds: float = Field(description="queued, unheld GPU-seconds of the pool's families")
    desired: int
    current: int
    workers_by_state: dict[str, int] = Field(default_factory=dict)


class WorkerOut(Out):
    id: UUID
    provider_id: UUID | None
    provider_kind: str | None
    external_id: str | None
    runtime_family: str
    gpu_type: str
    gpu_count: int
    vram_gb: float
    region: str | None
    price_per_hour_usd: Decimal
    state: str
    pool_id: str | None
    variant: str | None
    resident_models: list[str]
    cached_models: list[str]
    provisioned_at: datetime | None
    registered_at: datetime | None
    cold_start_s: float | None = Field(default=None, description="registration − provision request")
    last_heartbeat_at: datetime | None
    started_at: datetime | None
    stopped_at: datetime | None
    terminated_at: datetime | None = None
    telemetry: dict[str, Any] = Field(
        default_factory=dict,
        description="the worker's last report (GPU utilization, VRAM, temperature, disk, model cache); "
        "a value it did not report is absent, never 0",
    )
    telemetry_at: datetime | None = None
    provider_status: dict[str, Any] = Field(default_factory=dict, description="the provider's last view")
    provider_checked_at: datetime | None = None
    model_states: dict[str, Any] = Field(default_factory=dict, description="per model key: preparation state")
    prepare_request: dict[str, Any] = Field(default_factory=dict, description="the operator's last prepare request")
    last_error: str | None = None
    current_task_id: UUID | None = None
    actions: list[str] = Field(
        default_factory=list, description="operator actions this worker's state allows (platform admins)"
    )


class OfferOut(Out):
    provider: str
    gpu_class: str
    region: str
    vram_gb: float
    price_per_hour_usd: float
    available: int
    driver_version: str | None = None
    spot: bool = False
    paid: bool = False


class ProviderOut(Out):
    id: UUID
    kind: str
    name: str
    credentials_ref: str | None
    regions: list[str]
    enabled: bool
    budget_daily_usd: Decimal | None
    config: dict[str, Any]
    paid: bool | None = Field(default=None, description="the plugin bills (None: the scheduler did not answer)")
    loaded: bool | None = Field(default=None, description="the scheduler configured it from this row")
    healthy: bool | None = None
    health_detail: str | None = None
    paid_approved: bool = Field(default=False, description="config.allow_paid: the owner approved spending")
    spent_today_usd: float | None = Field(default=None, description="today's spend of this provider's workers")
    projected_today_usd: float | None = None


class RegisteredProviderOut(Out):
    key: str
    name: str
    paid: bool
    mock: bool
    row_id: UUID | None = None


class ProvidersOut(Out):
    rows: list[ProviderOut]
    registered: list[RegisteredProviderOut] = Field(description="installed provider plugins (empty if unreachable)")
    skipped: dict[str, str] = Field(
        default_factory=dict, description="provider key → why the scheduler could not configure it (never a secret)"
    )


class QueueTaskOut(Out):
    id: UUID
    org_id: UUID
    capability: str
    model_key: str
    adapter_id: str | None
    state: str
    priority: int
    held_reason: str | None
    est_seconds: float
    vram_gb: float
    lease_worker_id: UUID | None
    lease_expires_at: datetime | None
    created_at: datetime


class QueueOut(Out):
    tasks: list[QueueTaskOut]
    by_state: dict[str, int]
    held: dict[str, int]
    spend: dict[str, float] | None = Field(default=None, description="today's fleet spend (None: scheduler down)")


class ProvisionedOut(Out):
    worker_id: UUID
    external_id: str
    provider: str
    gpu_class: str
    variant: str | None = None


class ProvisionOut(Out):
    provisioned: list[ProvisionedOut]
    attempts: list[str] = Field(description="every provider/class/region tried, in order, with its outcome")


class StopOut(Out):
    worker_id: UUID
    state: str


class ActionOut(Out):
    worker_id: UUID
    state: str
    provider_state: str | None = None
    provider_status: dict[str, Any] | None = None


class ProviderTestOut(Out):
    provider: str
    healthy: bool
    detail: str
    offers: int = Field(description="live offers found (a read-only search: nothing rented, nothing spent)")
    offers_error: str | None = None
    cheapest_per_hour_usd: float | None = None
    classes: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)


class OrphanOut(Out):
    provider: str
    external_id: str
    state: str
    price_per_hour_usd: float
    label: str | None = None
    worker_id: str | None = None


class EnrollmentOut(Out):
    token: str = Field(description="shown once: the host sets it as WORKER_TOKEN; only its hash is stored")
    runtime_family: str
    provider_id: UUID | None
    expires_at: datetime


# ------------------------------------------------------------------ request bodies
class ProviderBody(Body):
    model_config = examples(
        [
            {
                "kind": "runpod_pod",
                "name": "RunPod EU",
                "credentials_ref": "env:RUNPOD_API_KEY",
                "regions": ["eu"],
                "enabled": True,
                "budget_daily_usd": 10,
                "config": {"allow_paid": False, "max_price_per_hour_usd": 1.0},
            }
        ]
    )
    kind: str = Field(min_length=1, description="a registered GPU provider plugin key")
    name: str = Field(min_length=1, max_length=120)
    credentials_ref: Annotated[str, Field(pattern=CREDENTIALS_REF)] | None = None
    regions: list[str] = Field(default_factory=list)
    enabled: bool = False
    budget_daily_usd: Annotated[Decimal, Field(ge=0)] | None = None
    config: dict[str, Any] = Field(default_factory=dict, description="overrides of the plugin defaults; no secrets")


class ProviderPatch(Body):
    model_config = examples([{"enabled": True, "config": {"allow_paid": True}, "note": "owner approved 5 USD/day"}])
    name: str | None = Field(default=None, min_length=1, max_length=120)
    credentials_ref: Annotated[str, Field(pattern=CREDENTIALS_REF)] | None = None
    regions: list[str] | None = None
    enabled: bool | None = None
    budget_daily_usd: Annotated[Decimal, Field(ge=0)] | None = None
    config: dict[str, Any] | None = None
    note: str = Field(default="", max_length=2000, description="required when enabling paid provisioning")


class ProvisionBody(Body):
    model_config = examples([{"provider": "mock", "gpu_class": "mock_gpu", "runtime_family": "cpu_model", "count": 1}])
    provider_id: UUID | None = None
    provider: str | None = Field(default=None, description="a plugin key, for providers without a row (mock, local)")
    gpu_class: str
    runtime_family: RuntimeFamily
    count: int = Field(default=1, ge=1, le=8)
    region: str | None = None
    variant: str | None = None


class StopBody(Body):
    model_config = examples([{"action": "stop"}, {"action": "terminate", "confirm": "<the worker id>"}])
    action: Literal["terminate", "stop"] = "terminate"
    confirm: str | None = Field(
        default=None, description="terminate destroys the instance and its disk: repeat the worker id to confirm"
    )


class PrepareBody(Body):
    model_config = examples([{"models": ["infinitetalk-single", "chatterbox-turbo"], "warm": True}])
    models: list[str] | None = Field(default=None, description="model keys; omitted: every model of its adapters")
    warm: bool = Field(default=True, description="also load them into GPU memory (else stop once installed)")


class PrepareOut(Out):
    worker_id: UUID
    request_id: str
    models: list[str] | None
    warm: bool
    cancel: bool = False


class ProfileProvisionBody(Body):
    model_config = examples([{"provider_id": "0192f0a0-0000-7000-8000-0000000000a1", "region": "eu"}])
    provider_id: UUID | None = None
    provider: str | None = Field(default=None, description="a plugin key, for providers without a row")
    region: str | None = None


class ModelSizeOut(Out):
    key: str
    adapter: str
    declared_gb: float = Field(description="the manifest's declared size (weights at the pin + dependencies)")
    repo: str | None = None
    revision: str | None = None
    license: str | None = None
    required: bool = Field(default=True, description="prepared at boot; false: optional, fetched on first use")


class ProfileSizingOut(Out):
    models: list[ModelSizeOut]
    models_gb: float
    staging_gb: float
    scratch_gb: float
    image_gb: float
    disk_gb: int = Field(description="the container disk provisioned for the profile (computed, rounded up)")
    vram_gb: float
    vram_min_gb: float
    vram_recommended_gb: float
    fits: bool
    notes: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ProfileOut(Out):
    id: str
    label: str
    gpu_class: str
    vram_gb: float
    colocate: bool
    image: str | None = None
    components: list[dict[str, Any]]
    regions: list[str] = Field(default_factory=list)
    enabled: bool
    concurrency: int = 1
    persistent_cache: str = "recommended"
    resident_together: bool = True
    prewarm: str = "boot"
    cold_start: str = ""
    sizing: ProfileSizingOut


class ProfileProvisionOut(Out):
    profile: str
    provisioned: list[dict[str, Any]]
    attempts: list[str]
    sizing: ProfileSizingOut


class TerminateOrphanBody(Body):
    provider: str
    external_id: str
    confirm: str = Field(description="repeat the external id to confirm")


class DeleteProviderBody(Body):
    confirm: str = Field(description="repeat the provider's name to confirm")


class EnrollBody(Body):
    model_config = examples([{"runtime_family": "tts"}])
    provider_id: UUID | None = None
    runtime_family: RuntimeFamily


# ------------------------------------------------------------------ helpers
def _secret_keys(config: dict[str, Any], prefix: str = "") -> list[str]:
    found = []
    for key, value in config.items():
        path = f"{prefix}{key}"
        if any(s in key.lower() for s in SECRET_KEYS):
            found.append(path)
        if isinstance(value, dict):
            found += _secret_keys(value, f"{path}.")
    return found


def _check_config(config: dict[str, Any]) -> None:
    secret = _secret_keys(config)
    if secret:
        raise InvalidInputError(
            "provider config must not carry secrets: put them behind credentials_ref (env:NAME or file:/path)",
            issues=[Issue("config", path, path=f"config.{path}") for path in secret],
        )
    refs = [k for k, v in config.items() if k.endswith("_ref") and v and not re.match(CREDENTIALS_REF, str(v))]
    if refs:  # e.g. image_login_ref: a reference to the value, never the value
        raise InvalidInputError(
            "a *_ref setting is a reference (env:NAME or file:/path), never the secret itself",
            issues=[Issue("config", k, path=f"config.{k}") for k in refs],
        )


def worker_actions(row: GpuWorker) -> list[str]:
    """What an operator can do with a worker in its state. Only workers a provider runs (an external
    id and a provider) can be started, restarted or refreshed; any of those can be terminated."""
    controlled = bool(row.external_id) and bool(row.provider_kind or row.provider_id) and row.pool_id is not None
    actions: list[str] = []
    if row.state == "terminated":
        return actions
    if controlled:
        actions.append("refresh")
    if row.state == "stopped" and controlled:
        actions.append("start")
    if row.state in ("idle", "provisioning") and controlled:
        actions.append("restart")
    if row.state in ("idle", "busy", "draining", "provisioning"):
        actions.append("stop")
    if row.state in ("idle", "busy", "draining", "provisioning") or (controlled and row.state in ("stopped", "failed")):
        actions.append("terminate")
    return actions


def _worker_out(row: GpuWorker) -> WorkerOut:
    cold = None
    if row.registered_at and row.provisioned_at:
        cold = round((row.registered_at - row.provisioned_at).total_seconds(), 3)
    return WorkerOut.model_validate(row).model_copy(update={"cold_start_s": cold, "actions": worker_actions(row)})


async def _provider(session: Any, provider_id: UUID) -> GpuProvider:
    row = await session.get(GpuProvider, provider_id)
    if row is None:
        raise NotFoundError("GPU provider not found", table="gpu_providers")
    return row


async def _reload(services: Any) -> None:
    """Best effort: the scheduler also reloads providers every minute."""
    with contextlib.suppress(UpstreamUnavailableError):
        await _client(services).reload()


# ------------------------------------------------------------------ org members
@router.get("/v1/gpu/pools", response_model=list[PoolOut])
async def list_pools(principal: Reader, services: ServicesDep) -> list[PoolOut]:
    status = await _client(services).status()
    return [PoolOut.model_validate(p) for p in status["pools"]]


WORKER_SCOPES = {
    "live": ("provisioning", "idle", "busy", "draining"),
    "active": ("provisioning", "idle", "busy", "draining", "stopped", "failed"),  # everything not destroyed
    "stopped": ("stopped",),
    "failed": ("failed",),
    "terminated": ("terminated",),
}


@router.get("/v1/gpu/workers", response_model=list[WorkerOut])
async def list_workers(
    principal: Reader,
    session: DbSession,
    state: str | None = None,
    scope: Literal["live", "active", "stopped", "failed", "terminated", "all"] = "active",
    limit: Annotated[int, Field(ge=1)] = 200,
) -> list[WorkerOut]:
    """Workers, newest first. The default scope (`active`) includes stopped workers (they keep their
    disk and can be started again) and failed ones; `terminated` lists destroyed instances."""
    query = sa.select(GpuWorker).order_by(GpuWorker.created_at.desc()).limit(min(limit, 500))
    if state:
        query = query.where(GpuWorker.state == state)
    elif scope != "all":
        query = query.where(GpuWorker.state.in_(WORKER_SCOPES[scope]))
    return [_worker_out(r) for r in (await session.execute(query)).scalars()]


@router.get("/v1/gpu/offers", response_model=list[OfferOut])
async def list_offers(
    principal: Reader, services: ServicesDep, gpu_class: str | None = None, region: str | None = None
) -> list[OfferOut]:
    return [OfferOut.model_validate(o) for o in await _client(services).offers(gpu_class=gpu_class, region=region)]


# ------------------------------------------------------------------ platform admins
@router.get("/v1/admin/gpu/providers", response_model=ProvidersOut)
async def list_providers(principal: PlatformAdmin, session: DbSession, services: ServicesDep) -> ProvidersOut:
    rows = list((await session.execute(sa.select(GpuProvider).order_by(GpuProvider.name))).scalars())
    try:
        status = await _client(services).status()
        registered = await _client(services).registered()
    except UpstreamUnavailableError:
        status, registered = {"providers": []}, []
    loaded = {p["row_id"]: p for p in status["providers"] if p.get("row_id")}
    paid = {r["key"]: r["paid"] for r in registered}
    by_kind = {p["key"]: p for p in status["providers"]}
    out = []
    now = services.clock()
    for row in rows:
        state = loaded.get(str(row.id))
        report = await fleet_db.spend_report(session, now=now, provider_id=row.id)
        out.append(
            ProviderOut.model_validate(row).model_copy(
                update={
                    "paid": paid.get(row.kind),
                    "loaded": state is not None if registered else None,
                    "healthy": state["healthy"] if state else None,
                    "health_detail": state["detail"] if state else None,
                    "paid_approved": bool((row.config or {}).get("allow_paid")),
                    "spent_today_usd": report.spent_usd,
                    "projected_today_usd": report.projected_usd,
                }
            )
        )
    return ProvidersOut(
        rows=out,
        registered=[
            RegisteredProviderOut(
                **r, row_id=UUID(by_kind[r["key"]]["row_id"]) if by_kind.get(r["key"], {}).get("row_id") else None
            )
            for r in registered
        ],
        skipped={str(k): str(v) for k, v in dict(status.get("skipped_providers") or {}).items()},
    )


async def _validate_kind(services: Any, kind: str) -> bool:
    """The registered plugin's paid flag; refuses an unknown kind (§25: validated, never an enum)."""
    registered = {r["key"]: r for r in await _client(services).registered()}
    if kind not in registered:
        raise InvalidInputError(
            f"{kind!r} is not a registered GPU provider",
            issues=[Issue("kind", f"registered: {sorted(registered)}", path="kind")],
        )
    return bool(registered[kind]["paid"])


@router.post("/v1/admin/gpu/providers", response_model=ProviderOut, status_code=201)
async def create_provider(
    body: ProviderBody,
    principal: PlatformAdmin,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> ProviderOut:
    _check_config(body.config)
    paid = await _validate_kind(services, body.kind)
    if paid and body.config.get("allow_paid"):
        raise ConflictError(
            "create the provider first, then enable paid provisioning with a PATCH and a note (the owner's approval)",
            issues=[Issue("config.allow_paid", "set through PATCH with a note", path="config.allow_paid")],
        )
    if (await session.execute(sa.select(GpuProvider.id).where(GpuProvider.name == body.name))).first():
        raise ConflictError("a provider with this name exists", issues=[Issue("name", body.name, path="name")])
    row = GpuProvider(
        kind=body.kind,
        name=body.name,
        credentials_ref=body.credentials_ref,
        regions=body.regions,
        enabled=body.enabled,
        budget_daily_usd=body.budget_daily_usd,
        config=body.config,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit(
        session, principal, "gpu_provider.create", "gpu_provider", row.id, request=request,
        after={"kind": row.kind, "name": row.name, "enabled": row.enabled, "credentials_ref": row.credentials_ref,
               "budget_daily_usd": str(row.budget_daily_usd) if row.budget_daily_usd is not None else None,
               "config": row.config},
    )  # fmt: skip
    background.add_task(_reload, services)  # after the transaction commits
    return ProviderOut.model_validate(row).model_copy(update={"paid": paid})


@router.patch("/v1/admin/gpu/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: UUID,
    body: ProviderPatch,
    principal: PlatformAdmin,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> ProviderOut:
    row = await _provider(session, provider_id)
    before = {
        "name": row.name, "credentials_ref": row.credentials_ref, "regions": list(row.regions or []),
        "enabled": row.enabled, "config": dict(row.config or {}),
        "budget_daily_usd": str(row.budget_daily_usd) if row.budget_daily_usd is not None else None,
    }  # fmt: skip
    changes = body.model_dump(exclude_unset=True, exclude={"note"})
    if "config" in changes:
        _check_config(changes["config"] or {})
        changes["config"] = changes["config"] or {}
    enabling_paid = bool((changes.get("config") or {}).get("allow_paid")) and not (row.config or {}).get("allow_paid")
    if enabling_paid and not body.note.strip():
        raise InvalidInputError(
            "enabling paid provisioning needs a note recording the owner's spending approval",
            issues=[Issue("note", "required with config.allow_paid", path="note")],
        )
    for key, value in changes.items():
        setattr(row, key, value)
    await session.flush()
    await session.refresh(row)
    after = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items()}
    action = "gpu_provider.paid_enabled" if enabling_paid else "gpu_provider.update"
    await audit(
        session, principal, action, "gpu_provider", row.id, request=request, before=before,
        after={**after, "note": body.note} if body.note else after,
    )  # fmt: skip
    background.add_task(_reload, services)  # after the transaction commits
    return ProviderOut.model_validate(row)


@router.post("/v1/admin/gpu/workers:provision", response_model=ProvisionOut)
async def provision_workers(
    body: ProvisionBody, principal: PlatformAdmin, request: Request, session: DbSession, services: ServicesDep
) -> ProvisionOut:
    """Provisions `count` workers on one provider now (the autoscaler's path: enrollment token, price
    captured at provision, fleet costs on stop). Paid providers still refuse without `allow_paid`."""
    if (body.provider_id is None) == (body.provider is None):
        raise InvalidInputError(
            "name the provider by provider_id (a configured row) or provider (a plugin key without a row)",
            issues=[Issue("provider_id", "exactly one of provider_id, provider", path="provider_id")],
        )
    if body.provider_id is not None:
        row = await _provider(session, body.provider_id)
        if not row.enabled:
            raise ConflictError("the provider is disabled", issues=[Issue("provider_id", "disabled")])
        key = row.kind
    else:
        key = str(body.provider)
    result = await _client(services).provision(
        {"provider": key, "gpu_class": body.gpu_class, "runtime_family": body.runtime_family.value,
         "count": body.count, "region": body.region, "variant": body.variant}
    )  # fmt: skip
    await audit(
        session, principal, "gpu_worker.provision", "gpu_provider", body.provider_id or key, request=request,
        after={"provider": key, "gpu_class": body.gpu_class, "runtime_family": body.runtime_family.value,
               "count": body.count, "provisioned": [p["external_id"] for p in result["provisioned"]]},
    )  # fmt: skip
    return ProvisionOut.model_validate(result)


@router.post("/v1/admin/gpu/workers/{worker_id}:stop", response_model=StopOut)
async def stop_worker(
    worker_id: UUID,
    body: StopBody,
    principal: PlatformAdmin,
    request: Request,
    session: DbSession,
    services: ServicesDep,
) -> StopOut:
    row = await session.get(GpuWorker, worker_id)
    if row is None:
        raise NotFoundError("worker not found", table="gpu_workers")
    _require_terminate_confirmation(body, worker_id)
    before = {"state": row.state}
    result = await _client(services).stop(str(worker_id), body.action)
    await audit(
        session, principal, "gpu_worker.stop", "gpu_worker", worker_id, request=request, before=before,
        after={"state": result["state"], "action": body.action},
    )  # fmt: skip
    return StopOut.model_validate(result)


def _require_terminate_confirmation(body: StopBody, worker_id: UUID) -> None:
    if body.action == "terminate" and (body.confirm or "").strip() != str(worker_id):
        raise InvalidInputError(
            "terminating destroys the instance and its disk: confirm with the worker id",
            issues=[Issue("confirm", "repeat the worker id", path="confirm")],
        )


async def _worker_action(
    action: Literal["start", "restart", "refresh"],
    worker_id: UUID,
    principal: Any,
    request: Request,
    session: Any,
    services: Any,
) -> ActionOut:
    row = await session.get(GpuWorker, worker_id)
    if row is None:
        raise NotFoundError("worker not found", table="gpu_workers")
    before = {"state": row.state}
    result = await _client(services).worker_action(str(worker_id), action)
    if action != "refresh":  # start and restart resume billing: audited like a provision
        await audit(
            session, principal, f"gpu_worker.{action}", "gpu_worker", worker_id, request=request, before=before,
            after={"state": result.get("state"), "provider_state": result.get("provider_state")},
        )  # fmt: skip
    return ActionOut.model_validate(result)


@router.post("/v1/admin/gpu/workers/{worker_id}:start", response_model=ActionOut)
async def start_worker(
    worker_id: UUID, principal: PlatformAdmin, request: Request, session: DbSession, services: ServicesDep
) -> ActionOut:
    """Starts a stopped worker's instance again (its disk and model cache were kept). Billing resumes."""
    return await _worker_action("start", worker_id, principal, request, session, services)


@router.post("/v1/admin/gpu/workers/{worker_id}:restart", response_model=ActionOut)
async def restart_worker(
    worker_id: UUID, principal: PlatformAdmin, request: Request, session: DbSession, services: ServicesDep
) -> ActionOut:
    """Restarts an idle or provisioning worker's container (a reboot in place where the provider can)."""
    return await _worker_action("restart", worker_id, principal, request, session, services)


@router.post("/v1/admin/gpu/workers/{worker_id}:refresh", response_model=ActionOut)
async def refresh_worker(
    worker_id: UUID, principal: PlatformAdmin, request: Request, session: DbSession, services: ServicesDep
) -> ActionOut:
    """Asks the provider about the instance now and reconciles the worker with its answer."""
    return await _worker_action("refresh", worker_id, principal, request, session, services)


@router.delete("/v1/admin/gpu/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: UUID,
    body: DeleteProviderBody,
    principal: PlatformAdmin,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> None:
    """Deletes a provider row that nothing references; one with workers or costs on record keeps its
    history and can only be disabled (PATCH enabled=false)."""
    row = await _provider(session, provider_id)
    if body.confirm.strip() != row.name:
        raise InvalidInputError(
            "confirm with the provider's name", issues=[Issue("confirm", "repeat the name", path="confirm")]
        )
    from ce_db.models.platform import FleetCost

    used = (
        await session.execute(
            sa.select(sa.func.count()).select_from(GpuWorker).where(GpuWorker.provider_id == provider_id)
        )
    ).scalar_one() + (
        await session.execute(
            sa.select(sa.func.count()).select_from(FleetCost).where(FleetCost.provider_id == provider_id)
        )
    ).scalar_one()
    if used:
        raise ConflictError(
            "this provider has workers or costs on record: disable it instead (PATCH enabled=false)",
            issues=[Issue("provider_id", f"{used} referencing rows")],
        )
    await session.execute(sa.delete(WorkerEnrollmentToken).where(WorkerEnrollmentToken.provider_id == provider_id))
    await session.delete(row)
    await audit(
        session, principal, "gpu_provider.delete", "gpu_provider", provider_id, request=request,
        before={"kind": row.kind, "name": row.name},
    )  # fmt: skip
    background.add_task(_reload, services)


@router.post("/v1/admin/gpu/providers/{provider_id}:test", response_model=ProviderTestOut)
async def test_provider(
    provider_id: UUID, principal: PlatformAdmin, session: DbSession, services: ServicesDep
) -> ProviderTestOut:
    """The provider's health call and a live offer search, read-only: nothing is rented or spent."""
    row = await _provider(session, provider_id)
    return ProviderTestOut.model_validate(await _client(services).test_provider(row.kind))


@router.post("/v1/admin/gpu/workers/{worker_id}:prepare", response_model=PrepareOut, status_code=202)
async def prepare_worker(
    worker_id: UUID, body: PrepareBody, principal: PlatformAdmin, request: Request, session: DbSession
) -> PrepareOut:
    """Asks a live worker to fetch and verify models into its cache (once per cache, never per job) and,
    with `warm`, load them into GPU memory. The scheduler hands the request over at the worker's next
    lease poll; progress (bytes, speed, ETA, state) shows in the worker's `model_states`."""
    row = await session.get(GpuWorker, worker_id, with_for_update=True)
    if row is None:
        raise NotFoundError("worker not found", table="gpu_workers")
    if row.state not in ("idle", "busy", "provisioning"):
        raise ConflictError(f"the worker is {row.state}: start it first", issues=[Issue("state", row.state)])
    request_id = secrets.token_hex(8)
    row.prepare_request = {
        "id": request_id,
        "models": body.models,
        "warm": body.warm,
        "requested_by": str(principal.user_id),
        "requested_at": datetime.now(tz=UTC).isoformat(),
    }
    await audit(
        session, principal, "gpu_worker.prepare", "gpu_worker", worker_id, request=request,
        after={"models": body.models, "warm": body.warm, "request_id": request_id},
    )  # fmt: skip
    return PrepareOut(worker_id=worker_id, request_id=request_id, models=body.models, warm=body.warm)


@router.post("/v1/admin/gpu/workers/{worker_id}:cancel-prepare", response_model=PrepareOut)
async def cancel_prepare(worker_id: UUID, principal: PlatformAdmin, request: Request, session: DbSession) -> PrepareOut:
    """Stops the worker's running prepare: its download is aborted and its staging removed; what was
    already installed stays. Retry with a new prepare."""
    row = await session.get(GpuWorker, worker_id, with_for_update=True)
    if row is None:
        raise NotFoundError("worker not found", table="gpu_workers")
    current = dict(row.prepare_request or {})
    if not current.get("id"):
        raise ConflictError("no prepare was requested for this worker", issues=[Issue("prepare_request", "none")])
    current["cancel"] = True
    row.prepare_request = current
    await audit(session, principal, "gpu_worker.cancel_prepare", "gpu_worker", worker_id, request=request)
    return PrepareOut(
        worker_id=worker_id, request_id=str(current["id"]), models=current.get("models"),
        warm=bool(current.get("warm", True)), cancel=True,
    )  # fmt: skip


@router.get("/v1/admin/gpu/profiles", response_model=list[ProfileOut])
async def list_profiles(principal: PlatformAdmin, services: ServicesDep) -> list[ProfileOut]:
    """Model profiles with the disk and VRAM computed from their manifests."""
    return [ProfileOut.model_validate(p) for p in await _client(services).profiles()]


@router.post("/v1/admin/gpu/profiles/{profile_id}:provision", response_model=ProfileProvisionOut)
async def provision_profile(
    profile_id: str,
    body: ProfileProvisionBody,
    principal: PlatformAdmin,
    request: Request,
    session: DbSession,
    services: ServicesDep,
) -> ProfileProvisionOut:
    """Rents what the profile needs now (a colocated profile: one instance, one worker per family), with
    the computed disk; each worker prepares its models right after boot. Paid providers still refuse
    without the owner's approval."""
    if (body.provider_id is None) == (body.provider is None):
        raise InvalidInputError(
            "name the provider by provider_id (a configured row) or provider (a plugin key without a row)",
            issues=[Issue("provider_id", "exactly one of provider_id, provider", path="provider_id")],
        )
    if body.provider_id is not None:
        row = await _provider(session, body.provider_id)
        if not row.enabled:
            raise ConflictError("the provider is disabled", issues=[Issue("provider_id", "disabled")])
        key = row.kind
    else:
        key = str(body.provider)
    result = await _client(services).provision_profile(profile_id, key, body.region)
    await audit(
        session, principal, "gpu_profile.provision", "gpu_profile", profile_id, request=request,
        after={"provider": key, "region": body.region, "disk_gb": result.get("sizing", {}).get("disk_gb"),
               "provisioned": [p.get("external_id") for p in result.get("provisioned", [])]},
    )  # fmt: skip
    return ProfileProvisionOut.model_validate(result)


@router.get("/v1/admin/gpu/orphans", response_model=list[OrphanOut])
async def list_orphans(principal: PlatformAdmin, services: ServicesDep) -> list[OrphanOut]:
    """Instances labeled as the fleet's that no worker tracks (they may still bill)."""
    return [OrphanOut.model_validate(o) for o in await _client(services).orphans()]


@router.post("/v1/admin/gpu/orphans:terminate", response_model=OrphanOut)
async def terminate_orphan(
    body: TerminateOrphanBody, principal: PlatformAdmin, request: Request, session: DbSession, services: ServicesDep
) -> OrphanOut:
    if body.confirm.strip() != body.external_id:
        raise InvalidInputError(
            "confirm with the instance's external id", issues=[Issue("confirm", "repeat the external id")]
        )
    result = await _client(services).terminate_orphan(body.provider, body.external_id)
    await audit(
        session, principal, "gpu_orphan.terminate", "gpu_instance", f"{body.provider}:{body.external_id}",
        request=request, after=result,
    )  # fmt: skip
    return OrphanOut(provider=body.provider, external_id=body.external_id, state=result["state"], price_per_hour_usd=0)


@router.post("/v1/admin/gpu/workers:enroll", response_model=EnrollmentOut, status_code=201)
async def enroll_worker(
    body: EnrollBody, principal: PlatformAdmin, request: Request, session: DbSession, services: ServicesDep
) -> EnrollmentOut:
    """A one-time token for a self-managed GPU host (§30): the host starts `python -m ce_worker`
    with `WORKER_TOKEN=<token>`; the token is shown once and expires after `ENROLLMENT_TOKEN_TTL_S`."""
    if body.provider_id is not None:
        await _provider(session, body.provider_id)
    token = secrets.token_urlsafe(32)
    expires = services.clock() + timedelta(seconds=services.settings.enrollment_token_ttl_s)
    row = WorkerEnrollmentToken(
        provider_id=body.provider_id,
        runtime_family=body.runtime_family.value,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),  # as the scheduler hashes it
        expires_at=expires,
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    await audit(
        session, principal, "gpu_worker.enroll", "worker_enrollment_token", row.id, request=request,
        after={"runtime_family": body.runtime_family.value, "provider_id": str(body.provider_id or ""),
               "expires_at": expires.isoformat()},
    )  # fmt: skip
    return EnrollmentOut(
        token=token, runtime_family=body.runtime_family.value, provider_id=body.provider_id, expires_at=expires
    )


@router.get("/v1/admin/gpu/queue", response_model=QueueOut)
async def gpu_queue(
    principal: PlatformAdmin, session: DbSession, services: ServicesDep, limit: Annotated[int, Field(ge=1)] = 200
) -> QueueOut:
    """Live `gpu_tasks` (queued, leased, running), counts by state and hold reason, and today's spend."""
    live = ("queued", "leased", "running")
    rows = (
        await session.execute(
            sa.select(GpuTask)
            .where(GpuTask.state.in_(live))
            .order_by(GpuTask.priority.desc(), GpuTask.created_at)
            .limit(min(limit, 1000))
        )
    ).scalars()
    tasks = [
        QueueTaskOut(
            id=t.id,
            org_id=t.org_id,
            capability=t.capability,
            model_key=t.model_key,
            adapter_id=(t.constraints or {}).get("adapter_id"),
            state=t.state,
            priority=t.priority,
            held_reason=t.held_reason,
            est_seconds=float(t.est_seconds or 0),
            vram_gb=float(t.vram_gb or 0),
            lease_worker_id=t.lease_worker_id,
            lease_expires_at=t.lease_expires_at,
            created_at=t.created_at,
        )
        for t in rows
    ]
    by_state = dict(
        (
            await session.execute(
                sa.select(GpuTask.state, sa.func.count()).where(GpuTask.state.in_(live)).group_by(GpuTask.state)
            )
        ).all()
    )
    held = dict(
        (
            await session.execute(
                sa.select(GpuTask.held_reason, sa.func.count())
                .where(GpuTask.state == "queued", GpuTask.held_reason.is_not(None))
                .group_by(GpuTask.held_reason)
            )
        ).all()
    )
    spend = None
    with contextlib.suppress(UpstreamUnavailableError):
        spend = (await _client(services).status())["spend"]
    return QueueOut(
        tasks=tasks, by_state={str(k): int(v) for k, v in by_state.items()},
        held={str(k): int(v) for k, v in held.items()}, spend=spend,
    )  # fmt: skip
