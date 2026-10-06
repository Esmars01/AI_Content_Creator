"""Projects, videos, versions, manifest entries and read-only projections (§29, §12)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import VersionFlag, VersionOrigin, VersionState
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_text, array_uuid, check_in, jsonb, pk, tenant_fk

MONEY = sa.Numeric(12, 4)


class Project(TenantMixin, Base):
    __tablename__ = "projects"
    id: Mapped[UUID] = pk()
    name: Mapped[str]
    description: Mapped[str] = mapped_column(server_default="")
    default_creator_id: Mapped[UUID | None]
    brand_kit_id: Mapped[UUID | None]
    budget_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    settings: Mapped[dict[str, Any]] = jsonb(default={})
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("default_creator_id", "creators", cycle_name="fk_projects_default_creator"),
        tenant_fk("brand_kit_id", "brand_kits", cycle_name="fk_projects_brand_kit"),
    )


class Video(TenantMixin, Base):
    __tablename__ = "videos"
    id: Mapped[UUID] = pk()
    project_id: Mapped[UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(server_default="")
    mode: Mapped[str] = mapped_column(server_default="")
    current_version_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(server_default="active")
    budget_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("project_id", "projects", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["org_id", "current_version_id"],
            ["video_versions.org_id", "video_versions.id"],
            use_alter=True,
            name="fk_videos_current_version",
        ),
        check_in("status", ("active", "archived")),
    )


class VideoVersion(TenantMixin, Base):
    """Identity columns are immutable (trigger, §12.8, erratum E1); status columns stay mutable."""

    __tablename__ = "video_versions"
    id: Mapped[UUID] = pk()
    video_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    branch: Mapped[str] = mapped_column(server_default="main")
    spec: Mapped[dict[str, Any]] = jsonb()
    spec_hash: Mapped[str]
    spec_content_digest: Mapped[str] = mapped_column(index=True)
    planned_routes: Mapped[dict[str, Any]] = jsonb(default={})
    plan_report_artifact_id: Mapped[UUID | None]
    state: Mapped[str]
    origin: Mapped[str]
    coverage_summary: Mapped[dict[str, Any]] = jsonb(default={})
    flags: Mapped[list[str]] = array_text()
    cost_estimate_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    cost_actual_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    frozen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("video_id", "number"),
        tenant_fk("video_id", "videos", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id", "parent_version_id"], ["video_versions.org_id", "video_versions.id"]),
        tenant_fk("plan_report_artifact_id", "artifacts"),
        check_in("state", [s.value for s in VersionState]),
        check_in("origin", [o.value for o in VersionOrigin]),
        sa.CheckConstraint(
            "flags <@ ARRAY[{}]::text[]".format(", ".join(f"'{f.value}'" for f in VersionFlag)), name="flags"
        ),
        sa.CheckConstraint("number > 0", name="number_positive"),
    )


class BuildManifestEntry(TenantMixin, Base):
    """Insert-only (§12.3): entries are added while nodes complete and never replaced."""

    __tablename__ = "build_manifest_entries"
    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    node_key: Mapped[str] = mapped_column(primary_key=True)
    route: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    effective_seed: Mapped[int | None] = mapped_column(sa.BigInteger)
    artifact_id: Mapped[UUID | None]
    config_digests: Mapped[dict[str, Any]] = jsonb(default={})
    impl_version: Mapped[str | None]
    __table_args__ = (
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("artifact_id", "artifacts"),
    )


class Scene(TenantMixin, Base):
    """Read-only projection of the spec (I2)."""

    __tablename__ = "scenes"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    scene_key: Mapped[str]
    order: Mapped[int]
    purpose: Mapped[str]
    world_version_id: Mapped[UUID | None] = mapped_column(index=True)
    camera_position_key: Mapped[str | None]
    time_of_day: Mapped[str | None]
    weather: Mapped[str | None]
    start_s: Mapped[float | None]
    end_s: Mapped[float | None]
    status: Mapped[str] = mapped_column(server_default="planned")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("version_id", "scene_key"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("world_version_id", "world_versions"),
    )


class SceneCast(TenantMixin, Base):
    """Projection for continuity queries ("which videos used world X / outfit Y")."""

    __tablename__ = "scene_cast"
    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    scene_key: Mapped[str] = mapped_column(primary_key=True)
    character_key: Mapped[str] = mapped_column(primary_key=True)
    creator_version_id: Mapped[UUID] = mapped_column(index=True)
    appearance_version_id: Mapped[UUID | None]
    voice_version_id: Mapped[UUID | None]
    wardrobe_version_id: Mapped[UUID | None] = mapped_column(index=True)
    __table_args__ = (
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("creator_version_id", "creator_versions"),
        tenant_fk("appearance_version_id", "appearance_versions"),
        tenant_fk("voice_version_id", "voice_versions"),
        tenant_fk("wardrobe_version_id", "wardrobe_versions"),
    )


class Shot(TenantMixin, Base):
    __tablename__ = "shots"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    scene_key: Mapped[str]
    shot_key: Mapped[str]
    type: Mapped[str]
    layer: Mapped[str]
    start_s: Mapped[float | None]
    end_s: Mapped[float | None]
    status: Mapped[str] = mapped_column(server_default="planned")
    selected_take_key: Mapped[str | None]
    qc_status: Mapped[str | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("version_id", "shot_key"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        check_in("layer", ("base", "overlay")),
    )


class Take(TenantMixin, Base):
    __tablename__ = "takes"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    shot_key: Mapped[str]
    take_key: Mapped[str]
    take_index: Mapped[int]
    effective_seed: Mapped[int | None] = mapped_column(sa.BigInteger)
    artifact_ids: Mapped[list[UUID]] = array_uuid()
    observed_behavior_artifact_id: Mapped[UUID | None]
    behavior_signature: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    qc_report_id: Mapped[UUID | None]
    rank: Mapped[int | None]
    selected: Mapped[bool] = mapped_column(server_default=sa.false())
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("version_id", "shot_key", "take_key"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("observed_behavior_artifact_id", "artifacts"),
        tenant_fk("qc_report_id", "qc_reports"),
    )


class EditProposal(TenantMixin, Base):
    __tablename__ = "edit_proposals"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    job_id: Mapped[UUID | None]
    instruction: Mapped[str] = mapped_column(server_default="")
    selection: Mapped[dict[str, Any]] = jsonb(default={})
    ops: Mapped[list[Any]] = jsonb(default=[])
    patch: Mapped[list[Any]] = jsonb(default=[])
    impact: Mapped[dict[str, Any]] = jsonb(default={})
    coverage_delta: Mapped[dict[str, Any]] = jsonb(default={})
    alternatives: Mapped[list[Any]] = jsonb(default=[])
    status: Mapped[str] = mapped_column(server_default="proposing")
    result_version_id: Mapped[UUID | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("job_id", "generation_jobs"),
        sa.ForeignKeyConstraint(["org_id", "result_version_id"], ["video_versions.org_id", "video_versions.id"]),
        check_in("status", ("proposing", "proposed", "applied", "rejected", "superseded", "failed")),
    )


class DirectorRun(TenantMixin, Base):
    __tablename__ = "director_runs"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID | None] = mapped_column(index=True)
    stage: Mapped[str]
    template_version: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    input: Mapped[dict[str, Any]] = jsonb(default={})
    output: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    tokens_in: Mapped[int] = mapped_column(server_default="0")
    tokens_out: Mapped[int] = mapped_column(server_default="0")
    latency_ms: Mapped[int] = mapped_column(server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(MONEY, server_default="0")
    status: Mapped[str]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        check_in("status", ("succeeded", "repaired", "failed")),
    )


class Critique(TenantMixin, Base):
    __tablename__ = "critiques"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    render_id: Mapped[UUID | None]
    job_id: Mapped[UUID | None]
    scores: Mapped[dict[str, Any]] = jsonb(default={})
    findings: Mapped[list[Any]] = jsonb(default=[])
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("render_id", "renders"),
        tenant_fk("job_id", "generation_jobs"),
    )


class Packaging(TenantMixin, Base):
    __tablename__ = "packaging"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    platform: Mapped[str]
    title: Mapped[str] = mapped_column(server_default="")
    description: Mapped[str] = mapped_column(server_default="")
    hashtags: Mapped[list[str]] = array_text()
    cta_text: Mapped[str] = mapped_column(server_default="")
    thumbnail_artifact_ids: Mapped[list[UUID]] = array_uuid()
    status: Mapped[str] = mapped_column(server_default="draft")
    # Phase 12 (Director stage 12): candidates, the limits applied, how it was written
    job_id: Mapped[UUID | None]
    thumbnail_candidates: Mapped[list[Any]] = jsonb(default=[])
    limits: Mapped[dict[str, Any]] = jsonb(default={})
    issues: Mapped[list[Any]] = jsonb(default=[])
    generator: Mapped[dict[str, Any]] = jsonb(default={})
    approved_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("job_id", "generation_jobs"),
        check_in("status", ("draft", "approved")),
    )


class Render(TenantMixin, Base):
    __tablename__ = "renders"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    preset_id: Mapped[str]
    aspect: Mapped[str]
    is_proxy: Mapped[bool] = mapped_column(server_default=sa.false())
    artifact_id: Mapped[UUID | None]
    c2pa_manifest: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    watermark_payload_id: Mapped[str | None]
    consent_ids: Mapped[list[UUID]] = array_uuid()
    provenance_mode: Mapped[str]
    qc_report_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(server_default="pending")
    cache_key: Mapped[str | None] = mapped_column(index=True)
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("artifact_id", "artifacts"),
        tenant_fk("qc_report_id", "qc_reports"),
        check_in("provenance_mode", ("real", "mock_dev")),
        check_in("status", ("pending", "ready", "failed")),
    )


class Export(TenantMixin, Base):
    __tablename__ = "exports"
    id: Mapped[UUID] = pk()
    render_id: Mapped[UUID] = mapped_column(index=True)
    packaging_id: Mapped[UUID | None]
    platform: Mapped[str]
    disclosure_checklist: Mapped[dict[str, Any]] = jsonb(default={})
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    # Phase 12: what was exported (a packaged render download, §10.3)
    version_id: Mapped[UUID | None] = mapped_column(index=True)
    preset_id: Mapped[str | None]
    caption_ids: Mapped[list[UUID]] = array_uuid()
    metadata_doc: Mapped[dict[str, Any]] = jsonb(default={})
    metadata_artifact_id: Mapped[UUID | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("render_id", "renders", ondelete="CASCADE"),
        tenant_fk("packaging_id", "packaging"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("metadata_artifact_id", "artifacts"),
    )


class Caption(TenantMixin, Base):
    __tablename__ = "captions"
    id: Mapped[UUID] = pk()
    version_id: Mapped[UUID] = mapped_column(index=True)
    language: Mapped[str]
    style_id: Mapped[str]
    format: Mapped[str]
    artifact_id: Mapped[UUID | None]
    review_state: Mapped[str] = mapped_column(server_default="n/a")
    # Phase 12 (caption translation review)
    route: Mapped[dict[str, Any]] = jsonb(default={})  # adapter, model, revision of a translation
    reviewed_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    review_note: Mapped[str | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        tenant_fk("artifact_id", "artifacts"),
        check_in("format", ("ass", "srt", "vtt")),
        check_in("review_state", ("n/a", "pending", "approved", "rejected")),
    )
