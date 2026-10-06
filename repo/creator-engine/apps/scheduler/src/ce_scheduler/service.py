"""The scheduler's worker API logic (§25): registration, leases with presigned I/O, heartbeats,
completion with output verification, failure classification, the reaper and cancellation probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import secrets
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_config.schemas import SchedulerConfig
from ce_contracts.plugins import PluginRegistry
from ce_db import ops_metrics, queue
from ce_db.execution import record_cost
from ce_db.fleet import record_fleet_cost
from ce_db.models.assets import GpuTask, JobAttempt
from ce_db.models.platform import GpuWorker, WorkerEnrollmentToken
from ce_db.models.videos import Video, VideoVersion
from ce_db.session import Database
from ce_obs import get_logger
from ce_obs.metrics import (
    COLD_START_SECONDS,
    GPU_COMPLETIONS_DEFERRED,
    GPU_LEASE_EXPIRED,
    GPU_LEASES,
    GPU_QUEUE,
    GPU_TASKS_DONE,
    LEASE_WAIT_SECONDS,
    MODEL_FETCH_SECONDS,
    MODEL_LOAD_SECONDS,
    OPS_ATTEMPTS,
    OPS_COLLECT_ERRORS,
    OPS_CONSISTENCY,
    OPS_COVERAGE,
    OPS_MEMORY_CONFLICTS,
    OPS_NOT_MEASURABLE,
    OPS_QC,
    OPS_RENDERED_MINUTES,
    OPS_SPEND,
    OPS_WINDOW,
    OPS_WORLD_QC,
    TASKS_HELD,
    WORKERS,
)
from ce_storage import StorageProvider, content_key
from ce_storage.content import file_sha256
from ce_worker.protocol import (
    CompleteBody,
    FailBody,
    HeartbeatBody,
    HeartbeatReply,
    LeaseBody,
    LeasedTask,
    LeaseReply,
    RegisterBody,
    RegisterReply,
    UploadBody,
    UploadReply,
    UploadSlot,
)
from sqlalchemy.dialects.postgresql import JSONB

from ce_scheduler.completion import ActivityCompleter, Cancelled, Gone, Unavailable

__all__ = ["AuthError", "Scheduler", "StaleTaskError", "WorkerIdentity", "hash_token"]

_log = get_logger("ce.scheduler")

INFRA_CLASSES = ("retryable", "oom", "timeout")
DEAD_STATES = ("stopped", "failed")  # a lease, heartbeat or completion never revives these


class AuthError(Exception):
    """Unknown or missing worker credentials."""


class StaleTaskError(Exception):
    """The worker no longer holds the task (its lease expired and it was requeued)."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: UUID
    runtime_family: str
    price_per_hour_usd: float
    gpu_type: str = "cpu"


def _busy_or_idle(worker_id: UUID) -> Any:
    """`busy` while the worker still holds a leased or running task, else `idle` (SQL, evaluated in
    the update): a worker running several tasks at once (`WORKER_CONCURRENCY`) stays busy until the
    last one ends, so the fleet never sees it idle (and stops it) mid-task."""
    active = (
        sa.select(GpuTask.id)
        .where(GpuTask.lease_worker_id == worker_id, GpuTask.state.in_(("leased", "running")))
        .exists()
    )
    return sa.case((active, "busy"), else_="idle")


@dataclass
class Scheduler:
    db: Database
    storage: StorageProvider
    bucket: str
    registry: PluginRegistry
    completer: ActivityCompleter
    config: SchedulerConfig
    registration_token: str
    presign_ttl_s: int = 900
    clock: Callable[[], datetime] = _utcnow
    vram_classes: tuple[float, ...] = ()  # VRAM per GPU class (config/gpu/pools.yaml), for OOM escalation
    _workers: dict[str, WorkerIdentity] = field(default_factory=dict)
    _sticky: dict[UUID, dict[str, datetime]] = field(default_factory=dict)

    @property
    def model_sizes(self) -> dict[str, float]:
        """Model key → GB (manifest `size_gb`, measured at the pins): the placement load penalty."""
        return {d.key: float(d.size_gb) for p in self.registry.plugins.values() for d in p.manifest.models}

    # ------------------------------------------------------------------ registration and auth
    def family_adapters(self, family: str) -> set[str]:
        return {p.id for p in self.registry.plugins.values() if p.manifest.runtime.family == family}

    async def register(self, body: RegisterBody, bearer: str | None) -> RegisterReply:
        """Registration with the shared registration token (dev, compose `worker-cpu`) or a one-time
        enrollment token (§30): an admin-issued one for a self-managed host, or the one the fleet
        manager passed to a worker it provisioned — that worker then takes over the `gpu_workers` row
        created at provision (cold start = registration − provision)."""
        if not bearer:
            raise AuthError("missing registration token")
        allowed = self.family_adapters(body.runtime_family)
        adapters = sorted(set(body.adapters) & allowed)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        cold_start: float | None = None
        async with self.db.transaction() as session:
            worker: GpuWorker | None = None
            provider_id: UUID | None = None
            if not hmac.compare_digest(bearer, self.registration_token):
                digest = hash_token(bearer)
                enrollment = (
                    await session.execute(
                        sa.select(WorkerEnrollmentToken)
                        .where(WorkerEnrollmentToken.token_hash == digest)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if enrollment is None or enrollment.used_at is not None or enrollment.expires_at <= now:
                    raise AuthError("invalid registration token (unknown, used or expired enrollment token)")
                if enrollment.runtime_family != body.runtime_family:
                    raise AuthError(f"the enrollment token is for the {enrollment.runtime_family} family")
                enrollment.used_at = now
                provider_id = enrollment.provider_id
                if enrollment.worker_id is not None:
                    worker = (
                        await session.execute(
                            sa.select(GpuWorker)
                            .where(GpuWorker.id == enrollment.worker_id, GpuWorker.state == "provisioning")
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if worker is None:
                        raise AuthError("the worker provisioned with this token is no longer provisioning")
            if worker is None:
                worker = GpuWorker(
                    provider_id=provider_id,
                    provider_kind=body.provider,
                    runtime_family=body.runtime_family,
                    gpu_type=body.gpu_type,
                    region=body.region,
                    external_id=body.external_id or body.name,
                    price_per_hour_usd=body.price_per_hour_usd,
                    started_at=now,
                )
                session.add(worker)
            else:  # provisioned by the fleet: keep the provider's price and class
                if worker.provisioned_at is not None:
                    cold_start = (now - worker.provisioned_at).total_seconds()
                worker.external_id = worker.external_id or body.external_id or body.name
            worker.gpu_count = body.hardware.gpu_count
            worker.vram_gb = body.hardware.vram_gb or worker.vram_gb
            worker.state = "idle"
            worker.token_hash = hash_token(token)
            worker.last_heartbeat_at = now
            worker.registered_at = now
            await session.flush()
            identity = WorkerIdentity(
                worker.id, body.runtime_family, float(worker.price_per_hour_usd or 0), worker.gpu_type
            )
        if cold_start is not None:
            COLD_START_SECONDS.labels(identity.gpu_type).observe(cold_start)
        self._workers[hash_token(token)] = identity
        _log.info("worker registered", worker_id=str(identity.worker_id), family=body.runtime_family, adapters=adapters)
        return RegisterReply(
            worker_id=str(identity.worker_id),
            token=token,
            heartbeat_s=self.config.heartbeat_s,
            lease_s=self.config.lease_s,
            long_poll_s=self.config.long_poll_s,
            adapters=adapters,
        )

    def forget(self, worker_id: UUID) -> None:
        """Drops a worker's cached identity (its token stops working on this replica)."""
        for digest in [d for d, w in self._workers.items() if w.worker_id == worker_id]:
            del self._workers[digest]

    async def authenticate(self, bearer: str | None) -> WorkerIdentity:
        if not bearer:
            raise AuthError("missing worker token")
        digest = hash_token(bearer)
        cached = self._workers.get(digest)
        if cached is not None:
            return cached
        async with self.db.session() as session:
            row = (
                await session.execute(
                    sa.select(GpuWorker).where(
                        GpuWorker.token_hash == digest, GpuWorker.state.notin_(("provisioning", "stopped", "failed"))
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise AuthError("unknown worker token")
        identity = WorkerIdentity(row.id, row.runtime_family, float(row.price_per_hour_usd), row.gpu_type)
        self._workers[digest] = identity
        return identity

    # ------------------------------------------------------------------ leases
    def _sticky_models(self, worker_id: UUID, now: datetime) -> frozenset[str]:
        window = timedelta(seconds=self.config.stickiness_s)
        return frozenset(m for m, at in self._sticky.get(worker_id, {}).items() if now - at <= window)

    async def _presign_task(self, task: GpuTask) -> LeasedTask:
        payload = task.payload or {}
        inputs = {}
        for sha in payload.get("inputs", []):
            request = await self.storage.presign_get(self.bucket, content_key(sha), ttl_s=self.presign_ttl_s)
            inputs[sha] = request.url
        slots = await self._slots(task.id, self.config.upload_slots)
        return LeasedTask(
            task_id=str(task.id),
            attempt_id=str(task.attempt_id),
            capability=task.capability,
            adapter_id=str(payload.get("adapter_id") or (task.constraints or {}).get("adapter_id")),
            model_key=task.model_key,
            seed=int(payload.get("seed", 0)),
            request=dict(payload.get("request", {})),
            inputs=inputs,
            uploads=slots,
            lease_expires_at=(task.lease_expires_at or self.clock()).isoformat(),
            labels={str(k): str(v) for k, v in dict(payload.get("labels", {})).items()},
        )

    async def _slots(self, task_id: UUID, count: int) -> list[UploadSlot]:
        out = []
        for _ in range(count):
            key = f"staging/{task_id}/{uuid.uuid4().hex}"
            request = await self.storage.presign_put(
                self.bucket, key, ttl_s=self.presign_ttl_s, content_type="application/octet-stream"
            )
            out.append(UploadSlot(key=key, url=request.url, headers=dict(request.headers), method=request.method))
        return out

    async def lease(self, worker: WorkerIdentity, body: LeaseBody) -> LeaseReply:
        allowed = self.family_adapters(worker.runtime_family)
        adapters = frozenset(set(body.adapters) & allowed)
        wait_s = self.config.long_poll_s if body.wait_s is None else min(body.wait_s, self.config.long_poll_s)
        deadline = asyncio.get_running_loop().time() + wait_s
        while True:
            now = self.clock()
            view = queue.WorkerView(
                worker_id=worker.worker_id,
                adapters=adapters,
                resident_models=frozenset(body.resident_models),
                cached_models=frozenset(body.cached_models),
                free_vram_gb=body.free_vram_gb,
                price_per_hour_usd=worker.price_per_hour_usd,
                sticky_models=self._sticky_models(worker.worker_id, now),
            )
            async with self.db.transaction() as session:
                found = (
                    await session.execute(
                        sa.select(GpuWorker.state, GpuWorker.vram_gb)
                        .where(GpuWorker.id == worker.worker_id)
                        .with_for_update()
                    )
                ).first()
                state, recorded_vram = (found[0], found[1]) if found is not None else (None, None)
                if not body.free_vram_gb and recorded_vram:
                    # A worker that reports no VRAM (e.g. provisioned without WORKER_VRAM_GB) could never
                    # lease a GPU task; the VRAM recorded for its instance at provisioning is used instead.
                    view = dataclasses.replace(view, free_vram_gb=float(recorded_vram))
                if state is None or state in DEAD_STATES:
                    self.forget(worker.worker_id)  # stopped by the fleet or failed by the reaper
                    raise AuthError("this worker was stopped; register again")
                tasks = await queue.lease(
                    session,
                    view,
                    now=now,
                    lease_s=self.config.lease_s,
                    limit=body.max_tasks,
                    weights=queue.PlacementWeights(**self.config.placement.model_dump()),
                    model_sizes=self.model_sizes,
                )
                await session.execute(
                    sa.update(GpuWorker)
                    .where(GpuWorker.id == worker.worker_id, GpuWorker.state.notin_(DEAD_STATES))
                    .values(
                        last_heartbeat_at=now,
                        state="busy" if tasks else _busy_or_idle(worker.worker_id),
                        resident_models=list(body.resident_models),
                        cached_models=list(body.cached_models),
                    )
                )
                leased = [await self._presign_task(t) for t in tasks]
            if leased:
                for row in tasks:
                    LEASE_WAIT_SECONDS.labels(row.capability).observe(max(0.0, (now - row.created_at).total_seconds()))
                for task in leased:
                    GPU_LEASES.labels(task.capability).inc()
                    self._sticky.setdefault(worker.worker_id, {})[task.model_key] = now
                return LeaseReply(tasks=leased)
            if asyncio.get_running_loop().time() >= deadline:
                return LeaseReply()
            await asyncio.sleep(self.config.poll_interval_s)

    async def heartbeat(self, worker: WorkerIdentity, body: HeartbeatBody) -> HeartbeatReply:
        now = self.clock()
        async with self.db.transaction() as session:
            state = await queue.heartbeat(
                session, UUID(body.task_id), worker.worker_id, now=now, lease_s=self.config.lease_s
            )
            await session.execute(
                sa.update(GpuWorker).where(GpuWorker.id == worker.worker_id).values(last_heartbeat_at=now)
            )
        if state is None:
            raise StaleTaskError(body.task_id)
        expires = (now + timedelta(seconds=self.config.lease_s)).isoformat()
        return HeartbeatReply(state=state, cancel=state == "cancelled", lease_expires_at=expires)

    async def uploads(self, worker: WorkerIdentity, body: UploadBody) -> UploadReply:
        await self._held(UUID(body.task_id), worker)
        return UploadReply(uploads=await self._slots(UUID(body.task_id), body.count))

    async def _held(self, task_id: UUID, worker: WorkerIdentity) -> GpuTask:
        async with self.db.session() as session:
            task = await session.get(GpuTask, task_id)
        if task is None or task.lease_worker_id != worker.worker_id or task.state not in ("leased", "running"):
            raise StaleTaskError(str(task_id))
        return task

    # ------------------------------------------------------------------ completion
    async def _verify_outputs(self, task: GpuTask, body: CompleteBody) -> list[dict[str, Any]]:
        prefix = f"staging/{task.id}/"
        moved: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="ce-sched-") as tmp:
            for output in body.outputs:
                if not output.slot_key.startswith(prefix):
                    raise ValueError(f"output {output.slot_key} is not an upload slot of this task")
                target = Path(tmp) / output.sha256
                await self.storage.download(self.bucket, output.slot_key, target)
                if file_sha256(target) != output.sha256 or target.stat().st_size != output.bytes:
                    raise ValueError(f"output {output.slot_key} does not match its sha256")
                key = content_key(output.sha256)
                if not await self.storage.exists(self.bucket, key):
                    await self.storage.copy(self.bucket, output.slot_key, key, sha256=output.sha256)
                await self.storage.delete(self.bucket, output.slot_key)
                moved.append({**output.model_dump(mode="json"), "storage_key": key})
        return moved

    async def complete(self, worker: WorkerIdentity, body: CompleteBody) -> None:
        task = await self._held(UUID(body.task_id), worker)
        try:
            outputs = await self._verify_outputs(task, body)
        except Exception as exc:
            _log.warning("output verification failed", task_id=body.task_id, error=str(exc)[:300])
            await self.fail(worker, FailBody(task_id=body.task_id, error_class="retryable", message=str(exc)))
            return
        now = self.clock()
        async with self.db.transaction() as session:
            done = await queue.finish(session, task.id, worker.worker_id, "succeeded")
            if done is None:
                raise StaleTaskError(body.task_id)
            attempt = await session.get_one(JobAttempt, done.attempt_id)
            unit_price = worker.price_per_hour_usd / 3600.0
            attempt.status = "succeeded"
            attempt.ended_at = now
            attempt.worker_id = worker.worker_id
            attempt.gpu_seconds = body.busy_seconds
            payload = dict(done.payload or {})
            # the dispatcher's work units make per-unit speed measurable (`ce_db.fleet.estimate_stats`)
            units = {"work_units": payload["work_units"]} if payload.get("work_units") else {}
            attempt.metrics = {**dict(attempt.metrics or {}), **units, **body.metrics}
            video_id = project_id = None
            if payload.get("version_id"):
                found = (
                    await session.execute(
                        sa.select(Video.id, Video.project_id)
                        .join(VideoVersion, VideoVersion.video_id == Video.id)
                        .where(VideoVersion.id == UUID(payload["version_id"]), Video.org_id == done.org_id)
                    )
                ).first()
                if found is not None:
                    video_id, project_id = found
            amount = await record_cost(
                session,
                done.org_id,
                kind="gpu",
                quantity=body.busy_seconds,
                unit="gpu_second",
                unit_price_usd=unit_price,
                version_id=UUID(payload["version_id"]) if payload.get("version_id") else None,
                video_id=video_id,
                project_id=project_id,
                node_id=done.node_id,
                worker_id=worker.worker_id,
            )
            attempt.cost_usd = amount
            await session.execute(
                sa.update(GpuWorker)
                .where(GpuWorker.id == worker.worker_id, GpuWorker.state.notin_(DEAD_STATES))
                .values(state=_busy_or_idle(worker.worker_id))
            )
            token = payload.get("task_token")
            capability = done.capability
            attempt_id = str(done.attempt_id)
        GPU_TASKS_DONE.labels(capability, "succeeded").inc()
        adapter_label = str(payload.get("adapter_id", "unknown"))
        if isinstance(body.metrics.get("load_seconds"), int | float):
            MODEL_LOAD_SECONDS.labels(adapter_label, worker.gpu_type).observe(float(body.metrics["load_seconds"]))
        if isinstance(body.metrics.get("fetch_seconds"), int | float):
            MODEL_FETCH_SECONDS.labels(adapter_label).observe(float(body.metrics["fetch_seconds"]))
        if token:
            result = {
                "status": "succeeded",
                "result": body.result,
                "outputs": outputs,
                "attempt_id": attempt_id,
                "worker_id": str(worker.worker_id),
                "busy_seconds": body.busy_seconds,
                "cost_usd": float(amount),
            }
            await self._deliver(task.id, token, {"kind": "complete", "result": result})

    def next_vram_class(self, failed_vram_gb: float) -> float | None:
        """The smallest configured VRAM class above the worker that ran out of memory (§25 OOM
        escalation); None when it already was the largest (the retry stays on that class)."""
        larger = sorted(v for v in self.vram_classes if v > failed_vram_gb)
        return larger[0] if larger else None

    async def fail(self, worker: WorkerIdentity | None, body: FailBody) -> None:
        """Infrastructure failures are retried here with the same seed (new attempt); `fatal` and
        `cancelled` fail the dispatch activity, and the build layer decides what happens next."""
        now = self.clock()
        token: str | None = None
        final: str | None = None
        async with self.db.transaction() as session:
            task = (
                await session.execute(sa.select(GpuTask).where(GpuTask.id == UUID(body.task_id)).with_for_update())
            ).scalar_one_or_none()
            if task is None or task.state not in queue.ACTIVE_STATES:
                return
            if worker is not None and task.lease_worker_id != worker.worker_id:
                raise StaleTaskError(body.task_id)
            if body.error_class in INFRA_CLASSES:
                escalate = None
                if body.error_class == "oom" and worker is not None:
                    vram = (
                        await session.execute(sa.select(GpuWorker.vram_gb).where(GpuWorker.id == worker.worker_id))
                    ).scalar()
                    escalate = self.next_vram_class(float(vram or 0.0))
                entry = await queue.retry_task(
                    session,
                    task,
                    now=now,
                    max_infra_retries=self.config.max_infra_retries,
                    error_class=body.error_class,
                    message=body.message,
                    min_vram_gb=escalate,
                )
                if entry.new_attempt_id is None:
                    final = "failed"
            else:
                attempt = await session.get_one(JobAttempt, task.attempt_id)
                attempt.status = "cancelled" if body.error_class == "cancelled" else "failed"
                attempt.error_class = body.error_class
                attempt.error_message = body.message[:2000]
                attempt.ended_at = now
                task.state = "cancelled" if body.error_class == "cancelled" else "failed"
                task.lease_expires_at = None
                final = task.state
            if worker is not None:
                await session.execute(
                    sa.update(GpuWorker)
                    .where(GpuWorker.id == worker.worker_id, GpuWorker.state.notin_(DEAD_STATES))
                    .values(state=_busy_or_idle(worker.worker_id))
                )
            token = (task.payload or {}).get("task_token")
            capability = task.capability
        GPU_TASKS_DONE.labels(capability, final or "requeued").inc()
        if final is not None and token:
            message = body.message or body.error_class
            await self._deliver(
                UUID(body.task_id), token, {"kind": "fail", "error_class": body.error_class, "message": message}
            )

    # ------------------------------------------------------------------ background work (leader)
    async def reap_once(self) -> int:
        now = self.clock()
        async with self.db.transaction() as session:
            reaped = await queue.reap(session, now=now, max_infra_retries=self.config.max_infra_retries)
        for entry in reaped:
            GPU_LEASE_EXPIRED.inc()
            _log.warning("lease expired", task_id=str(entry.task_id), requeued=entry.new_attempt_id is not None)
            if entry.new_attempt_id is None and entry.payload.get("task_token"):
                message = "infrastructure retries exhausted (lease expired)"
                await self._deliver(
                    entry.task_id,
                    entry.payload["task_token"],
                    {"kind": "fail", "error_class": "retryable", "message": message},
                )
        stale_before = now - timedelta(seconds=self.config.worker_stale_s)
        async with self.db.transaction() as session:
            stale = (
                await session.execute(
                    sa.select(GpuWorker)
                    .where(GpuWorker.state.in_(("idle", "busy")), GpuWorker.last_heartbeat_at < stale_before)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
            for worker in stale:
                worker.state, worker.stopped_at = "failed", now
                await record_fleet_cost(session, worker, end=now)  # its provisioned time ends here
        return len(reaped)

    async def _deliver(self, task_id: UUID, token: str, completion: dict[str, Any]) -> bool:
        """Reports a finished task to its dispatch activity. When Temporal cannot take it now, the
        completion is kept on the task row (`payload.completion_pending`) and redelivered by the
        leader (`deliver_pending_once`), so a Temporal outage delays a result instead of losing it
        (the task is already terminal here, so nothing else would ever report it)."""
        try:
            if completion["kind"] == "complete":
                await self.completer.complete(token, completion["result"])
            else:
                await self.completer.fail(token, completion["error_class"], completion["message"])
        except Gone as exc:
            _log.warning("activity already gone at completion", task_id=str(task_id), error=str(exc)[:200])
        except Exception as exc:  # Unavailable, or anything unexpected: keep it and retry
            _log.warning("completion deferred", task_id=str(task_id), error=str(exc)[:200])
            pending = {**completion, "since": self.clock().isoformat(), "error": str(exc)[:300]}
            async with self.db.transaction() as session:
                await session.execute(
                    sa.update(GpuTask)
                    .where(GpuTask.id == task_id)
                    .values(payload=GpuTask.payload.op("||")(sa.cast({"completion_pending": pending}, JSONB)))
                )
            GPU_COMPLETIONS_DEFERRED.inc()
            return False
        return True

    async def deliver_pending_once(self) -> int:
        """Redelivers completions deferred by `_deliver` (leader loop); returns how many went through."""
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    sa.select(GpuTask.id, GpuTask.payload)
                    .where(GpuTask.payload.has_key("completion_pending"))
                    .order_by(GpuTask.created_at)
                    .limit(200)
                )
            ).all()
        delivered = 0
        for task_id, payload in rows:
            completion, token = payload["completion_pending"], payload.get("task_token")
            try:
                if token:
                    if completion["kind"] == "complete":
                        await self.completer.complete(token, completion["result"])
                    else:
                        await self.completer.fail(token, completion["error_class"], completion["message"])
            except Gone as exc:
                _log.warning("deferred completion: activity gone", task_id=str(task_id), error=str(exc)[:200])
            except Exception as exc:
                _log.warning("deferred completion still undeliverable", task_id=str(task_id), error=str(exc)[:200])
                break  # Temporal is still unavailable: the rest waits for the next cycle
            async with self.db.transaction() as session:
                await session.execute(
                    sa.update(GpuTask)
                    .where(GpuTask.id == task_id)
                    .values(payload=GpuTask.payload.op("-")(sa.literal("completion_pending", sa.Text)))
                )
            delivered += 1
        return delivered

    async def probe_cancellations_once(self) -> int:
        """Heartbeats every live task's activity; a cancelled or vanished activity cancels the task."""
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    sa.select(GpuTask.id, GpuTask.payload, GpuTask.state).where(GpuTask.state.in_(queue.ACTIVE_STATES))
                )
            ).all()
        cancelled: list[UUID] = []
        for task_id, payload, _state in rows:
            token = (payload or {}).get("task_token")
            if not token:
                continue
            try:
                await self.completer.heartbeat(token)
            except Cancelled:
                cancelled.append(task_id)
                with contextlib.suppress(Gone, Unavailable):
                    await self.completer.report_cancellation(token)
            except Gone:
                cancelled.append(task_id)
            except Unavailable as exc:
                # Temporal is down or slow: that says nothing about any activity. Cancelling here
                # would abort every task in the queue on a Temporal restart; try again next cycle.
                _log.warning("cancellation probe skipped: Temporal unavailable", error=str(exc)[:200])
                break
        if cancelled:
            async with self.db.transaction() as session:
                await queue.cancel_tasks(session, cancelled)
                await session.execute(
                    sa.update(JobAttempt)
                    .where(JobAttempt.id.in_(sa.select(GpuTask.attempt_id).where(GpuTask.id.in_(cancelled))))
                    .where(JobAttempt.status == "running")
                    .values(status="cancelled", error_class="cancelled", ended_at=self.clock())
                )
            for task_id in cancelled:
                GPU_TASKS_DONE.labels("any", "cancelled").inc()
                _log.info("task cancelled by its workflow", task_id=str(task_id))
        return len(cancelled)

    async def refresh_metrics(self) -> None:
        async with self.db.session() as session:
            depth = await queue.queue_depth(session)
            workers = (
                await session.execute(
                    sa.select(GpuWorker.state, GpuWorker.runtime_family, sa.func.count()).group_by(
                        GpuWorker.state, GpuWorker.runtime_family
                    )
                )
            ).all()
            held = (
                await session.execute(
                    sa.select(GpuTask.held_reason, sa.func.count())
                    .where(GpuTask.state == "queued", GpuTask.held_reason.is_not(None))
                    .group_by(GpuTask.held_reason)
                )
            ).all()
        TASKS_HELD.clear()
        for reason, count in held:
            TASKS_HELD.labels(reason).set(count)
        GPU_QUEUE.clear()
        for (state, capability), count in depth.items():
            GPU_QUEUE.labels(state, capability).set(count)
        WORKERS.clear()
        for state, family, count in workers:
            WORKERS.labels(state, family).set(count)

    async def refresh_ops_metrics(self) -> ops_metrics.OpsMetrics | None:
        """The database-derived `ce_ops_*` gauges over the configured trailing window (leader only)."""
        window = self.config.ops_metrics_window_s
        try:
            async with self.db.session() as session:
                m = await ops_metrics.collect(session, since=datetime.now(UTC) - timedelta(seconds=window))
        except Exception:
            OPS_COLLECT_ERRORS.inc()
            _log.exception("ops metrics collection failed")
            return None
        OPS_WINDOW.set(window)
        for gauge, values in (
            (OPS_QC, m.qc_verdicts),
            (OPS_ATTEMPTS, m.attempts),
            (OPS_COVERAGE, m.coverage),
            (OPS_NOT_MEASURABLE, {(k,): v for k, v in m.not_measurable.items()}),
            (OPS_WORLD_QC, {(k,): v for k, v in m.world_qc.items()}),
            (OPS_CONSISTENCY, {(k,): v for k, v in m.consistency.items()}),
        ):
            gauge.clear()
            for labels, count in values.items():
                gauge.labels(*labels).set(count)
        OPS_MEMORY_CONFLICTS.set(m.memory_conflicts_open)
        OPS_SPEND.set(m.spend_usd)
        OPS_RENDERED_MINUTES.set(m.rendered_minutes)
        return m
