"""The scheduler's GPU task queue (§25): enqueue, lease with `SELECT … FOR UPDATE SKIP LOCKED`,
placement scoring, heartbeats, completion and the lease reaper.

This module is platform-internal: only the scheduler (and the orchestrator's dispatch activity)
calls it, never a tenant-facing route. Rows still carry `org_id`, and every write keeps it.

A task belongs to one attempt (`job_attempts`). When its lease expires the reaper marks the
attempt `failed: lease_expired` and requeues the task under a new `infra_retry` attempt with the
same seed (infrastructure retries reuse the seed, §12.5), until `max_infra_retries`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ce_db.models.assets import GpuTask, JobAttempt

__all__ = [
    "ACTIVE_STATES",
    "PlacementWeights",
    "ReapedTask",
    "WorkerView",
    "cancel_tasks",
    "enqueue",
    "finish",
    "heartbeat",
    "lease",
    "placement_score",
    "queue_depth",
    "reap",
    "retry_task",
]

ACTIVE_STATES = ("queued", "leased", "running")


@dataclass(frozen=True)
class WorkerView:
    """What a leasing worker reports (§25): the adapters it runs, models and free VRAM."""

    worker_id: UUID
    adapters: frozenset[str]
    resident_models: frozenset[str] = frozenset()
    cached_models: frozenset[str] = frozenset()
    free_vram_gb: float = 0.0
    price_per_hour_usd: float = 0.0
    sticky_models: frozenset[str] = frozenset()  # loaded within the stickiness window


@dataclass(frozen=True)
class PlacementWeights:
    priority: float = 1.0
    age_per_minute: float = 0.5
    resident: float = 20.0
    cached: float = 5.0
    sticky: float = 10.0
    load_penalty_per_gb: float = 0.1
    cost_penalty_per_usd_hour: float = 1.0
    # Fair share between organizations (Phase 14): candidates are the best `per_org_candidates`
    # queued tasks of each organization (0 = one global window), and each candidate loses
    # `fair_share` points per percent of the live leases its organization already holds.
    fair_share: float = 0.0
    per_org_candidates: int = 0


@dataclass
class ReapedTask:
    task_id: UUID
    org_id: UUID
    node_id: UUID
    old_attempt_id: UUID
    new_attempt_id: UUID | None  # None when the retry budget is spent (the task failed)
    payload: dict[str, Any] = field(default_factory=dict)


def placement_score(
    task: GpuTask, worker: WorkerView, now: datetime, weights: PlacementWeights, *, model_size_gb: float = 0.0
) -> float:
    """`priority + age_boost + resident_bonus + cached_bonus − load_penalty − cost_penalty` (§25)."""
    created = task.created_at or now
    age_min = max(0.0, (now - created).total_seconds() / 60.0)
    score = weights.priority * float(task.priority) + weights.age_per_minute * age_min
    if task.model_key in worker.resident_models:
        score += weights.resident
    elif task.model_key in worker.cached_models:
        score += weights.cached
    else:
        score -= weights.load_penalty_per_gb * model_size_gb
    if task.model_key in worker.sticky_models:
        score += weights.sticky
    score -= weights.cost_penalty_per_usd_hour * worker.price_per_hour_usd
    return score


async def enqueue(
    session: AsyncSession,
    *,
    org_id: UUID,
    node_id: UUID,
    attempt_id: UUID,
    capability: str,
    model_key: str,
    priority: int,
    vram_gb: float,
    est_seconds: float,
    constraints: dict[str, Any],
    payload: dict[str, Any],
) -> UUID:
    """Idempotent per attempt: a retried dispatch activity refreshes the payload (its new task
    token) of a live task instead of queueing a duplicate."""
    stmt = (
        insert(GpuTask)
        .values(
            org_id=org_id,
            node_id=node_id,
            attempt_id=attempt_id,
            capability=capability,
            model_key=model_key,
            priority=priority,
            vram_gb=vram_gb,
            est_seconds=est_seconds,
            constraints=constraints,
            payload=payload,
            state="queued",
        )
        .on_conflict_do_update(
            index_elements=[GpuTask.attempt_id],
            set_={"payload": payload, "updated_at": sa.func.now()},
            where=GpuTask.state.in_(ACTIVE_STATES),
        )
        .returning(GpuTask.id)
    )
    task_id = (await session.execute(stmt)).scalar_one_or_none()
    if task_id is None:  # the attempt's task already finished
        task_id = (await session.execute(sa.select(GpuTask.id).where(GpuTask.attempt_id == attempt_id))).scalar_one()
    return task_id


async def lease(
    session: AsyncSession,
    worker: WorkerView,
    *,
    now: datetime,
    lease_s: float,
    limit: int = 1,
    weights: PlacementWeights | None = None,
    window: int = 50,
    model_sizes: dict[str, float] | None = None,
) -> list[GpuTask]:
    """Leases up to `limit` queued tasks the worker can run, best placement score first.

    Without fair share the candidates are the `window` best by priority then age, across all
    organizations: an organization with a large backlog then starves the others until its backlog
    drains (measured in docs/LOAD_TEST.md). With `per_org_candidates`, every organization with
    eligible work contributes its best tasks, and `fair_share` lowers the score of organizations
    that already hold many live leases.
    """
    if not worker.adapters:
        return []
    weights = weights or PlacementWeights()
    adapter = GpuTask.constraints["adapter_id"].astext
    eligible = (
        GpuTask.state == "queued",
        GpuTask.held_reason.is_(None),  # budget holds (Phase 9)
        adapter.in_(sorted(worker.adapters)),
        GpuTask.vram_gb <= worker.free_vram_gb,
    )
    order = (GpuTask.priority.desc(), GpuTask.created_at.asc())
    if weights.per_org_candidates > 0:
        # each organization's best *unlocked* tasks, read from the partial index
        # `ix_gpu_tasks_queued_by_org` and locked inside the lateral lookup, so concurrent leasers
        # skip each other's rows instead of all contending for the same few candidates
        inner = aliased(GpuTask)
        inner_adapter = inner.constraints["adapter_id"].astext
        orgs = sa.select(inner.org_id).where(inner.state == "queued").distinct().subquery()
        best_of_org = (
            sa.select(inner.id)
            .where(
                inner.org_id == orgs.c.org_id,
                inner.state == "queued",
                inner.held_reason.is_(None),
                inner_adapter.in_(sorted(worker.adapters)),
                inner.vram_gb <= worker.free_vram_gb,
            )
            .order_by(inner.priority.desc(), inner.created_at.asc())
            .limit(weights.per_org_candidates)
            .with_for_update(skip_locked=True)
            .lateral()
        )
        best = sa.select(best_of_org.c.id).select_from(orgs.join(best_of_org, sa.true()))
        query = sa.select(GpuTask).where(GpuTask.id.in_(best))  # rows already locked by this transaction
    else:
        query = sa.select(GpuTask).where(*eligible).order_by(*order).limit(window).with_for_update(skip_locked=True)
    candidates = list((await session.execute(query)).scalars())
    share: dict[UUID, float] = {}
    if weights.fair_share > 0 and len({t.org_id for t in candidates}) > 1:
        live = dict(
            (
                await session.execute(
                    sa.select(GpuTask.org_id, sa.func.count())
                    .where(GpuTask.state.in_(("leased", "running")))
                    .group_by(GpuTask.org_id)
                )
            ).all()
        )
        total = sum(live.values())
        share = {org: 100.0 * n / total for org, n in live.items()} if total else {}
    sizes = model_sizes or {}
    candidates.sort(
        key=lambda t: (
            -(
                placement_score(t, worker, now, weights, model_size_gb=sizes.get(t.model_key, 0.0))
                - weights.fair_share * share.get(t.org_id, 0.0)
            ),
            t.created_at,
        )
    )
    chosen = candidates[:limit]
    expires = now + timedelta(seconds=lease_s)
    for task in chosen:
        task.state = "leased"
        task.lease_worker_id = worker.worker_id
        task.lease_expires_at = expires
    await session.flush()
    return chosen


async def heartbeat(
    session: AsyncSession, task_id: UUID, worker_id: UUID, *, now: datetime, lease_s: float, running: bool = True
) -> str | None:
    """Extends the lease of a task this worker holds. Returns the task state (`cancelled` tells the
    worker to stop) or None when the worker no longer holds the task (its lease was reaped)."""
    task = (
        await session.execute(sa.select(GpuTask).where(GpuTask.id == task_id).with_for_update())
    ).scalar_one_or_none()
    if task is None or task.lease_worker_id != worker_id:
        return None
    if task.state in ("leased", "running"):
        task.state = "running" if running else task.state
        task.lease_expires_at = now + timedelta(seconds=lease_s)
        await session.flush()
    return task.state


async def finish(session: AsyncSession, task_id: UUID, worker_id: UUID | None, state: str) -> GpuTask | None:
    """Moves a held task to a terminal state. Returns None when the caller no longer holds it (a
    late report after the reaper requeued it is ignored, so a task completes at most once)."""
    if state not in ("succeeded", "failed", "cancelled"):
        raise ValueError(f"{state} is not a terminal task state")
    task = (
        await session.execute(sa.select(GpuTask).where(GpuTask.id == task_id).with_for_update())
    ).scalar_one_or_none()
    if task is None or task.state not in ACTIVE_STATES:
        return None
    if worker_id is not None and task.lease_worker_id != worker_id:
        return None
    task.state = state
    task.lease_expires_at = None
    await session.flush()
    return task


async def cancel_tasks(session: AsyncSession, task_ids: Sequence[UUID]) -> list[GpuTask]:
    rows = (
        await session.execute(
            sa.select(GpuTask).where(GpuTask.id.in_(task_ids), GpuTask.state.in_(ACTIVE_STATES)).with_for_update()
        )
    ).scalars()
    out = []
    for task in rows:
        task.state = "cancelled"
        out.append(task)
    await session.flush()
    return out


async def retry_task(
    session: AsyncSession,
    task: GpuTask,
    *,
    now: datetime,
    max_infra_retries: int,
    error_class: str,
    message: str,
    min_vram_gb: float | None = None,
) -> ReapedTask:
    """Ends the task's attempt with `error_class` and requeues the task under a new `infra_retry`
    attempt with the same seed, or fails the task when the retry budget is spent.

    `min_vram_gb` (OOM escalation, §25): the requeued task only leases on workers with at least that
    much free VRAM — the scheduler passes the next larger VRAM class than the worker that ran out."""
    attempt = await session.get_one(JobAttempt, task.attempt_id)
    attempt.status = "failed"
    attempt.error_class = error_class
    attempt.error_message = message[:2000]
    attempt.ended_at = now
    retries = int((task.payload or {}).get("infra_retries", 0))
    entry = ReapedTask(task.id, task.org_id, task.node_id, task.attempt_id, None, dict(task.payload or {}))
    if retries >= max_infra_retries:
        task.state = "failed"
        task.lease_expires_at = None
    else:
        new_attempt = JobAttempt(
            org_id=task.org_id,
            node_id=task.node_id,
            attempt_no=attempt.attempt_no + 1,
            reason="infra_retry",
            adapter_id=attempt.adapter_id,
            model_id=attempt.model_id,
            model_revision=attempt.model_revision,
            translator_version=attempt.translator_version,
            seed=attempt.seed,
            started_at=now,
            status="running",
        )
        session.add(new_attempt)
        await session.flush()
        task.attempt_id = new_attempt.id
        task.state = "queued"
        task.lease_worker_id = None
        task.lease_expires_at = None
        task.payload = {**(task.payload or {}), "infra_retries": retries + 1}
        if min_vram_gb is not None and min_vram_gb > float(task.vram_gb or 0.0):
            history = list((task.payload or {}).get("oom_escalations", []))
            task.payload = {
                **task.payload,
                "oom_escalations": [*history, {"from_gb": float(task.vram_gb or 0.0), "to_gb": min_vram_gb}],
            }
            task.vram_gb = min_vram_gb
        entry.new_attempt_id = new_attempt.id
    await session.flush()
    return entry


async def reap(session: AsyncSession, *, now: datetime, max_infra_retries: int, limit: int = 100) -> list[ReapedTask]:
    """Requeues tasks whose lease expired (see the module docstring)."""
    expired = list(
        (
            await session.execute(
                sa.select(GpuTask)
                .where(GpuTask.state.in_(("leased", "running")), GpuTask.lease_expires_at < now)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    return [
        await retry_task(
            session,
            task,
            now=now,
            max_infra_retries=max_infra_retries,
            error_class="lease_expired",
            message="the worker stopped heartbeating; the task was requeued",
        )
        for task in expired
    ]


async def queue_depth(session: AsyncSession) -> dict[tuple[str, str], int]:
    rows = (
        await session.execute(
            sa.select(GpuTask.state, GpuTask.capability, sa.func.count())
            .where(GpuTask.state.in_(ACTIVE_STATES))
            .group_by(GpuTask.state, GpuTask.capability)
        )
    ).all()
    return {(str(s), str(c)): int(n) for s, c, n in rows}
