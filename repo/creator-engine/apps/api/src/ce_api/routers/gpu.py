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
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import RuntimeFamily
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError, UpstreamUnavailableError
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


class RegisteredProviderOut(Out):
    key: str
    name: str
    paid: bool
    mock: bool
    row_id: UUID | None = None


class ProvidersOut(Out):
    rows: list[ProviderOut]
    registered: list[RegisteredProviderOut] = Field(description="installed provider plugins (empty if unreachable)")


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
    model_config = examples([{"action": "terminate"}])
    action: Literal["terminate", "stop"] = "terminate"


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


def _worker_out(row: GpuWorker) -> WorkerOut:
    cold = None
    if row.registered_at and row.provisioned_at:
        cold = round((row.registered_at - row.provisioned_at).total_seconds(), 3)
    return WorkerOut.model_validate(row).model_copy(update={"cold_start_s": cold})


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


@router.get("/v1/gpu/workers", response_model=list[WorkerOut])
async def list_workers(
    principal: Reader, session: DbSession, state: str | None = None, limit: Annotated[int, Field(ge=1)] = 200
) -> list[WorkerOut]:
    query = sa.select(GpuWorker).order_by(GpuWorker.created_at.desc()).limit(min(limit, 500))
    if state:
        query = query.where(GpuWorker.state == state)
    else:
        query = query.where(GpuWorker.state.notin_(("stopped", "failed")))
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
    for row in rows:
        state = loaded.get(str(row.id))
        out.append(
            ProviderOut.model_validate(row).model_copy(
                update={
                    "paid": paid.get(row.kind),
                    "loaded": state is not None if registered else None,
                    "healthy": state["healthy"] if state else None,
                    "health_detail": state["detail"] if state else None,
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
    before = {"state": row.state}
    result = await _client(services).stop(str(worker_id), body.action)
    await audit(
        session, principal, "gpu_worker.stop", "gpu_worker", worker_id, request=request, before=before,
        after={"state": result["state"], "action": body.action},
    )  # fmt: skip
    return StopOut.model_validate(result)


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
