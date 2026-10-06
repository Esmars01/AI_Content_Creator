"""Tenancy and auth tables (§29)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_text, check_in, jsonb, pk

ROLES = ("owner", "admin", "editor", "viewer", "developer")


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[UUID] = pk()
    name: Mapped[str]
    plan: Mapped[str] = mapped_column(server_default="dev")
    settings: Mapped[dict[str, Any]] = jsonb(default={})


class User(Base):
    """Users are global identities; they belong to orgs through memberships."""

    __tablename__ = "users"
    id: Mapped[UUID] = pk()
    email: Mapped[str] = mapped_column(sa.Text, unique=True)
    name: Mapped[str] = mapped_column(server_default="")
    password_hash: Mapped[str | None]
    oidc_sub: Mapped[str | None] = mapped_column(unique=True)
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())
    is_platform_admin: Mapped[bool] = mapped_column(server_default=sa.false())
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (sa.CheckConstraint("email = lower(email)", name="email_lowercase"),)


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str]
    __table_args__ = (check_in("role", ROLES),)


class Invitation(TenantMixin, Base):
    __tablename__ = "invitations"
    id: Mapped[UUID] = pk()
    email: Mapped[str]
    role: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (sa.UniqueConstraint("org_id", "id"), check_in("role", ROLES))


class Session(TenantMixin, Base):
    """Server-side session store for httpOnly cookies (ADR 0014)."""

    __tablename__ = "sessions"
    id: Mapped[UUID] = pk()
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ip: Mapped[str | None]
    user_agent: Mapped[str | None]
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (sa.UniqueConstraint("org_id", "id"),)


class ApiKey(TenantMixin, Base):
    __tablename__ = "api_keys"
    id: Mapped[UUID] = pk()
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"))
    prefix: Mapped[str] = mapped_column(unique=True)
    hash: Mapped[str]
    scopes: Mapped[list[str]] = array_text()
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (sa.UniqueConstraint("org_id", "id"),)


class IdempotencyKey(TenantMixin, Base):
    __tablename__ = "idempotency_keys"
    org_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    key: Mapped[str] = mapped_column(primary_key=True)
    request_hash: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="in_progress")
    response: Mapped[dict[str, Any] | None] = jsonb(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    __table_args__ = (check_in("status", ("in_progress", "completed")),)


class OperatorProfile(TenantMixin, Base):
    """1:1 with the org (§41 defaults until the owner answers)."""

    __tablename__ = "operator_profiles"
    org_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    jurisdiction: Mapped[str] = mapped_column(server_default="EU")
    regions_served: Mapped[list[str]] = array_text()
    revenue_band: Mapped[str] = mapped_column(server_default="lt_1m")
    mau_band: Mapped[str | None]
    offers_model_as_service: Mapped[bool] = mapped_column(server_default=sa.false())
    license_confirmations: Mapped[dict[str, Any]] = jsonb(default={})
    updated_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    __table_args__ = (check_in("revenue_band", ("lt_1m", "1m_10m", "gte_10m")),)
