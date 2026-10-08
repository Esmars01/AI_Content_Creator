"""The scheduler service (§9, §25): the internal worker API plus the leader's background loops."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from ce_config.settings import EffectiveConfig, startup_issues
from ce_contracts.plugins import discover
from ce_db.session import Database
from ce_gpu.provider import ProviderError
from ce_obs import get_logger
from ce_storage import StorageProvider, create_storage
from ce_worker.protocol import (
    CompleteBody,
    FailBody,
    HeartbeatBody,
    HeartbeatReply,
    LeaseBody,
    LeaseReply,
    RegisterBody,
    RegisterReply,
    StatusBody,
    StatusReply,
    UploadBody,
    UploadReply,
)
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ce_scheduler.completion import ActivityCompleter, Gone, TemporalCompleter, Unavailable
from ce_scheduler.fleet import FleetManager
from ce_scheduler.leader import LeaderLoops
from ce_scheduler.providers import load_providers
from ce_scheduler.service import AuthError, Scheduler, StaleTaskError, WorkerIdentity

__all__ = ["LazyTemporalCompleter", "admin_token", "create_app", "registration_token"]

_log = get_logger("ce.scheduler")


def registration_token(effective: EffectiveConfig) -> str:
    """WORKER_TOKEN, or (dev/test) a token derived from SECRET_KEY that workers derive the same way."""
    s = effective.settings
    if s.worker_token is not None and s.worker_token.get_secret_value():
        return s.worker_token.get_secret_value()
    return hmac.new(s.secret_key.get_secret_value().encode(), b"ce-worker-registration", hashlib.sha256).hexdigest()


def admin_token(effective: EffectiveConfig) -> str:
    """The token the API presents on the scheduler's internal fleet endpoints (both derive it from
    SECRET_KEY; it is never sent to workers)."""
    secret = effective.settings.secret_key.get_secret_value().encode()
    return hmac.new(secret, b"ce-scheduler-fleet-admin", hashlib.sha256).hexdigest()


class ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    gpu_class: str
    runtime_family: str
    count: int = Field(default=1, ge=1, le=8)
    region: str | None = None
    variant: str | None = None


class ProfileProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    region: str | None = None


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(default="terminate", pattern="^(terminate|stop)$")


class LazyTemporalCompleter:
    """Connects to Temporal on first use (the scheduler may start before Temporal is ready)."""

    def __init__(self, address: str, namespace: str) -> None:
        self.address = address
        self.namespace = namespace
        self._inner: TemporalCompleter | None = None
        self._lock = asyncio.Lock()

    async def _get(self) -> TemporalCompleter:
        async with self._lock:
            if self._inner is None:
                from temporalio.client import Client
                from temporalio.contrib.pydantic import pydantic_data_converter

                try:
                    client = await Client.connect(
                        self.address, namespace=self.namespace, data_converter=pydantic_data_converter
                    )
                except Exception as exc:  # not reachable yet: transient, retried by the caller
                    raise Unavailable(f"cannot connect to Temporal at {self.address}: {exc}") from exc
                self._inner = TemporalCompleter(client)
            return self._inner

    async def complete(self, token: str, result: dict[str, Any]) -> None:
        await (await self._get()).complete(token, result)

    async def fail(self, token: str, error_class: str, message: str) -> None:
        await (await self._get()).fail(token, error_class, message)

    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None:
        await (await self._get()).heartbeat(token, details)

    async def report_cancellation(self, token: str) -> None:
        await (await self._get()).report_cancellation(token)


def _worker_storage(effective: EffectiveConfig) -> StorageProvider:
    """Presigned URLs for workers use the internal endpoint (the public one is for browsers)."""
    settings = effective.settings.model_copy(update={"s3_public_endpoint_url": None})
    return create_storage(settings)


def scheduler_of(request: Request) -> Scheduler:
    result: Scheduler = request.app.state.scheduler
    return result


def bearer(authorization: Annotated[str | None, Header()] = None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def current_worker(request: Request, token: Token) -> WorkerIdentity:
    try:
        return await scheduler_of(request).authenticate(token)
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


Token = Annotated[str | None, Depends(bearer)]
Worker = Annotated[WorkerIdentity, Depends(current_worker)]


def create_app(
    effective: EffectiveConfig,
    *,
    completer: ActivityCompleter | None = None,
    storage: StorageProvider | None = None,
    start_loops: bool = True,
) -> FastAPI:
    issues = [i for i in startup_issues(effective) if i.severity == "error"]
    if issues:
        raise RuntimeError("scheduler refuses to start: " + "; ".join(f"{i.code}: {i.message}" for i in issues))
    settings = effective.settings
    app_cfg = effective.bundle.app
    registry = discover(app_env=settings.app_env, include_mocks=settings.mock_gpu)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url, pool_size=10)
        scheduler = Scheduler(
            db=db,
            storage=storage or _worker_storage(effective),
            bucket=settings.s3_bucket_artifacts,
            registry=registry,
            completer=completer or LazyTemporalCompleter(settings.temporal_address, settings.temporal_namespace),
            config=app_cfg.scheduler,
            registration_token=registration_token(effective),
            # Worker URLs (a task's inputs and upload slots, each scoped to one content key or the
            # task's staging prefix) are granted once at lease time: they must outlive the longest
            # model node (model download and load included), not just the browser's 15 minutes (W4).
            presign_ttl_s=max(app_cfg.storage.presign_ttl_s, int(app_cfg.build.model_node_timeout_s) + 600),
            vram_classes=tuple(
                sorted({float(c.vram_gb) for c in effective.bundle.gpu_pools.classes.values() if c.vram_gb > 0})
            )
            if effective.bundle.gpu_pools
            else (),
        )
        pools = effective.bundle.gpu_pools.pools if effective.bundle.gpu_pools else []
        if not settings.mock_gpu:
            pools = [p for p in pools if not p.mock]  # no simulated capacity outside mock mode
        variants = effective.bundle.gpu_variants.variants if effective.bundle.gpu_variants else {}

        skipped: dict[str, str] = {}

        async def providers_now(previous: Any = None) -> Any:
            skipped.clear()
            return await load_providers(
                db, app_env=settings.app_env, include_mocks=settings.mock_gpu, previous=previous, skipped=skipped
            )

        events = None
        with contextlib.suppress(Exception):
            from ce_obs.events import EventBus
            from redis.asyncio import Redis

            events = EventBus(
                Redis.from_url(settings.redis_url, decode_responses=True),
                maxlen=app_cfg.events.stream_maxlen,
                retention_s=settings.sse_stream_retention_s,
                clock=lambda: datetime.now(UTC),
            )

        async def publish(org_id: Any, kind: str, data: dict[str, Any]) -> None:
            if events is not None:
                await events.publish(org_id, kind, data)

        fleet = FleetManager(
            db=db,
            pools=list(pools),
            providers=await providers_now(),
            manifests={p.id: p.manifest for p in registry.plugins.values()},
            budget_daily_usd=settings.budget_daily_usd,
            config=app_cfg.scheduler.fleet,
            variants=variants,
            scheduler_url=settings.scheduler_public_url,
            app_env=settings.app_env,
            publish=publish,
            profiles=dict(effective.bundle.gpu_profiles.profiles) if effective.bundle.gpu_profiles else {},
        )

        fleet.skipped_providers = skipped

        async def reload_providers() -> None:
            fleet.providers = await providers_now(fleet.providers)

        cfg = app_cfg.scheduler
        loops = LeaderLoops(
            db,
            [
                ("reaper", cfg.reaper_interval_s, scheduler.reap_once),
                ("cancellations", cfg.temporal_heartbeat_s, scheduler.probe_cancellations_once),
                ("completions", cfg.temporal_heartbeat_s, scheduler.deliver_pending_once),
                ("fleet", cfg.fleet_interval_s, fleet.tick),
                ("providers", 60.0, reload_providers),
                ("metrics", 5.0, scheduler.refresh_metrics),
                ("ops_metrics", cfg.ops_metrics_interval_s, scheduler.refresh_ops_metrics),
            ],
        )
        app.state.scheduler = scheduler
        app.state.loops = loops
        app.state.fleet = fleet
        app.state.reload_providers = reload_providers
        if start_loops:
            loops.start()
        _log.info("scheduler started", adapters=len(registry.plugins), providers=sorted(fleet.providers))
        try:
            yield
        finally:
            await loops.stop()
            await db.dispose()

    app = FastAPI(
        title="creator-engine scheduler",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        telemetry={"logs": False, "metrics": False, "auto_configure": False},  # ce_obs configures tracing
    )

    prefix = "/internal/v1/worker"

    @app.post(f"{prefix}/register", response_model=RegisterReply)
    async def register(body: RegisterBody, request: Request, token: Token) -> RegisterReply:
        try:
            return await scheduler_of(request).register(body, token)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.post(f"{prefix}/lease", response_model=LeaseReply)
    async def lease(body: LeaseBody, request: Request, who: Worker) -> LeaseReply:
        try:
            return await scheduler_of(request).lease(who, body)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.post(f"{prefix}/heartbeat", response_model=HeartbeatReply)
    async def heartbeat(body: HeartbeatBody, request: Request, who: Worker) -> HeartbeatReply:
        try:
            return await scheduler_of(request).heartbeat(who, body)
        except StaleTaskError as exc:
            raise HTTPException(409, f"task {exc} is no longer leased to this worker") from exc

    @app.post(f"{prefix}/status", response_model=StatusReply)
    async def status(body: StatusBody, request: Request, who: Worker) -> StatusReply:
        try:
            return await scheduler_of(request).status(who, body)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.post(f"{prefix}/upload", response_model=UploadReply)
    async def upload(body: UploadBody, request: Request, who: Worker) -> UploadReply:
        try:
            return await scheduler_of(request).uploads(who, body)
        except StaleTaskError as exc:
            raise HTTPException(409, f"task {exc} is no longer leased to this worker") from exc

    @app.post(f"{prefix}/complete", status_code=204)
    async def complete(body: CompleteBody, request: Request, who: Worker) -> Response:
        try:
            await scheduler_of(request).complete(who, body)
        except StaleTaskError as exc:
            raise HTTPException(409, f"task {exc} is no longer leased to this worker") from exc
        except Gone:
            pass
        return Response(status_code=204)

    @app.post(f"{prefix}/fail", status_code=204)
    async def fail(body: FailBody, request: Request, who: Worker) -> Response:
        try:
            await scheduler_of(request).fail(who, body)
        except StaleTaskError as exc:
            raise HTTPException(409, f"task {exc} is no longer leased to this worker") from exc
        return Response(status_code=204)

    # ------------------------------------------------------------------ fleet admin (API → scheduler)
    expected_admin = admin_token(effective)

    def fleet_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
        if not x_admin_token or not hmac.compare_digest(x_admin_token, expected_admin):
            raise HTTPException(401, "fleet admin token required")

    def fleet_of(request: Request) -> FleetManager:
        result: FleetManager = request.app.state.fleet
        return result

    admin = "/internal/v1/admin/fleet"
    guard = [Depends(fleet_admin)]

    @app.get(f"{admin}/status", dependencies=guard)
    async def fleet_status(request: Request) -> dict[str, Any]:
        return await fleet_of(request).status()

    @app.get(f"{admin}/offers", dependencies=guard)
    async def fleet_offers(request: Request, gpu_class: str | None = None, region: str | None = None) -> Any:
        return await fleet_of(request).offers(gpu_class=gpu_class, region=region)

    @app.get(f"{admin}/registered", dependencies=guard)
    async def fleet_registered() -> list[dict[str, Any]]:
        """The installed provider plugins (`gpu_providers.kind` is validated against these keys)."""
        found = discover(app_env=settings.app_env, include_mocks=settings.mock_gpu).providers("gpu")
        return [
            {"key": key, "name": p.manifest.name, "paid": "allow_paid" in p.manifest.defaults, "mock": p.manifest.mock}
            for key, p in sorted(found.items())
        ]

    @app.post(f"{admin}/reload", dependencies=guard, status_code=204)
    async def fleet_reload(request: Request) -> Response:
        await request.app.state.reload_providers()
        return Response(status_code=204)

    @app.post(f"{admin}/provision", dependencies=guard)
    async def fleet_provision(body: ProvisionRequest, request: Request) -> dict[str, Any]:
        """An administrator's explicit provision (`POST /v1/admin/gpu/workers:provision`): the same
        path as autoscaling — enrollment token, provider fallback is not applied (one provider was
        named) — in an `admin:<provider>` pool the autoscaler never stops."""
        from ce_config.schemas import GpuPool
        from ce_core.enums import RuntimeFamily

        fleet = fleet_of(request)
        try:
            family = RuntimeFamily(body.runtime_family)
        except ValueError:
            raise HTTPException(422, f"unknown runtime family {body.runtime_family!r}") from None
        if fleet._provider(body.provider) is None:
            raise HTTPException(409, f"provider {body.provider!r} is not configured or not enabled")
        regions = [body.region] if body.region else []
        if not regions:
            offers = await fleet.offers(gpu_class=body.gpu_class)
            regions = sorted({o["region"] for o in offers if o["provider"] == body.provider}) or ["local"]
        pool = GpuPool(
            id=f"admin:{body.provider}", gpu_classes=[body.gpu_class], providers=[body.provider],
            families=[family], min=0, max=body.count, idle_timeout_s=3600, target_latency_s=3600,
            spot_ok=False, regions=regions, enabled=True, autoscale=False,
        )  # fmt: skip
        provisioned: list[dict[str, Any]] = []
        from ce_scheduler.fleet import FleetDecision

        log = FleetDecision(pool.id, 0.0, body.count, 0)
        for _ in range(body.count):
            adapters = sorted(
                a for a, v in fleet.variants.items() if v.family == body.runtime_family and v.variant == body.variant
            )
            result = await fleet.scale_up(
                pool, family=body.runtime_family, variant=body.variant, adapters=adapters or None, log=log
            )
            if result is None:
                break
            provisioned.append(
                {"worker_id": str(result.worker_id), "external_id": result.external_id, "provider": result.provider,
                 "gpu_class": result.gpu_class, "variant": result.variant}
            )  # fmt: skip
        return {"provisioned": provisioned, "attempts": log.attempts}

    @app.post(f"{admin}/workers/{{worker_id}}/stop", dependencies=guard)
    async def fleet_stop(worker_id: str, body: StopRequest, request: Request) -> dict[str, Any]:
        import uuid as _uuid

        try:
            wid = _uuid.UUID(worker_id)
        except ValueError:
            raise HTTPException(404, "unknown worker") from None
        try:
            instance = await fleet_of(request).release(wid, action=body.action)
        except ProviderError as exc:
            raise HTTPException(502, str(exc)[:300]) from exc
        if instance is None:
            raise HTTPException(409, "the worker is not running, or its provider refused to stop it")
        return {"worker_id": worker_id, "state": instance.state}

    def _worker_uuid(worker_id: str) -> Any:
        import uuid as _uuid

        try:
            return _uuid.UUID(worker_id)
        except ValueError:
            raise HTTPException(404, "unknown worker") from None

    async def _act(action: Any) -> Any:
        """Operator actions: 404 unknown worker, 409 not allowed in its state, 502 the provider failed."""
        from ce_scheduler.fleet import FleetActionError

        try:
            return await action
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except FleetActionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(502, f"the provider failed: {str(exc)[:300]}") from exc

    @app.post(f"{admin}/workers/{{worker_id}}/start", dependencies=guard)
    async def fleet_start(worker_id: str, request: Request) -> dict[str, Any]:
        instance = await _act(fleet_of(request).start(_worker_uuid(worker_id)))
        return {"worker_id": worker_id, "state": "provisioning", "provider_state": instance.state}

    @app.post(f"{admin}/workers/{{worker_id}}/restart", dependencies=guard)
    async def fleet_restart(worker_id: str, request: Request) -> dict[str, Any]:
        instance = await _act(fleet_of(request).restart(_worker_uuid(worker_id)))
        return {"worker_id": worker_id, "state": "provisioning", "provider_state": instance.state}

    @app.post(f"{admin}/workers/{{worker_id}}/refresh", dependencies=guard)
    async def fleet_refresh(worker_id: str, request: Request) -> dict[str, Any]:
        result: dict[str, Any] = await _act(fleet_of(request).refresh(_worker_uuid(worker_id)))
        return result

    @app.get(f"{admin}/orphans", dependencies=guard)
    async def fleet_orphans(request: Request) -> list[dict[str, Any]]:
        """Instances labeled as the fleet's that no worker tracks (asks the providers now)."""
        found: list[dict[str, Any]] = await fleet_of(request).orphans()
        return found

    @app.post(f"{admin}/orphans/{{provider}}/{{external_id}}/terminate", dependencies=guard)
    async def fleet_terminate_orphan(provider: str, external_id: str, request: Request) -> dict[str, Any]:
        instance = await _act(fleet_of(request).terminate_orphan(provider, external_id))
        return {"provider": provider, "external_id": external_id, "state": instance.state}

    @app.get(f"{admin}/profiles", dependencies=guard)
    async def fleet_profiles(request: Request) -> list[dict[str, Any]]:
        """Model profiles with the disk and VRAM their manifests need (`ce_scheduler.profiles`)."""
        return fleet_of(request).profile_sizes()

    @app.post(f"{admin}/profiles/{{profile_id}}/provision", dependencies=guard)
    async def fleet_provision_profile(profile_id: str, body: ProfileProvisionRequest, request: Request) -> Any:
        fleet = fleet_of(request)
        profile = fleet.profiles.get(profile_id)
        if profile is None:
            raise HTTPException(404, f"unknown profile {profile_id!r}")
        if not profile.enabled:
            raise HTTPException(409, f"profile {profile_id!r} is disabled")
        return await _act(fleet.provision_profile(profile_id, profile, provider=body.provider, region=body.region))

    @app.post(f"{admin}/providers/{{key}}/test", dependencies=guard)
    async def fleet_test_provider(key: str, request: Request) -> dict[str, Any]:
        """A read-only check of one configured provider: its health call and a live offer search.
        Nothing is rented and nothing is spent."""
        fleet = fleet_of(request)
        fp = fleet._provider(key)
        if fp is None:
            reason = fleet.skipped_providers.get(key)
            raise HTTPException(409, f"provider {key!r} is not configured" + (f": {reason}" if reason else ""))
        try:
            health = await fp.provider.health()
            ok, detail = health.ok, health.detail
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        offers: list[dict[str, Any]] = []
        offers_error = None
        if ok:
            try:
                offers = [o.model_dump(mode="json") for o in await fp.provider.list_offers()]
            except ProviderError as exc:
                offers_error = str(exc)[:300]
        prices = [o["price_per_hour_usd"] for o in offers]
        return {
            "provider": key,
            "healthy": ok,
            "detail": detail,
            "offers": len(offers),
            "offers_error": offers_error,
            "cheapest_per_hour_usd": min(prices) if prices else None,
            "classes": sorted({o["gpu_class"] for o in offers}),
            "regions": sorted({o["region"] for o in offers}),
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        loops: LeaderLoops = request.app.state.loops
        return {"status": "ok", "leader": loops.is_leader}

    return app
