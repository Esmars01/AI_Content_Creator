"""Phase 14: fair-share leasing reads each organization's best queued tasks.

A partial index on queued tasks by organization, priority and age: the lease query's lateral
lookup takes the first `placement.per_org_candidates` rows of each organization from it instead
of ranking every queued task (docs/LOAD_TEST.md). No data changes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06 00:05:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_gpu_tasks_queued_by_org",
        "gpu_tasks",
        ["org_id", sa.text("priority DESC"), "created_at"],
        postgresql_where=sa.text("state = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("ix_gpu_tasks_queued_by_org", table_name="gpu_tasks")
