"""Production cutover: fleet operations, worker telemetry, task progress.

gpu_workers
- state `terminated`: an instance its provider destroyed (no further charge), distinct from `stopped`
  (kept with its disk, startable again) and `failed`.
- `telemetry` (+ `telemetry_at`): what the worker last reported (GPU utilization, VRAM, temperature,
  disk, model cache). A missing value is absent, never 0.
- `provider_status` (+ `provider_checked_at`): what the provider last said about the instance.
- `model_states`: per model key, the preparation state (not_installed → downloading → verifying →
  installed → loading → warm/ready, failed) with bytes, speed and revision.
- `last_error`, `current_task_id`, `terminated_at`.

gpu_tasks
- `phase`, `progress`, `progress_message`, `progress_detail`, `progress_at`: what the worker reported
  in its heartbeats (fetching or verifying the model, loading it, generating, uploading).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 00:07:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATES = "state IN ('provisioning', 'idle', 'busy', 'draining', 'stopped', 'failed')"
NEW_STATES = "state IN ('provisioning', 'idle', 'busy', 'draining', 'stopped', 'failed', 'terminated')"
EMPTY = sa.text("'{}'::jsonb")
WHEN = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_gpu_workers_state"), "gpu_workers", type_="check")
    op.create_check_constraint(op.f("ck_gpu_workers_state"), "gpu_workers", NEW_STATES)
    op.add_column("gpu_workers", sa.Column("telemetry", JSONB(), server_default=EMPTY, nullable=False))
    op.add_column("gpu_workers", sa.Column("telemetry_at", WHEN, nullable=True))
    op.add_column("gpu_workers", sa.Column("provider_status", JSONB(), server_default=EMPTY, nullable=False))
    op.add_column("gpu_workers", sa.Column("provider_checked_at", WHEN, nullable=True))
    op.add_column("gpu_workers", sa.Column("model_states", JSONB(), server_default=EMPTY, nullable=False))
    op.add_column("gpu_workers", sa.Column("last_error", sa.String(), nullable=True))
    op.add_column("gpu_workers", sa.Column("current_task_id", sa.Uuid(), nullable=True))
    op.add_column("gpu_workers", sa.Column("terminated_at", WHEN, nullable=True))
    op.add_column("gpu_tasks", sa.Column("phase", sa.String(), nullable=True))
    op.add_column("gpu_tasks", sa.Column("progress", sa.Float(), nullable=True))
    op.add_column("gpu_tasks", sa.Column("progress_message", sa.String(), nullable=True))
    op.add_column("gpu_tasks", sa.Column("progress_detail", JSONB(), server_default=EMPTY, nullable=False))
    op.add_column("gpu_tasks", sa.Column("progress_at", WHEN, nullable=True))


def downgrade() -> None:
    for column in ("progress_at", "progress_detail", "progress_message", "progress", "phase"):
        op.drop_column("gpu_tasks", column)
    for column in (
        "terminated_at", "current_task_id", "last_error", "model_states", "provider_checked_at",
        "provider_status", "telemetry_at", "telemetry",
    ):  # fmt: skip
        op.drop_column("gpu_workers", column)
    op.execute("UPDATE gpu_workers SET state = 'stopped' WHERE state = 'terminated'")
    op.drop_constraint(op.f("ck_gpu_workers_state"), "gpu_workers", type_="check")
    op.create_check_constraint(op.f("ck_gpu_workers_state"), "gpu_workers", OLD_STATES)
