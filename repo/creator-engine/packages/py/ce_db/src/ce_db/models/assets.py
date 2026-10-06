"""Assets, artifacts, cache, jobs, nodes, attempts, GPU tasks, notifications (§29, §25)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import ArtifactKind, JobKind
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_text, array_uuid, check_in, jsonb, pk, tenant_fk

MONEY = sa.Numeric(12, 4)
ASSET_KINDS = (
    "image",
    "video",
    "audio",
    "screen_recording",
    "logo",
    "product",
    "music",
    "sfx",
    "font",
    "document",
    "reference",
)


class Asset(TenantMixin, Base):
    __tablename__ = "assets"
    id: Mapped[UUID] = pk()
    project_id: Mapped[UUID | None] = mapped_column(index=True)
    kind: Mapped[str]
    storage_key: Mapped[str]
    mime: Mapped[str]
    bytes: Mapped[int] = mapped_column(sa.BigInteger)
    sha256: Mapped[str] = mapped_column(index=True)
    probe: Mapped[dict[str, Any]] = jsonb(default={})
    rights: Mapped[dict[str, Any]] = jsonb(default={})
    status: Mapped[str] = mapped_column(server_default="uploading")
    tags: Mapped[list[str]] = array_text()
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("project_id", "projects"),
        check_in("kind", ASSET_KINDS),
        check_in("status", ("uploading", "ready", "rejected")),
        sa.CheckConstraint("bytes >= 0", name="bytes"),
    )


class Artifact(TenantMixin, Base):
    """Content-addressed outputs; storage deduplicates by sha256 (§12.2)."""

    __tablename__ = "artifacts"
    id: Mapped[UUID] = pk()
    kind: Mapped[str]
    storage_key: Mapped[str]
    mime: Mapped[str]
    bytes: Mapped[int] = mapped_column(sa.BigInteger)
    sha256: Mapped[str] = mapped_column(index=True)
    media: Mapped[dict[str, Any]] = jsonb(default={})
    produced_by_node_id: Mapped[UUID | None]
    qc_state: Mapped[str] = mapped_column(server_default="unchecked")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("produced_by_node_id", "execution_nodes", cycle_name="fk_artifacts_produced_by_node"),
        check_in("kind", [k.value for k in ArtifactKind]),
        check_in("qc_state", ("unchecked", "accepted", "qc_rejected")),
    )


class CacheEntry(TenantMixin, Base):
    """Points only at non-rejected artifacts (trigger)."""

    __tablename__ = "cache_entries"
    org_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    cache_key: Mapped[str] = mapped_column(primary_key=True)
    artifact_id: Mapped[UUID]
    effective_seed: Mapped[int | None] = mapped_column(sa.BigInteger)
    __table_args__ = (tenant_fk("artifact_id", "artifacts", ondelete="CASCADE"),)


class ArtifactRef(TenantMixin, Base):
    """References that keep artifacts alive; GC removes only unreferenced artifacts (§12.10)."""

    __tablename__ = "artifact_refs"
    org_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    artifact_id: Mapped[UUID] = mapped_column(primary_key=True)
    ref_type: Mapped[str] = mapped_column(primary_key=True)
    ref_id: Mapped[str] = mapped_column(primary_key=True)
    __table_args__ = (
        tenant_fk("artifact_id", "artifacts", ondelete="CASCADE"),
        check_in(
            "ref_type",
            (
                "build_manifest",
                "take",
                "render",
                "identity_pack",
                "world_plate",
                "voice_conditioning",
                "creator_test",
                "memory_snapshot",
                "plan_report",
                "asset",
                "packaging",
                "export",
            ),
        ),
    )


class GenerationJob(TenantMixin, Base):
    __tablename__ = "generation_jobs"
    id: Mapped[UUID] = pk()
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="queued")
    priority: Mapped[int] = mapped_column(server_default="50")
    target_type: Mapped[str]
    target_id: Mapped[UUID]
    video_version_id: Mapped[UUID | None] = mapped_column(index=True)
    temporal_workflow_id: Mapped[str | None] = mapped_column(unique=True)
    parent_job_id: Mapped[UUID | None]
    input: Mapped[dict[str, Any]] = jsonb(default={})
    progress: Mapped[float] = mapped_column(server_default="0")
    cost_estimate_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    cost_actual_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    error: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    output: Mapped[dict[str, Any] | None] = jsonb(nullable=True)  # the job's result summary (Phase 12)
    requested_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("video_version_id", "video_versions", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id", "parent_job_id"], ["generation_jobs.org_id", "generation_jobs.id"]),
        check_in("kind", [k.value for k in JobKind]),
        check_in("status", ("queued", "running", "succeeded", "failed", "cancelled", "partial")),
        sa.CheckConstraint("progress >= 0 AND progress <= 1", name="progress"),
    )


class ExecutionNode(TenantMixin, Base):
    __tablename__ = "execution_nodes"
    id: Mapped[UUID] = pk()
    job_id: Mapped[UUID] = mapped_column(index=True)
    # null for studio jobs (identity packs, plates, voices; migration 0003): their nodes belong to the job
    version_id: Mapped[UUID | None] = mapped_column(index=True)
    node_key: Mapped[str]
    node_kind: Mapped[str]
    scene_key: Mapped[str | None]
    shot_key: Mapped[str | None]
    chunk_index: Mapped[int | None]
    take_index: Mapped[int | None]
    cache_key: Mapped[str | None] = mapped_column(index=True)
    route: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    effective_seed: Mapped[int | None] = mapped_column(sa.BigInteger)
    status: Mapped[str] = mapped_column(server_default="pending")
    artifact_ids: Mapped[list[UUID]] = array_uuid()
    qc_status: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(server_default="0")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("job_id", "node_key"),
        tenant_fk("job_id", "generation_jobs", ondelete="CASCADE"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        check_in(
            "status",
            ("pending", "cached", "queued", "running", "succeeded", "failed", "skipped", "needs_review", "cancelled"),
        ),
    )


class JobAttempt(TenantMixin, Base):
    __tablename__ = "job_attempts"
    id: Mapped[UUID] = pk()
    node_id: Mapped[UUID] = mapped_column(index=True)
    attempt_no: Mapped[int]
    reason: Mapped[str]
    worker_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_workers.id"))
    adapter_id: Mapped[str | None]
    model_id: Mapped[str | None]
    model_revision: Mapped[str | None]
    translator_version: Mapped[str | None]
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    gpu_seconds: Mapped[float] = mapped_column(server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    status: Mapped[str] = mapped_column(server_default="running")
    error_class: Mapped[str | None]
    error_message: Mapped[str | None]
    metrics: Mapped[dict[str, Any]] = jsonb(default={})
    logs_key: Mapped[str | None]
    seed: Mapped[int | None] = mapped_column(sa.BigInteger)
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("node_id", "attempt_no"),
        tenant_fk("node_id", "execution_nodes", ondelete="CASCADE"),
        check_in("reason", ("initial", "infra_retry", "qc_retry", "fallback")),
        check_in("status", ("running", "succeeded", "failed", "cancelled")),
        check_in("error_class", ("retryable", "fatal", "oom", "timeout", "cancelled", "lease_expired"), nullable=True),
    )


class GpuTask(TenantMixin, Base):
    """The scheduler's GPU queue entry for one attempt (§25). Leased with SELECT … FOR UPDATE SKIP LOCKED."""

    __tablename__ = "gpu_tasks"
    id: Mapped[UUID] = pk()
    node_id: Mapped[UUID] = mapped_column(index=True)
    attempt_id: Mapped[UUID] = mapped_column(unique=True)
    priority: Mapped[int] = mapped_column(server_default="50")
    capability: Mapped[str]
    model_key: Mapped[str]
    vram_gb: Mapped[float] = mapped_column(server_default="0")
    est_seconds: Mapped[float] = mapped_column(server_default="0")
    constraints: Mapped[dict[str, Any]] = jsonb(default={})
    payload: Mapped[dict[str, Any]] = jsonb(default={})
    state: Mapped[str] = mapped_column(server_default="queued")
    lease_worker_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("gpu_workers.id"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Phase 9 (migration 0002): a queued task the fleet holds back (daily budget, project or video
    # budget cap); leasing skips it until the hold is released (§25).
    held_reason: Mapped[str | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("node_id", "execution_nodes", ondelete="CASCADE"),
        tenant_fk("attempt_id", "job_attempts", ondelete="CASCADE"),
        check_in("state", ("queued", "leased", "running", "succeeded", "failed", "cancelled")),
        sa.Index("ix_gpu_tasks_queue", "state", "priority", "created_at"),
        # fair-share leasing: each organization's best queued tasks (migration 0005)
        sa.Index(
            "ix_gpu_tasks_queued_by_org",
            "org_id",
            sa.text("priority DESC"),
            "created_at",
            postgresql_where=sa.text("state = 'queued'"),
        ),
    )


class Notification(TenantMixin, Base):
    __tablename__ = "notifications"
    id: Mapped[UUID] = pk()
    user_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str]
    payload: Mapped[dict[str, Any]] = jsonb(default={})
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        check_in(
            "kind",
            ("budget_alert", "qc_flag", "job_done", "memory_conflict", "worker_state", "consistency_warning"),
        ),
    )
