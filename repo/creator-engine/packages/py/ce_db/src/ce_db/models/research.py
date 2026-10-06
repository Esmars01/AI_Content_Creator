"""Research, templates, brand, consent, safety and retention (§29)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_text, array_uuid, check_in, embedding, jsonb, pk, tenant_fk


class ResearchSource(TenantMixin, Base):
    __tablename__ = "research_sources"
    id: Mapped[UUID] = pk()
    project_id: Mapped[UUID | None] = mapped_column(index=True)
    kind: Mapped[str]
    uri: Mapped[str | None]
    asset_id: Mapped[UUID | None]
    title: Mapped[str] = mapped_column(server_default="")
    content_hash: Mapped[str | None]
    trust: Mapped[str]
    fetched_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(server_default="pending")
    # Phase 12 (persistent ingestion): what was read, how, and why it failed
    mime: Mapped[str | None]
    bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    language: Mapped[str | None]
    note_text: Mapped[str | None]  # a pasted note's text (kind `note` without an asset)
    extract: Mapped[dict[str, Any]] = jsonb(default={})  # pages, characters, warnings, final URL
    error: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    fact_count: Mapped[int] = mapped_column(server_default="0")
    embedding_model: Mapped[str | None]
    job_id: Mapped[UUID | None]
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("project_id", "projects", ondelete="CASCADE"),
        tenant_fk("asset_id", "assets"),
        tenant_fk("job_id", "generation_jobs"),
        check_in("kind", ("url", "pdf", "doc", "note", "transcript", "video")),
        check_in("trust", ("user_provided", "web")),
        check_in("status", ("pending", "ingested", "failed")),
    )


class ResearchFact(TenantMixin, Base):
    __tablename__ = "research_facts"
    id: Mapped[UUID] = pk()
    source_id: Mapped[UUID] = mapped_column(index=True)
    text: Mapped[str]
    quote_span: Mapped[dict[str, Any]] = jsonb(default={})
    embedding: Mapped[Sequence[float] | None] = embedding()
    entities: Mapped[list[Any]] = jsonb(default=[])
    chunk_index: Mapped[int] = mapped_column(server_default="0")
    embedding_model: Mapped[str | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("source_id", "research_sources", ondelete="CASCADE"),
        sa.Index(
            "ix_research_facts_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Claim(TenantMixin, Base):
    """Stable across versions: unique(video_id, claim_key)."""

    __tablename__ = "claims"
    id: Mapped[UUID] = pk()
    video_id: Mapped[UUID] = mapped_column(index=True)
    claim_key: Mapped[str]
    first_version_id: Mapped[UUID]
    text: Mapped[str]
    verdict: Mapped[str] = mapped_column(server_default="uncertain")
    evidence_fact_ids: Mapped[list[UUID]] = array_uuid()
    confidence: Mapped[float] = mapped_column(server_default="0")
    override_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    override_reason: Mapped[str | None]
    # Phase 12 (the claim ledger): the latest check of the claim and where it lives
    last_version_id: Mapped[UUID | None]
    segment_key: Mapped[str | None]
    evidence: Mapped[list[Any]] = jsonb(default=[])  # quotes with source ids, titles and spans
    reasons: Mapped[list[Any]] = jsonb(default=[])
    closed_book: Mapped[bool] = mapped_column(server_default=sa.false())
    blocking: Mapped[bool] = mapped_column(server_default=sa.false())
    overridable: Mapped[bool] = mapped_column(server_default=sa.true())
    detected: Mapped[bool] = mapped_column(server_default=sa.false())
    override_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("video_id", "claim_key"),
        tenant_fk("video_id", "videos", ondelete="CASCADE"),
        tenant_fk("first_version_id", "video_versions"),
        tenant_fk("last_version_id", "video_versions"),
        check_in("verdict", ("supported", "unsupported", "uncertain", "conflicting")),
    )


class SpecTemplate(TenantMixin, Base):
    __tablename__ = "spec_templates"
    id: Mapped[UUID] = pk()
    kind: Mapped[str]
    name: Mapped[str]
    body: Mapped[dict[str, Any]] = jsonb(default={})
    composes_from: Mapped[list[UUID]] = array_uuid()
    # Phase 12: a saved template is immutable; editing it creates the next version (parent link)
    description: Mapped[str] = mapped_column(server_default="")
    version: Mapped[int] = mapped_column(server_default="1")
    parent_template_id: Mapped[UUID | None]
    source_version_id: Mapped[UUID | None]
    paths: Mapped[list[str]] = array_text()
    body_digest: Mapped[str] = mapped_column(server_default="")
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.ForeignKeyConstraint(["org_id", "parent_template_id"], ["spec_templates.org_id", "spec_templates.id"]),
        tenant_fk("source_version_id", "video_versions", ondelete="SET NULL"),
        check_in("kind", ("video", "scene", "creator_style", "camera", "caption", "brand")),
    )


class BrandKit(TenantMixin, Base):
    __tablename__ = "brand_kits"
    id: Mapped[UUID] = pk()
    name: Mapped[str]
    logo_asset_id: Mapped[UUID | None]
    colors: Mapped[dict[str, Any]] = jsonb(default={})
    fonts: Mapped[dict[str, Any]] = jsonb(default={})
    caption_style_id: Mapped[str | None]
    v1: Mapped[dict[str, Any]] = jsonb(default={})
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (sa.UniqueConstraint("org_id", "id"), tenant_fk("logo_asset_id", "assets"))


class Consent(TenantMixin, Base):
    __tablename__ = "consents"
    id: Mapped[UUID] = pk()
    subject_name: Mapped[str]
    scope: Mapped[str]
    consent_media_asset_id: Mapped[UUID | None]
    phrase: Mapped[str | None]
    statement_text: Mapped[str] = mapped_column(server_default="")
    permitted_uses: Mapped[dict[str, Any]] = jsonb(default={})
    jurisdictions: Mapped[list[str]] = array_text()
    face_match_score: Mapped[float | None]
    voice_match_score: Mapped[float | None]
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("consent_media_asset_id", "assets"),
        check_in("scope", ("face", "voice", "both")),
    )


class ProtectedPerson(Base):
    """Blocklist (§32). `org_id` null = a global row, writable only by platform admins."""

    __tablename__ = "protected_persons"
    id: Mapped[UUID] = pk()
    org_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str]
    kind: Mapped[str]
    reference_embeddings: Mapped[dict[str, Any]] = jsonb(default={})
    __table_args__ = (check_in("kind", ("public_figure", "political", "minor_protection")),)


class RetentionPolicy(TenantMixin, Base):
    __tablename__ = "retention_policies"
    org_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    data_kind: Mapped[str] = mapped_column(primary_key=True)
    retain_days: Mapped[int]
    __table_args__ = (sa.CheckConstraint("retain_days > 0", name="retain_days"),)
