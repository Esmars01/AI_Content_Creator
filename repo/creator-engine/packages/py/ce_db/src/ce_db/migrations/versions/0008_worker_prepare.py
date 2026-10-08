"""Production cutover: model preparation requests.

`gpu_workers.prepare_request`: an operator's request that a worker fetch, verify and (warm) load
models ahead of the first task — `{id, models, warm, requested_by, requested_at, cancel}`. The
scheduler relays it to the worker in its lease or status reply; the worker reports progress in
`model_states`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08 00:08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gpu_workers", sa.Column("prepare_request", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("gpu_workers", "prepare_request")
