"""Database-derived operations metrics (§34, Phase 14): the numbers that live in rows rather than
in one process's counters. The scheduler's leader computes them on a timer and sets gauges
(`ce_obs.metrics`), so every series has exactly one writer.

All rates are over a trailing window (`since`); labels are low-cardinality (adapter ids,
languages, dimensions, outcomes, reasons) — never org, video or creator ids:

- QC verdicts of shots by the shot's avatar adapter × language, and node attempts by reason
  (initial, infra/QC retry, fallback) × status × error class;
- coverage outcomes by adapter × dimension, and `NOT_MEASURABLE` observations by dimension (each
  dimension has one analyzer; rows do not store the analyzer's name);
- world QC verdicts;
- consistency verdicts (the out-of-band rate across creators);
- open memory conflicts;
- spend in the window and the planned minutes of the versions rendered in it (cost per output
  minute = spend ÷ minutes; measured render durations are not stored in rows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.assets import ExecutionNode, JobAttempt
from ce_db.models.behavior import BehaviorObservation, QCReport
from ce_db.models.creators import ConsistencyReport
from ce_db.models.memory import CreatorMemoryItem
from ce_db.models.platform import CostLedger
from ce_db.models.videos import Render, VideoVersion

__all__ = ["OpsMetrics", "collect"]


@dataclass
class OpsMetrics:
    qc_verdicts: dict[tuple[str, str, str], int] = field(default_factory=dict)  # adapter, language, verdict
    attempts: dict[tuple[str, str, str], int] = field(default_factory=dict)  # reason, status, error class
    coverage: dict[tuple[str, str, str], int] = field(default_factory=dict)  # adapter, dimension, outcome
    not_measurable: dict[str, int] = field(default_factory=dict)  # dimension
    observations_total: int = 0
    world_qc: dict[str, int] = field(default_factory=dict)  # verdict
    consistency: dict[str, int] = field(default_factory=dict)  # verdict
    memory_conflicts_open: int = 0
    spend_usd: float = 0.0
    rendered_minutes: float = 0.0

    @property
    def cost_per_output_minute(self) -> float | None:
        return self.spend_usd / self.rendered_minutes if self.rendered_minutes > 0 else None

    def as_dict(self) -> dict[str, Any]:
        def keyed(d: dict[Any, int]) -> dict[str, int]:
            return {"|".join(k) if isinstance(k, tuple) else str(k): v for k, v in sorted(d.items())}

        return {
            "qc_verdicts": keyed(self.qc_verdicts),
            "attempts": keyed(self.attempts),
            "coverage": keyed(self.coverage),
            "not_measurable": keyed(self.not_measurable),
            "observations_total": self.observations_total,
            "world_qc": keyed(self.world_qc),
            "consistency": keyed(self.consistency),
            "memory_conflicts_open": self.memory_conflicts_open,
            "spend_usd": round(self.spend_usd, 6),
            "rendered_minutes": round(self.rendered_minutes, 4),
            "cost_per_output_minute": self.cost_per_output_minute,
        }


async def _counts(session: AsyncSession, stmt: sa.Select[*tuple[Any, ...]]) -> dict[Any, int]:
    """Rows `(*labels, count)` as `{labels: count}`, labels as strings."""
    return {tuple(str(v) for v in row[:-1]): int(row[-1]) for row in (await session.execute(stmt)).all()}


async def _counts1(session: AsyncSession, stmt: sa.Select[*tuple[Any, ...]]) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in (await session.execute(stmt)).all()}


async def collect(session: AsyncSession, *, since: datetime) -> OpsMetrics:
    out = OpsMetrics()
    language = VideoVersion.spec["meta"]["language"].astext

    # QC verdicts of shots, labelled with the shot's avatar route and the video's language
    adapter = (
        sa.select(ExecutionNode.route["adapter_id"].astext)
        .where(
            ExecutionNode.org_id == QCReport.org_id,
            ExecutionNode.version_id == QCReport.version_id,
            ExecutionNode.node_kind == "avatar.render",
            ExecutionNode.shot_key == QCReport.checks["shot_key"].astext,
        )
        .limit(1)
        .scalar_subquery()
    )
    keys: tuple[Any, ...] = (sa.func.coalesce(adapter, "none"), sa.func.coalesce(language, "und"), QCReport.verdict)
    out.qc_verdicts = await _counts(
        session,
        sa.select(*keys, sa.func.count())
        .join(VideoVersion, sa.and_(VideoVersion.org_id == QCReport.org_id, VideoVersion.id == QCReport.version_id))
        .where(QCReport.target_type == "shot", QCReport.created_at >= since)
        .group_by(*keys),
    )

    keys = (JobAttempt.reason, JobAttempt.status, sa.func.coalesce(JobAttempt.error_class, "none"))
    out.attempts = await _counts(
        session, sa.select(*keys, sa.func.count()).where(JobAttempt.started_at >= since).group_by(*keys)
    )

    keys = (
        sa.func.coalesce(BehaviorObservation.adapter_id, "none"),
        BehaviorObservation.dimension,
        BehaviorObservation.outcome,
    )
    out.coverage = await _counts(
        session, sa.select(*keys, sa.func.count()).where(BehaviorObservation.created_at >= since).group_by(*keys)
    )
    out.observations_total = sum(out.coverage.values())
    out.not_measurable = await _counts1(
        session,
        sa.select(BehaviorObservation.dimension, sa.func.count())
        .where(BehaviorObservation.created_at >= since, BehaviorObservation.verdict == "NOT_MEASURABLE")
        .group_by(BehaviorObservation.dimension),
    )

    out.world_qc = await _counts1(
        session,
        sa.select(QCReport.verdict, sa.func.count())
        .where(
            QCReport.created_at >= since,
            QCReport.target_type == "node",
            QCReport.checks["kind"].astext.in_(("qc.world", "qc.continuity")),
        )
        .group_by(QCReport.verdict),
    )

    out.consistency = await _counts1(
        session,
        sa.select(ConsistencyReport.verdict, sa.func.count())
        .where(ConsistencyReport.created_at >= since)
        .group_by(ConsistencyReport.verdict),
    )

    out.memory_conflicts_open = int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(CreatorMemoryItem)
                .where(
                    CreatorMemoryItem.conflict_state == "unresolved",
                    CreatorMemoryItem.status.in_(("proposed", "active")),
                )
            )
        ).scalar_one()
    )

    out.spend_usd = float(
        (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(CostLedger.amount_usd), 0)).where(CostLedger.created_at >= since)
            )
        ).scalar_one()
    )
    rendered = (
        sa.select(Render.version_id)
        .where(Render.is_proxy.is_(False), Render.status == "ready", Render.created_at >= since)
        .distinct()
        .subquery()
    )
    seconds = await session.execute(
        sa.select(
            sa.func.coalesce(sa.func.sum(VideoVersion.spec["meta"]["target_duration_s"].astext.cast(sa.Float)), 0.0)
        ).where(VideoVersion.id.in_(sa.select(rendered.c.version_id)))
    )
    out.rendered_minutes = float(seconds.scalar_one()) / 60.0
    return out
