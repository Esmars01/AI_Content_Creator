"""studio jobs (Phase 10): execution nodes without a video version.

Identity packs, wardrobe references, voice design and tests, and world plates dispatch model calls
through the scheduler like build nodes do; their nodes belong to the job (creator, appearance,
voice or world target) and have no video version.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05 00:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("execution_nodes", "version_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM execution_nodes WHERE version_id IS NULL")
    op.alter_column("execution_nodes", "version_id", existing_type=sa.Uuid(), nullable=False)
