"""The fleet manager (§25, Phase 9). The scheduler is the fleet's only authority: it asks providers
for hosts, gives each one a one-time enrollment token, and records what every host cost.

Every tick (leader only):

1. **Provisioning watch.** A worker the fleet provisioned that has not registered within
   `scheduler.fleet.provision_timeout_s`, or that its provider reports failed or gone, is terminated
   and marked `failed`; its provisioned time goes to `fleet_costs`.
2. **Budget.** Today's spend and its projection (`ce_db.fleet.spend_report`): while the projection
   exceeds `BUDGET_DAILY_USD`, queued low-priority tasks are held; tasks of a video or project over
   its `budget_usd` are held whatever their priority (`ce_db.fleet.apply_holds`). Newly held work
   raises a `budget_alert` notification and a `budget.alert` event for the org, once per cooldown.
3. **Scale up.** Per enabled `autoscale` pool: desired workers = ⌈backlog GPU-seconds of the pool's
   families (held tasks excluded) ÷ target latency⌉ clamped to [min, max]. Growth stops when the
   added burn would push the projection over the daily budget, or a provider's own
   `budget_daily_usd`. A stopped worker of the pool is restarted before a new one is provisioned.
   The image variant is the one serving the adapters with the most backlog
   (`config/gpu/variants.yaml`). Providers are tried in pool order and classes in pool order;
   `NoCapacityError`, a provider error and an offer whose NVIDIA driver is older than the pool's
   `min_driver_version` all fall through to the next class, then the next provider.
4. **Scale down.** Idle fleet workers above the desired size, idle longer than `idle_timeout_s`,
   are terminated (or stopped, `idle_action: stop`) and their lifetime goes to `fleet_costs`.

Pools whose workers are started elsewhere (`autoscale: false`: compose `worker-cpu`, self-managed
hosts) only report their desired size. Paid providers are inert until the owner approves spending
(`ce_scheduler.providers`, §41).
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_config.schemas import FleetConfig, GpuPool, GpuVariant
from ce_contracts.manifest import PluginManifest
from ce_db import fleet as fleet_db
from ce_db.models.assets import GpuTask, JobAttempt, Notification
from ce_db.models.platform import GpuWorker, WorkerEnrollmentToken
from ce_db.session import Database
from ce_gpu.provider import GPUProvider, NoCapacityError, ProviderError, ProviderInstance, ProvisionSpec
from ce_obs import get_logger
from ce_obs.events import EventType
from ce_obs.metrics import FLEET_DESIRED, FLEET_PROVISIONS, FLEET_SPEND

from ce_scheduler.providers import FleetProvider

__all__ = [
    "EventPublisher",
    "FleetActionError",
    "FleetDecision",
    "FleetManager",
    "ProvisionResult",
    "desired_workers",
    "driver_at_least",
]

_log = get_logger("ce.scheduler.fleet")

EventPublisher = Callable[[UUID, str, dict[str, Any]], Awaitable[None]]

RECONCILED_STATES = ("idle", "busy", "draining", "stopped")  # provisioning has its own watch


class FleetActionError(Exception):
    """An operator action the worker's state does not allow (the API answers 409)."""


def _provider_status(instance: ProviderInstance) -> dict[str, Any]:
    """What the console shows of the provider's view; no secrets ever reach `detail`."""
    return {
        "state": instance.state,
        "price_per_hour_usd": instance.price_per_hour_usd,
        "detail": {k: v for k, v in instance.detail.items() if v is not None},
    }


def desired_workers(backlog_seconds: float, pool: GpuPool) -> int:
    wanted = math.ceil(backlog_seconds / pool.target_latency_s) if backlog_seconds > 0 else 0
    return max(pool.min, min(pool.max, wanted))


def driver_at_least(found: str | None, minimum: str | None) -> bool:
    """Numeric dotted-version comparison; an unknown driver passes (the worker's own startup check
    refuses a CUDA image on an older driver, §36)."""
    if not minimum or not found:
        return True

    def parts(value: str) -> tuple[int, ...]:
        return tuple(int(p) for p in value.split(".") if p.isdigit())

    return parts(found) >= parts(minimum)


@dataclass
class ProvisionResult:
    worker_id: UUID
    external_id: str
    provider: str
    gpu_class: str
    variant: str | None


@dataclass
class FleetDecision:
    pool: str
    backlog_seconds: float
    desired: int
    current: int
    provisioned: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)  # "provider/class: outcome" in fallback order
    held: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class FleetManager:
    db: Database
    pools: list[GpuPool]
    providers: Mapping[str, FleetProvider | GPUProvider]
    manifests: Mapping[str, PluginManifest]
    budget_daily_usd: float
    config: FleetConfig = field(default_factory=FleetConfig)
    variants: Mapping[str, GpuVariant] = field(default_factory=dict)
    scheduler_url: str = "http://localhost:8100"
    app_env: str = "dev"
    publish: EventPublisher | None = None
    gpu_prices: Mapping[str, float] = field(default_factory=dict)  # estimate when no offer gives a price
    clock: Callable[[], datetime] = _utcnow
    _alerted: dict[tuple[str, str], datetime] = field(default_factory=dict)
    _reconciled_at: datetime | None = None
    last_orphans: list[dict[str, Any]] = field(default_factory=list)  # from the leader's last sweep
    skipped_providers: dict[str, str] = field(default_factory=dict)  # provider key → why it is not configured

    def __post_init__(self) -> None:
        self.providers = {
            key: p if isinstance(p, FleetProvider) else FleetProvider(key=key, provider=p)
            for key, p in self.providers.items()
        }

    def _provider(self, key: str) -> FleetProvider | None:
        found = self.providers.get(key)
        return found if isinstance(found, FleetProvider) else None

    def _family(self, adapter_id: str) -> str | None:
        manifest = self.manifests.get(adapter_id)
        return manifest.runtime.family if manifest else None

    # ------------------------------------------------------------------ backlog
    async def _backlog(self) -> dict[str, float]:
        """Queued, unheld GPU-seconds per adapter."""
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    sa.select(
                        GpuTask.constraints["adapter_id"].astext.label("adapter"), sa.func.sum(GpuTask.est_seconds)
                    )
                    .where(GpuTask.state == "queued", GpuTask.held_reason.is_(None))
                    .group_by(sa.text("1"))
                )
            ).all()
        return {str(adapter): float(seconds or 0.0) for adapter, seconds in rows if adapter}

    def _pool_backlog(self, pool: GpuPool, backlog: Mapping[str, float]) -> dict[str, float]:
        families = {str(f) for f in pool.families}
        return {a: s for a, s in backlog.items() if self._family(a) in families}

    def _target(self, pool: GpuPool, backlog: Mapping[str, float]) -> tuple[str, str | None, list[str]]:
        """(family, variant, adapters) of the image serving the most backlog in this pool."""
        groups: dict[tuple[str, str | None], float] = {}
        for adapter, seconds in backlog.items():
            variant = self.variants.get(adapter)
            family = variant.family if variant else self._family(adapter)
            if family is None:
                continue
            key = (str(family), variant.variant if variant else None)
            groups[key] = groups.get(key, 0.0) + seconds
        if not groups:
            return str(pool.families[0]), None, []
        family, chosen = max(groups.items(), key=lambda kv: (kv[1], kv[0][0], kv[0][1] or ""))[0]
        adapters = sorted(
            a for a, v in self.variants.items() if chosen is not None and v.family == family and v.variant == chosen
        )
        return family, chosen, adapters

    # ------------------------------------------------------------------ workers
    async def _pool_workers(self, pool: GpuPool) -> list[GpuWorker]:
        """The pool's live workers: the ones it provisioned, plus statically started workers of its
        classes (they count towards its size; the fleet never stops those)."""
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    sa.select(GpuWorker)
                    .where(
                        GpuWorker.state.in_(fleet_db.LIVE_STATES),
                        sa.or_(
                            GpuWorker.pool_id == pool.id,
                            sa.and_(GpuWorker.pool_id.is_(None), GpuWorker.gpu_type.in_(pool.gpu_classes)),
                        ),
                    )
                    .order_by(GpuWorker.created_at)
                )
            ).scalars()
            return list(rows)

    # ------------------------------------------------------------------ read-only status
    async def status(self) -> dict[str, Any]:
        """Pools (backlog, desired and live workers), today's spend and the configured providers —
        computed from the database, so any scheduler replica answers the same."""
        now = self.clock()
        async with self.db.session() as session:
            report = await fleet_db.spend_report(session, now=now, horizon_h=self.config.spend_horizon_h)
            held = dict(
                (
                    await session.execute(
                        sa.select(GpuTask.held_reason, sa.func.count())
                        .where(GpuTask.state == "queued", GpuTask.held_reason.is_not(None))
                        .group_by(GpuTask.held_reason)
                    )
                ).all()
            )
        backlog = await self._backlog()
        pools = []
        for pool in self.pools:
            pool_backlog = self._pool_backlog(pool, backlog)
            seconds = sum(pool_backlog.values())
            workers = await self._pool_workers(pool)
            states: dict[str, int] = {}
            for w in workers:
                states[w.state] = states.get(w.state, 0) + 1
            pools.append(
                {
                    **pool.model_dump(mode="json"),
                    "backlog_seconds": round(seconds, 3),
                    "desired": desired_workers(seconds, pool) if pool.enabled else 0,
                    "current": len(workers),
                    "workers_by_state": states,
                    "providers_available": [k for k in pool.providers if self._provider(k) is not None],
                }
            )
        providers = []
        for key, fp in sorted(self.providers.items()):
            assert isinstance(fp, FleetProvider)
            try:
                health = await fp.provider.health()
                ok, detail = health.ok, health.detail
            except Exception as exc:
                ok, detail = False, type(exc).__name__
            providers.append(
                {
                    "key": key,
                    "row_id": str(fp.row_id) if fp.row_id else None,
                    "name": fp.name,
                    "paid": fp.paid,
                    "budget_daily_usd": fp.budget_daily_usd,
                    "healthy": ok,
                    "detail": detail,
                }
            )
        return {
            "pools": pools,
            "spend": {**report.as_dict(), "budget_daily_usd": self.budget_daily_usd},
            "held": {str(k): int(v) for k, v in held.items()},
            "providers": providers,
            "skipped_providers": dict(self.skipped_providers),
            "orphans": list(self.last_orphans),
        }

    async def offers(self, gpu_class: str | None = None, region: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, fp in sorted(self.providers.items()):
            assert isinstance(fp, FleetProvider)
            try:
                found = await fp.provider.list_offers(gpu_class=gpu_class, region=region)
            except ProviderError as exc:
                _log.info("offers unavailable", provider=key, error=str(exc)[:200])
                continue
            out += [{**o.model_dump(mode="json"), "paid": fp.paid} for o in found]
        return out

    # ------------------------------------------------------------------ tick
    async def tick(self) -> list[FleetDecision]:
        now = self.clock()
        due = (
            self._reconciled_at is None
            or (now - self._reconciled_at).total_seconds() >= self.config.reconcile_interval_s
        )
        if due:
            self._reconciled_at = now
            await self.reconcile()
            self.last_orphans = await self.orphans()  # adopts untracked instances of provisioning rows
            if self.last_orphans:
                _log.warning("orphan instances", count=len(self.last_orphans), orphans=self.last_orphans[:10])
        failed = await self._watch_provisioning(now)
        await self._terminate_failed(now)
        async with self.db.transaction() as session:
            report = await fleet_db.spend_report(session, now=now, horizon_h=self.config.spend_horizon_h)
            daily_exceeded = bool(self.budget_daily_usd) and report.projected_usd > self.budget_daily_usd
            change = await fleet_db.apply_holds(
                session, daily_exceeded=daily_exceeded, low_priority_max=self.config.low_priority_max
            )
        FLEET_SPEND.labels("spent").set(report.spent_usd)
        FLEET_SPEND.labels("projected").set(report.projected_usd)
        FLEET_SPEND.labels("budget").set(self.budget_daily_usd)
        if change.held_scopes:
            await self._alert(change.held_scopes, report, now)
        if daily_exceeded:
            _log.warning(
                "projected daily spend exceeds BUDGET_DAILY_USD",
                projected=report.projected_usd,
                budget=self.budget_daily_usd,
            )
        backlog = await self._backlog()
        decisions: list[FleetDecision] = []
        burn_added = 0.0
        for pool in self.pools:
            pool_backlog = self._pool_backlog(pool, backlog)
            seconds = sum(pool_backlog.values())
            desired = desired_workers(seconds, pool) if pool.enabled else 0
            FLEET_DESIRED.labels(pool.id).set(desired)
            workers = await self._pool_workers(pool)
            decision = FleetDecision(pool.id, seconds, desired, len(workers))
            decision.failed = [f for pid, f in failed if pid == pool.id]
            decisions.append(decision)
            if not (pool.enabled and pool.autoscale):
                continue
            if desired > len(workers):
                price = await self._estimated_price(pool)
                for _ in range(desired - len(workers)):
                    projected = report.projected_usd + (burn_added + price) * self.config.spend_horizon_h
                    if self.budget_daily_usd and projected > self.budget_daily_usd:
                        decision.held = (
                            f"projected spend {projected:.2f} USD would exceed BUDGET_DAILY_USD "
                            f"{self.budget_daily_usd:.2f}"
                        )
                        _log.warning("fleet growth held by the daily budget", pool=pool.id, projected=projected)
                        break
                    family, variant, adapters = self._target(pool, pool_backlog)
                    result = await self.scale_up(pool, family=family, variant=variant, adapters=adapters, log=decision)
                    if result is None:
                        break
                    burn_added += price
                    decision.provisioned.append(result.external_id)
            elif len(workers) > desired:
                decision.stopped = await self._scale_down(pool, workers, len(workers) - desired, now)
        return decisions

    async def _estimated_price(self, pool: GpuPool) -> float:
        """The dearest hourly price the pool could be charged (its providers' offers for its classes),
        for the budget projection; the charged price is captured from the instance at provision."""
        prices = [self.gpu_prices.get(c, 0.0) for c in pool.gpu_classes]
        for key in pool.providers:
            fp = self._provider(key)
            if fp is None:
                continue
            try:
                offers = await fp.provider.list_offers()
            except ProviderError:
                continue
            prices += [o.price_per_hour_usd for o in offers if o.gpu_class in pool.gpu_classes]
        return max(prices, default=0.0)

    # ------------------------------------------------------------------ alerts
    async def _alert(self, scopes: Mapping[str, list[str]], report: fleet_db.SpendReport, now: datetime) -> None:
        cooldown = timedelta(seconds=self.config.alert_cooldown_s)
        for reason, orgs in scopes.items():
            for org in orgs:
                last = self._alerted.get((reason, org))
                if last is not None and now - last < cooldown:
                    continue
                self._alerted[(reason, org)] = now
                payload = {
                    "reason": reason,
                    "spent_usd": report.spent_usd,
                    "projected_usd": report.projected_usd,
                    "budget_daily_usd": self.budget_daily_usd,
                    "message": {
                        "budget_daily": "Low-priority GPU work is held: today's projected spend exceeds the daily "
                        "budget.",
                        "budget_video": "GPU work for a video is held: the video reached its budget.",
                        "budget_project": "GPU work for a project is held: the project reached its budget.",
                    }.get(reason, reason),
                }
                async with self.db.transaction() as session:
                    session.add(Notification(org_id=UUID(org), kind="budget_alert", payload=payload))
                if self.publish is not None:
                    try:
                        await self.publish(UUID(org), EventType.BUDGET_ALERT.value, payload)
                    except Exception as exc:  # events are best effort; the notification is the record
                        _log.warning("budget alert event failed", error=str(exc)[:200])
                _log.warning("budget alert", reason=reason, org_id=org)

    # ------------------------------------------------------------------ provisioning
    async def scale_up(
        self,
        pool: GpuPool,
        *,
        family: str | None = None,
        variant: str | None = None,
        adapters: list[str] | None = None,
        log: FleetDecision | None = None,
    ) -> ProvisionResult | None:
        """One more worker for `pool`: restart a stopped one, else provision with fallback."""
        family = family or str(pool.families[0])
        restarted = await self._restart_stopped(pool, family, variant)
        if restarted is not None:
            return restarted
        now = self.clock()
        for provider_key in pool.providers:
            fp = self._provider(provider_key)
            if fp is None:
                continue
            if fp.budget_daily_usd is not None:
                async with self.db.session() as session:
                    own = await fleet_db.spend_report(
                        session, now=now, horizon_h=self.config.spend_horizon_h, provider_id=fp.row_id
                    )
                if own.projected_usd >= fp.budget_daily_usd:
                    self._note(log, f"{provider_key}: provider budget_daily_usd reached")
                    continue
            for gpu_class in pool.gpu_classes:
                for region in pool.regions or ["local"]:
                    result = await self._try_provision(fp, pool, gpu_class, region, family, variant, adapters, log)
                    if result is not None:
                        return result
        return None

    def _note(self, log: FleetDecision | None, entry: str) -> None:
        if log is not None:
            log.attempts.append(entry)

    async def _try_provision(
        self,
        fp: FleetProvider,
        pool: GpuPool,
        gpu_class: str,
        region: str,
        family: str,
        variant: str | None,
        adapters: list[str] | None,
        log: FleetDecision | None,
    ) -> ProvisionResult | None:
        label = f"{fp.key}/{gpu_class}/{region}"
        if pool.min_driver_version:
            try:
                offers = await fp.provider.list_offers(gpu_class=gpu_class, region=region)
            except ProviderError as exc:
                self._note(log, f"{label}: offers failed ({str(exc)[:80]})")
                FLEET_PROVISIONS.labels(fp.key, "error").inc()
                return None
            if offers and not any(driver_at_least(o.driver_version, pool.min_driver_version) for o in offers):
                self._note(log, f"{label}: driver older than {pool.min_driver_version}")
                FLEET_PROVISIONS.labels(fp.key, "driver").inc()
                return None
        now = self.clock()
        token = secrets.token_urlsafe(32)
        digest = _hash(token)
        async with self.db.transaction() as session:
            worker = GpuWorker(
                provider_id=fp.row_id,
                provider_kind=fp.key,
                runtime_family=family,
                gpu_type=gpu_class,
                region=region,
                state="provisioning",
                pool_id=pool.id,
                variant=variant,
                provisioned_at=now,
                started_at=now,
                last_heartbeat_at=now,
            )
            session.add(worker)
            await session.flush()
            session.add(
                WorkerEnrollmentToken(
                    provider_id=fp.row_id,
                    runtime_family=family,
                    token_hash=digest,
                    expires_at=now + timedelta(seconds=self.config.provision_timeout_s),
                    worker_id=worker.id,
                )
            )
            worker_id = worker.id
        spec = ProvisionSpec(
            gpu_class=gpu_class,
            region=region,
            runtime_family=family,
            variant=variant,
            env=self._worker_env(token, fp, worker_id, family, gpu_class, region, adapters),
            spot_ok=pool.spot_ok,
        )
        try:
            instance = await fp.provider.provision(spec)
        except NoCapacityError as exc:
            await self._discard(worker_id)
            self._note(log, f"{label}: no capacity")
            FLEET_PROVISIONS.labels(fp.key, "no_capacity").inc()
            _log.info("no capacity; falling back", provider=fp.key, gpu_class=gpu_class, error=str(exc)[:200])
            return None
        except ProviderError as exc:
            await self._discard(worker_id)
            self._note(log, f"{label}: error ({str(exc)[:80]})")
            FLEET_PROVISIONS.labels(fp.key, "error").inc()
            _log.warning("provisioning failed; falling back", provider=fp.key, gpu_class=gpu_class, error=str(exc))
            return None
        async with self.db.transaction() as session:
            row = await session.get_one(GpuWorker, worker_id)
            row.external_id = instance.external_id
            row.price_per_hour_usd = fp.provider.price(instance)  # type: ignore[assignment]
            row.vram_gb = instance.vram_gb or row.vram_gb
        FLEET_PROVISIONS.labels(fp.key, "ok").inc()
        self._note(log, f"{label}: provisioned {instance.external_id}")
        _log.info(
            "worker provisioned",
            pool=pool.id,
            provider=fp.key,
            external_id=instance.external_id,
            gpu_class=gpu_class,
            variant=variant,
        )
        return ProvisionResult(worker_id, instance.external_id, fp.key, gpu_class, variant)

    def _worker_env(
        self,
        token: str,
        fp: FleetProvider,
        worker_id: UUID,
        family: str,
        gpu_class: str,
        region: str,
        adapters: list[str] | None,
    ) -> dict[str, str]:
        env = {
            "SCHEDULER_URL": self.scheduler_url,
            "APP_ENV": self.app_env,
            **self.config.worker_env,  # e.g. SCHEDULER_URL as the provider's hosts reach it
            "WORKER_TOKEN": token,
            "WORKER_NAME": f"{fp.key}-{str(worker_id)[:8]}",
            "WORKER_ID": str(worker_id),  # providers label the instance with it (recovery, orphan sweep)
            "WORKER_PROVIDER": fp.key,
            "WORKER_RUNTIME_FAMILY": family,
            "WORKER_GPU_TYPE": gpu_class,
            "WORKER_REGION": region,
        }
        if adapters:
            env["WORKER_ADAPTERS"] = ",".join(adapters)
        return env

    async def _discard(self, worker_id: UUID) -> None:
        """A provision the provider refused: nothing ran, nothing was charged."""
        async with self.db.transaction() as session:
            await session.execute(sa.delete(WorkerEnrollmentToken).where(WorkerEnrollmentToken.worker_id == worker_id))
            await session.execute(sa.delete(GpuWorker).where(GpuWorker.id == worker_id))

    async def _restart_stopped(self, pool: GpuPool, family: str, variant: str | None) -> ProvisionResult | None:
        """A stopped worker of this pool (idle_action: stop) with the same image is started again;
        its enrollment token is re-armed, since a stopped host keeps its environment."""
        if pool.idle_action != "stop":
            return None
        async with self.db.session() as session:
            candidates = list(
                (
                    await session.execute(
                        sa.select(GpuWorker)
                        .where(
                            GpuWorker.pool_id == pool.id,
                            GpuWorker.state == "stopped",
                            GpuWorker.runtime_family == family,
                            GpuWorker.external_id.is_not(None),
                            GpuWorker.variant.is_(None) if variant is None else GpuWorker.variant == variant,
                        )
                        .order_by(GpuWorker.stopped_at.desc())
                    )
                ).scalars()
            )
        for worker in candidates:
            fp = await self._worker_provider(worker)
            if fp is None or worker.external_id is None:
                continue
            try:
                if (await fp.provider.status(worker.external_id)).state != "stopped":
                    continue  # terminated or gone: nothing to restart
                instance = await fp.provider.start(worker.external_id)
            except ProviderError as exc:
                _log.info("restart failed", external_id=worker.external_id, error=str(exc)[:200])
                FLEET_PROVISIONS.labels(fp.key, "restart_error").inc()
                continue
            await self._rearm(worker.id, fp, instance)
            FLEET_PROVISIONS.labels(fp.key, "restarted").inc()
            _log.info("stopped worker restarted", pool=pool.id, external_id=worker.external_id)
            return ProvisionResult(worker.id, worker.external_id, fp.key, worker.gpu_type, worker.variant)
        return None

    async def _worker_provider(self, worker: GpuWorker) -> FleetProvider | None:
        """The configured provider that runs `worker` (its `gpu_providers` row, else its plugin key)."""
        for candidate in self.providers.values():
            if isinstance(candidate, FleetProvider) and worker.provider_id and candidate.row_id == worker.provider_id:
                return candidate
        return self._provider(worker.provider_kind) if worker.provider_kind else None

    # ------------------------------------------------------------------ watch and scale-down
    async def _watch_provisioning(self, now: datetime) -> list[tuple[str, str]]:
        """Fails provisioned workers that never registered in time or that their provider lost."""
        deadline = now - timedelta(seconds=self.config.provision_timeout_s)
        async with self.db.session() as session:
            pending = list(
                (
                    await session.execute(
                        sa.select(GpuWorker).where(GpuWorker.state == "provisioning", GpuWorker.pool_id.is_not(None))
                    )
                ).scalars()
            )
        failed: list[tuple[str, str]] = []
        for worker in pending:
            fp = await self._worker_provider(worker)
            lost = False
            if fp is not None and worker.external_id:
                try:
                    lost = (await fp.provider.status(worker.external_id)).state in ("failed", "terminated")
                except ProviderError:
                    lost = True
            timed_out = (worker.provisioned_at or worker.created_at) < deadline
            if not (lost or timed_out):
                continue
            if fp is not None and worker.external_id:
                try:
                    await fp.provider.terminate(worker.external_id)
                except ProviderError as exc:
                    _log.warning("terminate failed", external_id=worker.external_id, error=str(exc)[:200])
            async with self.db.transaction() as session:
                row = await session.get_one(GpuWorker, worker.id)
                row.state, row.stopped_at = "failed", now
                row.token_hash = None  # its instance was terminated above (see _terminate_failed)
                await fleet_db.record_fleet_cost(session, row, end=now)
            reason = "lost by its provider" if lost else "did not register in time"
            _log.warning("provisioned worker failed", external_id=worker.external_id, reason=reason)
            failed.append((worker.pool_id or "", worker.external_id or str(worker.id)))
        return failed

    async def _terminate_failed(self, now: datetime) -> list[str]:
        """Terminates the instances of fleet workers the reaper failed (no heartbeat for
        `worker_stale_s`). The reaper only marks the row, so a partitioned or crashed host kept
        billing, outside the spend report, while the autoscaler provisioned its replacement (audit
        W9). `token_hash` cleared marks an instance as terminated, as `release` does."""
        async with self.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        sa.select(GpuWorker).where(
                            GpuWorker.state == "failed",
                            GpuWorker.pool_id.is_not(None),
                            GpuWorker.external_id.is_not(None),
                            GpuWorker.token_hash.is_not(None),
                        )
                    )
                ).scalars()
            )
        terminated: list[str] = []
        for worker in rows:
            fp = await self._worker_provider(worker)
            if fp is None:
                continue
            try:
                await fp.provider.terminate(str(worker.external_id))
            except ProviderError as exc:
                _log.warning(
                    "terminate of a failed worker failed", external_id=worker.external_id, error=str(exc)[:200]
                )
                continue  # retried on the next tick
            async with self.db.transaction() as session:
                row = await session.get_one(GpuWorker, worker.id, with_for_update=True)
                row.token_hash = None
            _log.warning("terminated the instance of a failed worker", external_id=worker.external_id)
            terminated.append(str(worker.external_id))
        return terminated

    async def _idle_since(self, worker: GpuWorker) -> datetime:
        async with self.db.session() as session:
            last = (
                await session.execute(
                    sa.select(sa.func.max(JobAttempt.ended_at)).where(JobAttempt.worker_id == worker.id)
                )
            ).scalar_one()
        start = worker.registered_at or worker.provisioned_at or worker.started_at or worker.created_at
        return max(last, start) if last is not None else start

    async def _scale_down(self, pool: GpuPool, workers: list[GpuWorker], excess: int, now: datetime) -> list[str]:
        cutoff = now - timedelta(seconds=pool.idle_timeout_s)
        stopped: list[str] = []
        for worker in sorted(workers, key=lambda w: w.created_at, reverse=True):  # newest first
            if len(stopped) >= excess:
                break
            if worker.pool_id != pool.id or worker.state != "idle" or await self._idle_since(worker) > cutoff:
                continue
            if await self.release(worker.id, action=pool.idle_action) is not None:
                stopped.append(worker.external_id or str(worker.id))
        return stopped

    async def release(self, worker_id: UUID, *, action: str = "terminate") -> ProviderInstance | None:
        """Stops or terminates one worker and records its provisioned time (scale-down and the admin
        actions). `stop` keeps the instance and its disk (`stopped`, startable again); `terminate`
        destroys it (`terminated`), also for a stopped or failed worker whose instance still exists.
        Returns None when there is nothing to do or its provider refused (the error is recorded)."""
        now = self.clock()
        async with self.db.session() as session:
            worker = await session.get(GpuWorker, worker_id)
        if worker is None or worker.state == "terminated":
            return None
        live = worker.state in fleet_db.LIVE_STATES
        if action == "stop" and not live:
            return None
        fp = await self._worker_provider(worker)
        if not live and (fp is None or not worker.external_id):
            return None  # a stopped or failed worker no provider runs: nothing to terminate
        instance: ProviderInstance | None = None
        if fp is not None and worker.external_id:
            try:
                if action == "stop":
                    instance = await fp.provider.stop(worker.external_id)
                else:
                    instance = await fp.provider.terminate(worker.external_id)
            except ProviderError as exc:
                _log.warning("release failed", external_id=worker.external_id, action=action, error=str(exc)[:200])
                await self._record_error(worker_id, f"{action} failed: {str(exc)[:300]}")
                return None
        destroyed = instance is not None and action != "stop"
        async with self.db.transaction() as session:
            row = await session.get_one(GpuWorker, worker_id, with_for_update=True)
            if row.state in fleet_db.LIVE_STATES:
                await fleet_db.record_fleet_cost(session, row, end=now)
            row.state = "terminated" if destroyed else "stopped"
            row.stopped_at = row.stopped_at if row.stopped_at and not live else now
            row.token_hash = None
            row.current_task_id = None
            if destroyed:
                row.terminated_at = now
            if instance is not None:
                row.provider_status, row.provider_checked_at = _provider_status(instance), now
        _log.info("worker released", external_id=worker.external_id, action=action)
        return instance or ProviderInstance(
            provider=fp.key if fp else "unknown",
            external_id=worker.external_id or str(worker.id),
            gpu_class=worker.gpu_type,
            region=worker.region or "",
            runtime_family=worker.runtime_family,
            state="stopped",
            price_per_hour_usd=float(worker.price_per_hour_usd or 0),
        )

    # ------------------------------------------------------------------ operator actions
    async def _record_error(self, worker_id: UUID, message: str) -> None:
        async with self.db.transaction() as session:
            await session.execute(sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(last_error=message))

    async def _controlled(self, worker_id: UUID) -> tuple[GpuWorker, FleetProvider, str]:
        async with self.db.session() as session:
            worker = await session.get(GpuWorker, worker_id)
        if worker is None:
            raise LookupError("unknown worker")
        fp = await self._worker_provider(worker)
        if fp is None or not worker.external_id:
            raise FleetActionError("no configured provider runs this worker (self-managed or its provider is disabled)")
        return worker, fp, worker.external_id

    async def _rearm(self, worker_id: UUID, fp: FleetProvider, instance: ProviderInstance) -> None:
        """A started or restarted instance boots the worker again with the environment it was given at
        provision: its enrollment token is re-armed, and it is watched like a new provision."""
        now = self.clock()
        async with self.db.transaction() as session:
            row = await session.get_one(GpuWorker, worker_id, with_for_update=True)
            row.state = "provisioning"
            row.provisioned_at = now
            row.started_at = now
            row.stopped_at = None
            row.registered_at = None
            row.last_heartbeat_at = now
            row.token_hash = None
            row.current_task_id = None
            row.last_error = None
            row.price_per_hour_usd = fp.provider.price(instance) or row.price_per_hour_usd  # type: ignore[assignment]
            row.provider_status, row.provider_checked_at = _provider_status(instance), now
            await session.execute(
                sa.update(WorkerEnrollmentToken)
                .where(WorkerEnrollmentToken.worker_id == worker_id)
                .values(used_at=None, expires_at=now + timedelta(seconds=self.config.provision_timeout_s))
            )

    async def start(self, worker_id: UUID) -> ProviderInstance:
        """Starts a stopped worker's instance again (its disk and model cache are kept)."""
        worker, fp, external_id = await self._controlled(worker_id)
        if worker.state != "stopped":
            raise FleetActionError(f"only a stopped worker can be started (this one is {worker.state})")
        current = await fp.provider.status(external_id)
        if current.state == "terminated":
            await self._reconcile_one(worker, current)
            raise FleetActionError("the provider no longer has this instance: it is now marked terminated")
        instance = await fp.provider.start(external_id)
        await self._rearm(worker_id, fp, instance)
        FLEET_PROVISIONS.labels(fp.key, "restarted").inc()
        _log.info("worker started", external_id=external_id)
        return instance

    async def restart(self, worker_id: UUID) -> ProviderInstance:
        """Restarts an idle (or still provisioning) worker's container; a stopped one is started."""
        worker, fp, external_id = await self._controlled(worker_id)
        if worker.state == "stopped":
            return await self.start(worker_id)
        if worker.state not in ("idle", "provisioning"):
            raise FleetActionError(f"only an idle or provisioning worker can be restarted (this one is {worker.state})")
        instance = await fp.provider.restart(external_id)
        now = self.clock()
        async with self.db.transaction() as session:
            row = await session.get_one(GpuWorker, worker_id, with_for_update=True)
            if row.state == "idle":
                await fleet_db.record_fleet_cost(session, row, end=now)
        await self._rearm(worker_id, fp, instance)
        _log.info("worker restarted", external_id=external_id)
        return instance

    async def refresh(self, worker_id: UUID) -> dict[str, Any]:
        """Asks the provider about one worker now and reconciles the row with its answer."""
        worker, fp, external_id = await self._controlled(worker_id)
        try:
            instance = await fp.provider.status(external_id)
        except ProviderError as exc:
            await self._record_error(worker_id, f"status failed: {str(exc)[:300]}")
            raise
        return await self._reconcile_one(worker, instance)

    async def _reconcile_one(self, worker: GpuWorker, instance: ProviderInstance) -> dict[str, Any]:
        """The provider is the truth about the instance, the platform about the work:
        - gone at the provider: a live worker fails (no charge continues), a stopped one is terminated;
        - stopped outside the platform: an idle or busy worker becomes stopped (its lease is reaped);
        - failed at the provider: a registered worker fails, and the failed-worker sweep terminates it."""
        now = self.clock()
        status = _provider_status(instance)
        target: str | None = None
        error: str | None = None
        gone = instance.state == "terminated"
        if gone and worker.state in fleet_db.LIVE_STATES:
            target, error = "failed", "the provider no longer has this instance"
        elif gone and worker.state in ("stopped", "failed"):
            target = "terminated"
        elif instance.state == "stopped" and worker.state in ("idle", "busy", "draining"):
            target, error = "stopped", "stopped outside the platform (provider console or host)"
        elif instance.state == "failed" and worker.state in ("idle", "busy", "draining"):
            message = str(instance.detail.get("status_msg") or "")[:200]
            target, error = "failed", f"the provider reports the instance failed{': ' + message if message else ''}"
        async with self.db.transaction() as session:
            row = await session.get_one(GpuWorker, worker.id, with_for_update=True)
            row.provider_status, row.provider_checked_at = status, now
            if target is not None and row.state == worker.state:
                if row.state in fleet_db.LIVE_STATES:
                    await fleet_db.record_fleet_cost(session, row, end=now)
                row.state = target
                row.stopped_at = row.stopped_at or now
                row.current_task_id = None
                if gone:
                    row.terminated_at = now
                    row.token_hash = None  # nothing left to terminate
                elif target == "stopped":
                    row.token_hash = None
                if error:
                    row.last_error = error
                _log.warning("worker reconciled with its provider", worker_id=str(worker.id), state=target)
            return {"worker_id": str(row.id), "state": row.state, "provider_status": status}

    async def reconcile(self) -> int:
        """One batch of provisioned workers compared with their provider (oldest check first)."""
        async with self.db.session() as session:
            rows = list(
                (
                    await session.execute(
                        sa.select(GpuWorker)
                        .where(GpuWorker.state.in_(RECONCILED_STATES), GpuWorker.external_id.is_not(None))
                        .where(sa.or_(GpuWorker.pool_id.is_not(None), GpuWorker.provider_id.is_not(None)))
                        .order_by(GpuWorker.provider_checked_at.asc().nulls_first())
                        .limit(self.config.reconcile_batch)
                    )
                ).scalars()
            )
        checked = 0
        for worker in rows:
            fp = await self._worker_provider(worker)
            if fp is None or not worker.external_id:
                continue
            try:
                instance = await fp.provider.status(worker.external_id)
            except ProviderError as exc:
                await self._record_error(worker.id, f"status failed: {str(exc)[:300]}")
                continue
            await self._reconcile_one(worker, instance)
            checked += 1
        return checked

    async def labeled_instances(self) -> list[tuple[FleetProvider, ProviderInstance]]:
        out: list[tuple[FleetProvider, ProviderInstance]] = []
        for fp in self.providers.values():
            assert isinstance(fp, FleetProvider)
            try:
                found = await fp.provider.list_instances()
            except NotImplementedError:
                continue
            except ProviderError as exc:
                _log.info("instances unavailable", provider=fp.key, error=str(exc)[:200])
                continue
            out += [(fp, i) for i in found if i.state != "terminated"]
        return out

    async def orphans(self) -> list[dict[str, Any]]:
        """Instances labeled as the fleet's that no worker row tracks (never a non-fleet instance).
        Provisioning rows without an instance id adopt theirs first (a crash between the rental and its
        bookkeeping), so a recovered instance is never rented twice or left billing unseen."""
        instances = await self.labeled_instances()
        async with self.db.session() as session:
            tracked = {
                (r[0], r[1])
                for r in (
                    await session.execute(
                        sa.select(GpuWorker.provider_kind, GpuWorker.external_id).where(
                            GpuWorker.external_id.is_not(None), GpuWorker.state != "terminated"
                        )
                    )
                ).all()
            }
        out: list[dict[str, Any]] = []
        for fp, instance in instances:
            if (fp.key, instance.external_id) in tracked:
                continue
            if await self._adopt(fp, instance):
                continue
            out.append(
                {
                    "provider": fp.key,
                    "external_id": instance.external_id,
                    "state": instance.state,
                    "price_per_hour_usd": instance.price_per_hour_usd,
                    "label": instance.detail.get("label"),
                    "worker_id": instance.detail.get("worker_id"),
                }
            )
        return out

    async def _adopt(self, fp: FleetProvider, instance: ProviderInstance) -> bool:
        worker_id = instance.detail.get("worker_id")
        if not worker_id:
            return False
        async with self.db.transaction() as session:
            row = await session.get(GpuWorker, UUID(str(worker_id)), with_for_update=True)
            if row is None or row.external_id is not None or row.state not in ("provisioning", "failed"):
                return False
            row.external_id = instance.external_id
            row.provider_status, row.provider_checked_at = _provider_status(instance), self.clock()
            if row.state == "failed":
                row.token_hash = row.token_hash or f"adopted:{row.id}"  # the failed-worker sweep terminates it
        _log.warning("adopted an untracked instance", worker_id=str(worker_id), external_id=instance.external_id)
        return True

    async def terminate_orphan(self, provider: str, external_id: str) -> ProviderInstance:
        """Terminates an instance only if it is still an orphan (labeled as the fleet's, untracked)."""
        if not any(o["provider"] == provider and o["external_id"] == external_id for o in await self.orphans()):
            raise FleetActionError("not an orphan: the instance is tracked, unlabeled or already gone")
        fp = self._provider(provider)
        assert fp is not None
        instance = await fp.provider.terminate(external_id)
        _log.warning("orphan instance terminated", provider=provider, external_id=external_id)
        return instance
