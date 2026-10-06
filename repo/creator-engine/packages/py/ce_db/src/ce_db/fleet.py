"""Fleet bookkeeping in the database (§25 fleet manager and cost ledger, Phase 9).

- **Spend.** Today's spend = every `cost_ledger` row since midnight UTC (GPU attempts, LLM tokens,
  storage and egress, all orgs) + the idle overhead of workers that stopped today (`fleet_costs`) +
  the not-yet-billed time of workers still running (provisioned seconds today × price − the busy
  seconds their attempts already billed). The projection adds the running fleet's hourly burn over
  the configured horizon.
- **Fleet costs.** When a worker stops (scale-down, admin stop, the reaper marking it failed), its
  lifetime is recorded once in `fleet_costs`: provisioned seconds × the price captured at provision,
  the busy seconds its attempts billed, and the idle remainder (pool overhead, no org).
- **Holds.** Budget holds mark queued `gpu_tasks` with a `held_reason`; leasing skips them
  (`ce_db.queue.lease`). A hold is released when its reason no longer applies.
- **Estimates.** Seconds per work unit, p50/p90 by adapter × GPU class from completed attempts
  (`estimate()` calibration, §25); callers fall back to the adapter's own estimate below `min_samples`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.assets import GpuTask, JobAttempt
from ce_db.models.platform import CostLedger, FleetCost, GpuProvider, GpuWorker
from ce_db.models.videos import Project, Video, VideoVersion

__all__ = [
    "LIVE_STATES",
    "EstimateStats",
    "SpendReport",
    "apply_holds",
    "estimate_stats",
    "record_fleet_cost",
    "spend_report",
    "start_of_day",
]

LIVE_STATES = ("provisioning", "idle", "busy", "draining")


def start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _money(value: Any) -> float:
    return float(Decimal(str(value or 0)))


async def _busy_seconds(session: AsyncSession, worker_id: UUID, since: datetime | None = None) -> float:
    query = sa.select(sa.func.coalesce(sa.func.sum(JobAttempt.gpu_seconds), 0.0)).where(
        JobAttempt.worker_id == worker_id
    )
    if since is not None:
        query = query.where(JobAttempt.ended_at >= since)
    return float((await session.execute(query)).scalar_one())


@dataclass(frozen=True)
class SpendReport:
    """USD today (UTC) and the projection the fleet manager compares with the budget."""

    ledger_usd: float
    idle_usd: float
    running_unbilled_usd: float
    burn_per_hour_usd: float
    horizon_h: float

    @property
    def spent_usd(self) -> float:
        return round(self.ledger_usd + self.idle_usd + self.running_unbilled_usd, 6)

    @property
    def projected_usd(self) -> float:
        return round(self.spent_usd + self.burn_per_hour_usd * self.horizon_h, 6)

    def as_dict(self) -> dict[str, float]:
        return {
            "ledger_usd": round(self.ledger_usd, 6),
            "idle_usd": round(self.idle_usd, 6),
            "running_unbilled_usd": round(self.running_unbilled_usd, 6),
            "spent_usd": self.spent_usd,
            "burn_per_hour_usd": round(self.burn_per_hour_usd, 6),
            "projected_usd": self.projected_usd,
        }


async def spend_report(
    session: AsyncSession, *, now: datetime, horizon_h: float = 1.0, provider_id: UUID | None = None
) -> SpendReport:
    """Today's spend, optionally for one provider's workers only (its `budget_daily_usd`)."""
    day = start_of_day(now)
    if provider_id is None:
        ledger = _money(
            (
                await session.execute(
                    sa.select(sa.func.coalesce(sa.func.sum(CostLedger.amount_usd), 0)).where(
                        CostLedger.created_at >= day
                    )
                )
            ).scalar_one()
        )
    else:
        ledger = _money(
            (
                await session.execute(
                    sa.select(sa.func.coalesce(sa.func.sum(CostLedger.amount_usd), 0))
                    .join(GpuWorker, GpuWorker.id == CostLedger.worker_id)
                    .where(CostLedger.created_at >= day, GpuWorker.provider_id == provider_id)
                )
            ).scalar_one()
        )
    idle_query = sa.select(sa.func.coalesce(sa.func.sum(FleetCost.idle_usd), 0)).where(FleetCost.period_end >= day)
    if provider_id is not None:
        idle_query = idle_query.where(FleetCost.provider_id == provider_id)
    idle = _money((await session.execute(idle_query)).scalar_one())
    workers_query = sa.select(GpuWorker).where(GpuWorker.state.in_(LIVE_STATES))
    if provider_id is not None:
        workers_query = workers_query.where(GpuWorker.provider_id == provider_id)
    unbilled = burn = 0.0
    for worker in (await session.execute(workers_query)).scalars():
        price = _money(worker.price_per_hour_usd)
        if price <= 0:
            continue
        burn += price
        started = worker.provisioned_at or worker.started_at or worker.created_at
        since = max(started, day)
        seconds_today = max(0.0, (now - since).total_seconds())
        busy_today = await _busy_seconds(session, worker.id, since=since)
        unbilled += max(0.0, seconds_today - busy_today) * price / 3600.0
    return SpendReport(ledger, idle, unbilled, burn, horizon_h)


async def record_fleet_cost(
    session: AsyncSession, worker: GpuWorker, *, end: datetime, pool_id: str | None = None
) -> FleetCost | None:
    """Records a stopped worker's lifetime once (idempotent per worker and provisioned period: a
    stopped worker the fleet restarts begins a new period)."""
    start = worker.provisioned_at or worker.started_at or worker.created_at
    existing = (
        await session.execute(
            sa.select(FleetCost.id).where(FleetCost.worker_id == worker.id, FleetCost.period_start == start)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    provisioned = max(0.0, (end - start).total_seconds())
    busy = min(provisioned, await _busy_seconds(session, worker.id, since=start))
    idle = max(0.0, provisioned - busy)
    price = Decimal(str(worker.price_per_hour_usd or 0))
    kind = worker.provider_kind
    if kind is None and worker.provider_id is not None:
        kind = (
            await session.execute(sa.select(GpuProvider.kind).where(GpuProvider.id == worker.provider_id))
        ).scalar_one_or_none()
    row = FleetCost(
        worker_id=worker.id,
        provider_id=worker.provider_id,
        provider_kind=kind,
        pool_id=pool_id or worker.pool_id,
        gpu_type=worker.gpu_type,
        period_start=start,
        period_end=end,
        provisioned_seconds=round(provisioned, 3),
        busy_seconds=round(busy, 3),
        idle_seconds=round(idle, 3),
        price_per_hour_usd=price,
        provisioned_usd=(Decimal(str(provisioned)) * price / 3600).quantize(Decimal("0.000001")),
        idle_usd=(Decimal(str(idle)) * price / 3600).quantize(Decimal("0.000001")),
    )
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True)
class HoldChange:
    held: int
    released: int
    held_scopes: dict[str, list[str]]  # reason → org ids with newly held tasks


async def _over_cap(session: AsyncSession, scope: str) -> dict[UUID, tuple[UUID, float, float]]:
    """Videos or projects whose ledger spend reached their `budget_usd`: id → (org, spent, cap)."""
    owner: Any = Video if scope == "video" else Project
    query = (
        sa.select(owner.id, owner.org_id, owner.budget_usd, sa.func.coalesce(sa.func.sum(CostLedger.amount_usd), 0))
        .select_from(CostLedger)
        .join(VideoVersion, VideoVersion.id == CostLedger.version_id)
        .join(Video, Video.id == VideoVersion.video_id)
    )
    if scope == "project":
        query = query.join(Project, Project.id == Video.project_id)
    query = query.where(owner.budget_usd.is_not(None)).group_by(owner.id, owner.org_id, owner.budget_usd)
    out: dict[UUID, tuple[UUID, float, float]] = {}
    for item_id, org_id, cap, spent in (await session.execute(query)).all():
        if _money(spent) >= _money(cap):
            out[item_id] = (org_id, _money(spent), _money(cap))
    return out


async def apply_holds(session: AsyncSession, *, daily_exceeded: bool, low_priority_max: int) -> HoldChange:
    """Holds and releases queued tasks:
    - `budget_daily`: low-priority tasks (priority ≤ `low_priority_max`) while the projected daily spend
      exceeds `BUDGET_DAILY_USD`;
    - `budget_video` / `budget_project`: every queued task of a video or project whose spend reached its
      `budget_usd` (a cap is a cap, whatever the priority)."""
    videos = await _over_cap(session, "video")
    projects = await _over_cap(session, "project")
    rows = (
        await session.execute(
            sa.select(GpuTask, Video.id, Video.project_id)
            .outerjoin(VideoVersion, VideoVersion.id == sa.cast(GpuTask.payload["version_id"].astext, sa.Uuid))
            .outerjoin(Video, Video.id == VideoVersion.video_id)
            .where(GpuTask.state == "queued")
            .with_for_update(of=GpuTask, skip_locked=True)
        )
    ).all()
    held = released = 0
    scopes: dict[str, list[str]] = {}
    for task, video_id, project_id in rows:
        reason = None
        if video_id is not None and video_id in videos:
            reason = "budget_video"
        elif project_id is not None and project_id in projects:
            reason = "budget_project"
        elif daily_exceeded and task.priority <= low_priority_max:
            reason = "budget_daily"
        if reason == task.held_reason:
            continue
        if reason is None:
            released += 1
        else:
            held += 1
            scopes.setdefault(reason, [])
            if str(task.org_id) not in scopes[reason]:
                scopes[reason].append(str(task.org_id))
        task.held_reason = reason
    await session.flush()
    return HoldChange(held, released, scopes)


@dataclass(frozen=True)
class EstimateStats:
    adapter_id: str
    gpu_type: str
    n: int
    p50_seconds_per_unit: float
    p90_seconds_per_unit: float


async def estimate_stats(
    session: AsyncSession, *, adapters: Iterable[str] | None = None, min_samples: int = 5
) -> list[EstimateStats]:
    """p50/p90 seconds per work unit by adapter × GPU class over succeeded attempts that reported
    `metrics.work_units` (the worker does since Phase 9)."""
    units = JobAttempt.metrics["work_units"].astext.cast(sa.Float)
    per_unit = JobAttempt.gpu_seconds / sa.func.nullif(units, 0)
    query = (
        sa.select(
            JobAttempt.adapter_id,
            GpuWorker.gpu_type,
            sa.func.count(),
            sa.func.percentile_cont(0.5).within_group(per_unit),
            sa.func.percentile_cont(0.9).within_group(per_unit),
        )
        .join(GpuWorker, GpuWorker.id == JobAttempt.worker_id)
        .where(JobAttempt.status == "succeeded", JobAttempt.metrics.has_key("work_units"), units > 0)
        .group_by(JobAttempt.adapter_id, GpuWorker.gpu_type)
    )
    if adapters is not None:
        query = query.where(JobAttempt.adapter_id.in_(list(adapters)))
    out = []
    for adapter, gpu_type, n, p50, p90 in (await session.execute(query)).all():
        if adapter is None or int(n) < min_samples:
            continue
        out.append(EstimateStats(str(adapter), str(gpu_type), int(n), round(float(p50), 4), round(float(p90), 4)))
    return out


def stale_before(now: datetime, seconds: float) -> datetime:
    return now - timedelta(seconds=seconds)
