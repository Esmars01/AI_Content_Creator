"""Production cutover: tenant provenance.

`organizations.is_demo` marks the dev seed's org (`ce seed dev`). Production refuses to sign into a
demo org, and `ce data purge-demo` removes demo orgs with everything they hold. The seed's org has a
fixed id (ce_testing.fixtures.ALEX.ORG_ID), so an org seeded before this flag existed is backfilled.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08 00:06:00
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_ORG_ID = "0192f0a0-0000-7000-8000-0000000000f0"  # ALEX.ORG_ID: only the dev seed uses it


def upgrade() -> None:
    op.add_column("organizations", sa.Column("is_demo", sa.Boolean(), server_default=sa.false(), nullable=False))
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()), sa.column("is_demo", sa.Boolean()))
    op.execute(organizations.update().where(organizations.c.id == UUID(SEED_ORG_ID)).values(is_demo=True))


def downgrade() -> None:
    op.drop_column("organizations", "is_demo")
