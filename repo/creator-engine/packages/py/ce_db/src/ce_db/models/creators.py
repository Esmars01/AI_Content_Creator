"""Creators and identity (§29, §17, §21). Approved versions are immutable (I3, trigger)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import CreatorKind, RecordStatus
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_uuid, check_in, jsonb, pk, tenant_fk

STATUSES = [s.value for s in RecordStatus]


def _current(table: str, version_table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "current_version_id"],
        [f"{version_table}.org_id", f"{version_table}.id"],
        use_alter=True,
        name=f"fk_{table}_current_version",
    )


def _parent(version_table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["org_id", "parent_version_id"], [f"{version_table}.org_id", f"{version_table}.id"])


class Creator(TenantMixin, Base):
    """The root identity entity (ADR 0026); the API's `creator_id`."""

    __tablename__ = "creators"
    id: Mapped[UUID] = pk()
    name: Mapped[str]
    kind: Mapped[str] = mapped_column(server_default=CreatorKind.SYNTHETIC.value)
    current_version_id: Mapped[UUID | None]
    consent_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(server_default="active")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        _current("creators", "creator_versions"),
        tenant_fk("consent_id", "consents"),
        check_in("kind", [k.value for k in CreatorKind]),
        check_in("status", ("active", "archived")),
        sa.CheckConstraint("kind <> 'digital_twin' OR consent_id IS NOT NULL", name="twin_needs_consent"),
    )


class CreatorVersion(TenantMixin, Base):
    __tablename__ = "creator_versions"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    dna: Mapped[dict[str, Any]] = jsonb()
    appearance_version_id: Mapped[UUID | None]
    voice_version_id: Mapped[UUID | None]
    default_world_ids: Mapped[list[UUID]] = array_uuid()
    default_wardrobe_version_ids: Mapped[list[UUID]] = array_uuid()
    status: Mapped[str] = mapped_column(server_default="draft")
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("creator_id", "number"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        _parent("creator_versions"),
        tenant_fk("appearance_version_id", "appearance_versions"),
        tenant_fk("voice_version_id", "voice_versions"),
        check_in("status", STATUSES),
    )


class Appearance(TenantMixin, Base):
    __tablename__ = "appearances"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str]
    current_version_id: Mapped[UUID | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        _current("appearances", "appearance_versions"),
    )


class AppearanceVersion(TenantMixin, Base):
    __tablename__ = "appearance_versions"
    id: Mapped[UUID] = pk()
    appearance_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    dna: Mapped[dict[str, Any]] = jsonb()
    canonical_face_asset_id: Mapped[UUID | None]
    identity_pack: Mapped[dict[str, Any]] = jsonb(default={})
    lora_artifacts: Mapped[dict[str, Any]] = jsonb(default={})
    age_checks: Mapped[dict[str, Any]] = jsonb(default={})
    status: Mapped[str] = mapped_column(server_default="draft")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("appearance_id", "number"),
        tenant_fk("appearance_id", "appearances", ondelete="CASCADE"),
        _parent("appearance_versions"),
        tenant_fk("canonical_face_asset_id", "assets"),
        check_in("status", STATUSES),
    )


class Voice(TenantMixin, Base):
    """`creator_id` is null for org voice presets."""

    __tablename__ = "voices"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID | None] = mapped_column(index=True)
    name: Mapped[str]
    kind: Mapped[str] = mapped_column(server_default="designed")
    current_version_id: Mapped[UUID | None]
    consent_id: Mapped[UUID | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        _current("voices", "voice_versions"),
        tenant_fk("consent_id", "consents"),
        check_in("kind", ("designed", "cloned", "preset")),
        sa.CheckConstraint("kind <> 'cloned' OR consent_id IS NOT NULL", name="cloned_needs_consent"),
    )


class VoiceVersion(TenantMixin, Base):
    __tablename__ = "voice_versions"
    id: Mapped[UUID] = pk()
    voice_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    references: Mapped[list[Any]] = jsonb(default=[])
    description: Mapped[str] = mapped_column(server_default="")
    wpm: Mapped[dict[str, Any]] = jsonb(default={})
    lexicon: Mapped[list[Any]] = jsonb(default=[])
    default_prosody: Mapped[dict[str, Any]] = jsonb(default={})
    status: Mapped[str] = mapped_column(server_default="draft")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("voice_id", "number"),
        tenant_fk("voice_id", "voices", ondelete="CASCADE"),
        _parent("voice_versions"),
        check_in("status", STATUSES),
    )


class VoiceCandidate(TenantMixin, Base):
    __tablename__ = "voice_candidates"
    id: Mapped[UUID] = pk()
    voice_id: Mapped[UUID] = mapped_column(index=True)
    job_id: Mapped[UUID | None]
    artifact_id: Mapped[UUID | None]
    description: Mapped[str] = mapped_column(server_default="")
    engine: Mapped[str]
    selected: Mapped[bool] = mapped_column(server_default=sa.false())
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("voice_id", "voices", ondelete="CASCADE"),
        tenant_fk("job_id", "generation_jobs"),
        tenant_fk("artifact_id", "artifacts"),
    )


class VoiceConditioning(TenantMixin, Base):
    """Per-engine cached conditioning (`voice.prepare`), keyed by voice version × adapter × revision."""

    __tablename__ = "voice_conditioning"
    voice_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    adapter_id: Mapped[str] = mapped_column(primary_key=True)
    revision: Mapped[str] = mapped_column(primary_key=True)
    artifact_id: Mapped[UUID]
    __table_args__ = (
        tenant_fk("voice_version_id", "voice_versions", ondelete="CASCADE"),
        tenant_fk("artifact_id", "artifacts"),
    )


class Wardrobe(TenantMixin, Base):
    __tablename__ = "wardrobes"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str]
    current_version_id: Mapped[UUID | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        _current("wardrobes", "wardrobe_versions"),
    )


class WardrobeVersion(TenantMixin, Base):
    __tablename__ = "wardrobe_versions"
    id: Mapped[UUID] = pk()
    wardrobe_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    spec: Mapped[dict[str, Any]] = jsonb()
    reference_asset_ids: Mapped[list[UUID]] = array_uuid()
    status: Mapped[str] = mapped_column(server_default="draft")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("wardrobe_id", "number"),
        tenant_fk("wardrobe_id", "wardrobes", ondelete="CASCADE"),
        _parent("wardrobe_versions"),
        check_in("status", STATUSES),
    )


class CreatorTest(TenantMixin, Base):
    __tablename__ = "creator_tests"
    id: Mapped[UUID] = pk()
    creator_version_id: Mapped[UUID] = mapped_column(index=True)
    appearance_version_id: Mapped[UUID | None]
    voice_version_id: Mapped[UUID | None]
    world_version_id: Mapped[UUID | None]
    job_id: Mapped[UUID | None]
    render_artifact_id: Mapped[UUID | None]
    scorecard: Mapped[dict[str, Any]] = jsonb(default={})
    coverage_report: Mapped[dict[str, Any]] = jsonb(default={})
    human_rating: Mapped[int | None]
    same_person_rating: Mapped[int | None]
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_version_id", "creator_versions", ondelete="CASCADE"),
        tenant_fk("appearance_version_id", "appearance_versions"),
        tenant_fk("voice_version_id", "voice_versions"),
        tenant_fk("world_version_id", "world_versions"),
        tenant_fk("job_id", "generation_jobs"),
        tenant_fk("render_artifact_id", "artifacts"),
        sa.CheckConstraint("human_rating IS NULL OR human_rating BETWEEN 1 AND 5", name="human_rating"),
        sa.CheckConstraint("same_person_rating IS NULL OR same_person_rating BETWEEN 1 AND 5", name="same_person"),
    )


class CreatorBaseline(TenantMixin, Base):
    __tablename__ = "creator_baselines"
    id: Mapped[UUID] = pk()
    creator_version_id: Mapped[UUID] = mapped_column(index=True)
    source: Mapped[str]
    stats: Mapped[dict[str, Any]] = jsonb(default={})
    window_n: Mapped[int] = mapped_column(server_default="0")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_version_id", "creator_versions", ondelete="CASCADE"),
        check_in("source", ("creator_test", "rolling")),
    )


class ConsistencyReport(TenantMixin, Base):
    __tablename__ = "consistency_reports"
    id: Mapped[UUID] = pk()
    creator_id: Mapped[UUID] = mapped_column(index=True)
    creator_version_id: Mapped[UUID]
    version_id: Mapped[UUID]
    metrics: Mapped[dict[str, Any]] = jsonb(default={})
    verdict: Mapped[str]
    deviations: Mapped[list[Any]] = jsonb(default=[])
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("creator_id", "creators", ondelete="CASCADE"),
        tenant_fk("creator_version_id", "creator_versions"),
        tenant_fk("version_id", "video_versions", ondelete="CASCADE"),
        check_in("verdict", ("in_band", "warn", "out_of_band")),
    )
