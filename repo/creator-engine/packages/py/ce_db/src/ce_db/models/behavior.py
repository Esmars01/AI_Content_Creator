"""Behavior observations, measured profiles, QC reports and human ratings (§29, §16)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import CoverageLevel, ObservationVerdict, Outcome, ProfileSource, RealizationMethod
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, check_in, jsonb, pk, tenant_fk


class BehaviorObservation(TenantMixin, Base):
    """Append-only (trigger): one row per judged item, per take and at viewer level (§16.6)."""

    __tablename__ = "behavior_observations"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    take_id: Mapped[UUID | None]
    level_scope: Mapped[str]
    item_ref: Mapped[str]
    character_key: Mapped[str]
    dimension: Mapped[str] = mapped_column(index=True)
    requested: Mapped[dict[str, Any]] = jsonb(default={})
    level: Mapped[str]
    method: Mapped[str]
    approximation_executed: Mapped[bool | None]
    verdict: Mapped[str]
    outcome: Mapped[str]
    measures: Mapped[dict[str, Any]] = jsonb(default={})
    confidence: Mapped[float] = mapped_column(server_default="0")
    adapter_id: Mapped[str | None]
    translator_version: Mapped[str | None]
    model_revision: Mapped[str | None]
    language: Mapped[str | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("take_id", "takes"),
        check_in("level_scope", ("take", "viewer")),
        check_in("level", [v.value for v in CoverageLevel]),
        check_in("method", [m.value for m in RealizationMethod]),
        check_in("verdict", [v.value for v in ObservationVerdict]),
        check_in("outcome", [o.value for o in Outcome]),
        sa.CheckConstraint("level_scope = 'viewer' OR take_id IS NOT NULL", name="take_rows_have_take"),
    )


class ModelBehaviorProfile(Base):
    """Global measured behavior profile per adapter × translator × revision × dimension × language."""

    __tablename__ = "model_behavior_profiles"
    id: Mapped[UUID] = pk()
    model_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("models.id", ondelete="CASCADE"))
    adapter_id: Mapped[str]
    translator_version: Mapped[str]
    revision: Mapped[str]
    dimension: Mapped[str]
    language: Mapped[str] = mapped_column(server_default="und")
    source: Mapped[str]
    declared: Mapped[dict[str, Any]] = jsonb(default={})
    measured: Mapped[dict[str, Any]] = jsonb(default={})
    knob_calibration: Mapped[dict[str, Any]] = jsonb(default={})
    __table_args__ = (
        sa.UniqueConstraint("adapter_id", "translator_version", "revision", "dimension", "language", "source"),
        check_in("source", [s.value for s in ProfileSource]),
    )


class QCReport(TenantMixin, Base):
    __tablename__ = "qc_reports"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID | None] = mapped_column(index=True)
    target_type: Mapped[str]
    target_id: Mapped[UUID]
    checks: Mapped[dict[str, Any]] = jsonb(default={})
    verdict: Mapped[str]
    thresholds_digest: Mapped[str]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        check_in("target_type", ("take", "node", "shot", "render", "consistency", "creator_test", "world_plate")),
        check_in("verdict", ("pass", "warn", "fail")),
    )


class HumanRating(TenantMixin, Base):
    __tablename__ = "human_ratings"
    id: Mapped[UUID] = pk()
    target_type: Mapped[str]
    target_id: Mapped[UUID] = mapped_column(index=True)
    question_key: Mapped[str]
    rating: Mapped[dict[str, Any]] = jsonb()
    rater_user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        check_in("target_type", ("take", "render", "creator_test", "consistency", "benchmark_pair")),
    )
