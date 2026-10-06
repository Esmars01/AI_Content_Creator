"""Models, plugins, GPU fleet, cost, flags and audit (§29). Global tables have no org_id."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import PluginStatus, PromotionBasis, RuntimeFamily, Validation
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_text, check_in, jsonb, pk, tenant_fk

MONEY = sa.Numeric(12, 4)


class Plugin(Base):
    __tablename__ = "plugins"
    id: Mapped[UUID] = pk()
    plugin_key: Mapped[str]
    version: Mapped[str]
    kind: Mapped[str]
    manifest: Mapped[dict[str, Any]] = jsonb()
    status: Mapped[str]
    validation: Mapped[str]
    runtime_family: Mapped[str]
    enabled: Mapped[bool] = mapped_column(server_default=sa.false())
    __table_args__ = (
        sa.UniqueConstraint("plugin_key", "version"),
        check_in("status", [s.value for s in PluginStatus]),
        check_in("validation", [v.value for v in Validation]),
        check_in("runtime_family", [f.value for f in RuntimeFamily]),
    )


class Model(Base):
    __tablename__ = "models"
    id: Mapped[UUID] = pk()
    plugin_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("plugins.id", ondelete="CASCADE"))
    model_key: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str] = mapped_column(server_default="")
    capabilities: Mapped[list[str]] = array_text()
    source_uri: Mapped[str]
    revision: Mapped[str]
    checksums: Mapped[dict[str, Any]] = jsonb(default={})
    size_gb: Mapped[float] = mapped_column(server_default="0")
    vram_min_gb: Mapped[float] = mapped_column(server_default="0")
    vram_rec_gb: Mapped[float] = mapped_column(server_default="0")
    languages: Mapped[dict[str, Any]] = jsonb(default={})
    license: Mapped[dict[str, Any]] = jsonb()
    dependencies: Mapped[list[Any]] = jsonb(default=[])
    obligations: Mapped[list[Any]] = jsonb(default=[])
    allowed_envs: Mapped[list[str]] = array_text()
    status: Mapped[str]
    validation: Mapped[str]
    promotion_basis: Mapped[str | None]
    quality_scores: Mapped[dict[str, Any]] = jsonb(default={})
    __table_args__ = (
        check_in("status", [s.value for s in PluginStatus]),
        check_in("validation", [v.value for v in Validation]),
        check_in("promotion_basis", [p.value for p in PromotionBasis], nullable=True),
    )


class ModelBenchmark(Base):
    __tablename__ = "model_benchmarks"
    id: Mapped[UUID] = pk()
    model_id: Mapped[UUID] = mapped_column(sa.ForeignKey("models.id", ondelete="CASCADE"), index=True)
    eval_set_version: Mapped[str]
    metrics: Mapped[dict[str, Any]] = jsonb(default={})
    behavior_profile: Mapped[dict[str, Any]] = jsonb(default={})
    human_scores: Mapped[dict[str, Any]] = jsonb(default={})
    verdict: Mapped[str]
    __table_args__ = (check_in("verdict", ("pending", "pass", "fail")),)


class BenchmarkPair(Base):
    __tablename__ = "benchmark_pairs"
    id: Mapped[UUID] = pk()
    benchmark_id: Mapped[UUID] = mapped_column(sa.ForeignKey("model_benchmarks.id", ondelete="CASCADE"), index=True)
    item_key: Mapped[str]
    a_artifact_id: Mapped[UUID]
    b_artifact_id: Mapped[UUID]


class GpuProvider(Base):
    """`kind` is text validated against registered GPUProvider plugins, never a DB enum (§25)."""

    __tablename__ = "gpu_providers"
    id: Mapped[UUID] = pk()
    kind: Mapped[str]
    name: Mapped[str] = mapped_column(unique=True)
    credentials_ref: Mapped[str | None]
    regions: Mapped[list[str]] = array_text()
    enabled: Mapped[bool] = mapped_column(server_default=sa.false())
    budget_daily_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    # Phase 9 (migration 0002): provider settings over the plugin's manifest defaults (classes, images,
    # endpoints, `allow_paid`, …). Never secrets: `credentials_ref` names where those come from.
    config: Mapped[dict[str, Any]] = jsonb(default={})


class GpuWorker(Base):
    __tablename__ = "gpu_workers"
    id: Mapped[UUID] = pk()
    provider_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_providers.id"), index=True)
    external_id: Mapped[str | None]
    runtime_family: Mapped[str]
    gpu_type: Mapped[str]
    gpu_count: Mapped[int] = mapped_column(server_default="1")
    vram_gb: Mapped[float] = mapped_column(server_default="0")
    region: Mapped[str | None]
    price_per_hour_usd: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    state: Mapped[str] = mapped_column(server_default="provisioning")
    resident_models: Mapped[list[str]] = array_text()
    cached_models: Mapped[list[str]] = array_text()
    token_hash: Mapped[str | None] = mapped_column(unique=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Phase 9 (migration 0002): the fleet's view of a worker it provisioned — the provider plugin key,
    # the pool and image variant, when the provider was asked and when the worker registered (cold
    # start = the gap).
    provider_kind: Mapped[str | None]
    pool_id: Mapped[str | None]
    variant: Mapped[str | None]
    provisioned_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    registered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        check_in("runtime_family", [f.value for f in RuntimeFamily]),
        check_in("state", ("provisioning", "idle", "busy", "draining", "stopped", "failed")),
    )


class FleetCost(Base):
    """A stopped worker's provisioned time (§25 cost ledger, Phase 9, migration 0002; append-only).

    `cost_ledger` rows attribute *busy* GPU seconds to the attempts of an org; the rest of a worker's
    provisioned time is pool overhead that belongs to no org. This platform table records both per
    worker lifetime: provisioned seconds × the price captured at provision, the busy seconds the
    attempts already billed, and the idle remainder."""

    __tablename__ = "fleet_costs"
    id: Mapped[UUID] = pk()
    worker_id: Mapped[UUID] = mapped_column(sa.ForeignKey("gpu_workers.id"), index=True)
    provider_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_providers.id"))
    provider_kind: Mapped[str | None]
    pool_id: Mapped[str | None]
    gpu_type: Mapped[str]
    period_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    provisioned_seconds: Mapped[float]
    busy_seconds: Mapped[float]
    idle_seconds: Mapped[float]
    price_per_hour_usd: Mapped[Decimal] = mapped_column(MONEY)
    provisioned_usd: Mapped[Decimal] = mapped_column(MONEY)
    idle_usd: Mapped[Decimal] = mapped_column(MONEY)


class WorkerEnrollmentToken(Base):
    __tablename__ = "worker_enrollment_tokens"
    id: Mapped[UUID] = pk()
    provider_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_providers.id"))
    runtime_family: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    # Phase 9 (migration 0002): the worker the fleet provisioned with this token; a restart of a
    # stopped worker re-arms the same token (its environment cannot change while stopped).
    worker_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_workers.id", ondelete="SET NULL"))
    __table_args__ = (check_in("runtime_family", [f.value for f in RuntimeFamily]),)


class CostLedger(TenantMixin, Base):
    """Append-only (trigger)."""

    __tablename__ = "cost_ledger"
    id: Mapped[UUID] = pk()
    project_id: Mapped[UUID | None] = mapped_column(index=True)
    video_id: Mapped[UUID | None] = mapped_column(index=True)
    version_id: Mapped[UUID | None]
    node_id: Mapped[UUID | None]
    worker_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_workers.id"))
    kind: Mapped[str]
    quantity: Mapped[float]
    unit: Mapped[str]
    unit_price_usd: Mapped[Decimal] = mapped_column(MONEY)
    amount_usd: Mapped[Decimal] = mapped_column(MONEY)
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("project_id", "projects"),
        tenant_fk("video_id", "videos"),
        tenant_fk("version_id", "video_versions"),
        tenant_fk("node_id", "execution_nodes"),
        check_in("kind", ("gpu", "llm", "storage", "egress")),
    )


class FeatureFlag(Base):
    """Global flags. Safety flags (e.g. digital_twins_enabled) are not org-overridable."""

    __tablename__ = "feature_flags"
    key: Mapped[str] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(server_default="")
    enabled: Mapped[bool] = mapped_column(server_default=sa.false())
    org_overridable: Mapped[bool] = mapped_column(server_default=sa.false())
    rules: Mapped[dict[str, Any]] = jsonb(default={})
    updated_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))


class OrgFeatureFlag(TenantMixin, Base):
    __tablename__ = "org_feature_flags"
    org_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(sa.ForeignKey("feature_flags.key", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool]


class AuditLog(Base):
    """Append-only (trigger). `org_id` is null for platform-level actions by platform admins."""

    __tablename__ = "audit_logs"
    id: Mapped[UUID] = pk()
    org_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    actor_kind: Mapped[str]
    action: Mapped[str] = mapped_column(index=True)
    target_type: Mapped[str]
    target_id: Mapped[str | None]
    before: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    after: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    ip: Mapped[str | None]
    user_agent: Mapped[str | None]
    __table_args__ = (check_in("actor_kind", ("user", "api_key", "system", "worker")),)
