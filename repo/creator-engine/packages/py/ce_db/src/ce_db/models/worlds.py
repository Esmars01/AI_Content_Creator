"""Worlds and products (§29, §19). Approved versions are immutable (I3, trigger)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import RecordStatus
from sqlalchemy.orm import Mapped, mapped_column

from ce_db.base import Base, TenantMixin, array_uuid, check_in, jsonb, pk, tenant_fk

STATUSES = [s.value for s in RecordStatus]


class World(TenantMixin, Base):
    __tablename__ = "worlds"
    id: Mapped[UUID] = pk()
    name: Mapped[str]
    kind: Mapped[str]
    owner_creator_id: Mapped[UUID | None]
    current_version_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(server_default="active")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        tenant_fk("owner_creator_id", "creators"),
        sa.ForeignKeyConstraint(
            ["org_id", "current_version_id"],
            ["world_versions.org_id", "world_versions.id"],
            use_alter=True,
            name="fk_worlds_current_version",
        ),
        check_in("status", ("active", "archived")),
    )


class WorldVersion(TenantMixin, Base):
    """`plates` is the only store of approved plates: {camera_position_key: {time_of_day: {weather: asset_id}}}."""

    __tablename__ = "world_versions"
    id: Mapped[UUID] = pk()
    world_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    dna: Mapped[dict[str, Any]] = jsonb()
    plate_candidates: Mapped[dict[str, Any]] = jsonb(default={})
    plates: Mapped[dict[str, Any]] = jsonb(default={})
    fingerprints_artifact_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(server_default="draft")
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("world_id", "number"),
        tenant_fk("world_id", "worlds", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id", "parent_version_id"], ["world_versions.org_id", "world_versions.id"]),
        tenant_fk("fingerprints_artifact_id", "artifacts"),
        check_in("status", STATUSES),
    )


class Product(TenantMixin, Base):
    __tablename__ = "products"
    id: Mapped[UUID] = pk()
    name: Mapped[str]
    current_version_id: Mapped[UUID | None]
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.ForeignKeyConstraint(
            ["org_id", "current_version_id"],
            ["product_versions.org_id", "product_versions.id"],
            use_alter=True,
            name="fk_products_current_version",
        ),
    )


class ProductVersion(TenantMixin, Base):
    __tablename__ = "product_versions"
    id: Mapped[UUID] = pk()
    product_id: Mapped[UUID] = mapped_column(index=True)
    number: Mapped[int]
    parent_version_id: Mapped[UUID | None]
    description: Mapped[str] = mapped_column(server_default="")
    asset_ids: Mapped[list[UUID]] = array_uuid()
    brand_kit_id: Mapped[UUID | None]
    claims_allowed: Mapped[list[Any]] = jsonb(default=[])
    status: Mapped[str] = mapped_column(server_default="draft")
    __table_args__ = (
        sa.UniqueConstraint("org_id", "id"),
        sa.UniqueConstraint("product_id", "number"),
        tenant_fk("product_id", "products", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id", "parent_version_id"], ["product_versions.org_id", "product_versions.id"]),
        tenant_fk("brand_kit_id", "brand_kits"),
        check_in("status", STATUSES),
    )
