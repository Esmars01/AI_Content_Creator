"""Fair share between organizations in leasing (Phase 14, docs/LOAD_TEST.md): one global window by
priority and age lets an organization's backlog starve the others; per-organization candidates
plus the live-lease penalty interleave them."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from ce_config.schemas import PlacementConfig
from ce_db import queue
from ce_db.models.assets import ExecutionNode, GenerationJob, GpuTask, JobAttempt
from ce_db.models.platform import GpuWorker
from ce_db.models.tenancy import Organization
from ce_db.session import Database
from ce_testing.database import TestDatabase

pytestmark = pytest.mark.infra
BACKLOG, LATE = 80, 5


@pytest.fixture(scope="module")
def fair_db() -> Iterator[TestDatabase]:
    database = TestDatabase(prefix="ce_fair")
    asyncio.run(database.create())
    yield database
    asyncio.run(database.drop())


async def _enqueue(session: Any, org: uuid.UUID, n: int, created: datetime) -> None:
    job = GenerationJob(org_id=org, kind="generate", target_type="video_version", target_id=uuid.uuid4())
    session.add(job)
    await session.flush()
    for i in range(n):
        node = ExecutionNode(org_id=org, job_id=job.id, node_key=f"n{i}", node_kind="tts.segment")
        session.add(node)
        await session.flush()
        attempt = JobAttempt(org_id=org, node_id=node.id, attempt_no=1, reason="initial", status="running")
        session.add(attempt)
        await session.flush()
        task_id = await queue.enqueue(
            session, org_id=org, node_id=node.id, attempt_id=attempt.id, capability="voice.tts", model_key="m",
            priority=50, vram_gb=0, est_seconds=1, constraints={"adapter_id": "a"}, payload={},
        )  # fmt: skip
        await session.execute(
            sa.update(GpuTask).where(GpuTask.id == task_id).values(created_at=created + timedelta(seconds=i))
        )


async def _first_late_lease(db: Database, weights: queue.PlacementWeights) -> int | None:
    """Leases one task at a time (the leases stay live) and returns how many leases it took
    until the late organization got its first one."""
    async with db.transaction() as session:
        await session.execute(sa.delete(GpuTask))
        big, small = Organization(name="backlog"), Organization(name="late")
        row = GpuWorker(runtime_family="cpu_model", gpu_type="cpu", state="idle")
        session.add_all([big, small, row])
        await session.flush()
        start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        await _enqueue(session, big.id, BACKLOG, start)
        await _enqueue(session, small.id, LATE, start + timedelta(minutes=5))
    worker = queue.WorkerView(row.id, frozenset({"a"}), free_vram_gb=24)
    now = datetime(2026, 10, 5, 12, 10, tzinfo=UTC)
    for n in range(1, BACKLOG + LATE + 1):
        async with db.transaction() as session:
            (task,) = await queue.lease(session, worker, now=now, lease_s=600, weights=weights)
            if task.org_id == small.id:
                return n
    return None


async def test_one_global_window_starves_a_late_organization(fair_db: TestDatabase) -> None:
    db = Database(fair_db.url, pool_size=2)
    try:
        assert await _first_late_lease(db, queue.PlacementWeights()) == BACKLOG + 1  # after the whole backlog
    finally:
        await db.dispose()


async def test_fair_share_serves_the_late_organization_at_once(fair_db: TestDatabase) -> None:
    db = Database(fair_db.url, pool_size=2)
    try:
        fair = queue.PlacementWeights(**PlacementConfig().model_dump())  # the shipped defaults
        assert await _first_late_lease(db, fair) == 2  # the backlog's oldest task, then the late org
    finally:
        await db.dispose()
