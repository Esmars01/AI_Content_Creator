"""Scheduler (§25): placement scoring, SKIP LOCKED leasing, heartbeats, completion with output
verification, failure classification, lease expiry, cancellation probes and the fleet manager."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_config.schemas import GpuPool, SchedulerConfig
from ce_config.settings import load_effective
from ce_contracts.common import HardwareInfo
from ce_contracts.plugins import discover
from ce_core.enums import RuntimeFamily
from ce_db import queue
from ce_db.models.assets import ExecutionNode, GenerationJob, GpuTask, JobAttempt
from ce_db.session import Database
from ce_scheduler.completion import Cancelled, Gone
from ce_scheduler.fleet import desired_workers
from ce_scheduler.service import AuthError, Scheduler, StaleTaskError
from ce_storage import content_key, create_storage
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX
from ce_testing.seed import example_version_row, seed_dev
from ce_testing.stack import stack_env
from ce_worker.protocol import CompleteBody, FailBody, HeartbeatBody, LeaseBody, OutputDescriptor, RegisterBody

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def test_placement_prefers_priority_age_resident_and_cheap_workers() -> None:
    weights = queue.PlacementWeights()
    worker = queue.WorkerView(
        uuid.uuid4(), frozenset({"a"}), resident_models=frozenset({"m1"}), cached_models=frozenset({"m2"})
    )

    def task(priority: int, model: str, age_min: float = 0) -> GpuTask:
        t = GpuTask(priority=priority, model_key=model)
        t.created_at = NOW - timedelta(minutes=age_min)
        return t

    resident = queue.placement_score(task(50, "m1"), worker, NOW, weights)
    cached = queue.placement_score(task(50, "m2"), worker, NOW, weights)
    cold = queue.placement_score(task(50, "m3"), worker, NOW, weights, model_size_gb=40)
    assert resident > cached > cold
    assert queue.placement_score(task(50, "m3", age_min=60), worker, NOW, weights) > queue.placement_score(
        task(50, "m3"), worker, NOW, weights
    )
    pricey = queue.WorkerView(worker.worker_id, worker.adapters, price_per_hour_usd=3.0)
    assert queue.placement_score(task(50, "m1"), pricey, NOW, weights) < queue.placement_score(
        task(50, "m1"), worker, NOW, weights
    )


def test_desired_workers_follow_backlog_and_bounds() -> None:
    pool = GpuPool(
        id="p",
        gpu_classes=["mock_gpu"],
        providers=["mock"],
        families=[RuntimeFamily.CPU_MODEL],
        min=0,
        max=3,
        idle_timeout_s=60,
        target_latency_s=60,
        spot_ok=True,
        regions=["local"],
        enabled=True,
    )
    assert desired_workers(0, pool) == 0
    assert desired_workers(90, pool) == 2
    assert desired_workers(10_000, pool) == 3


@pytest.fixture(scope="module")
def sched_db(repo_vocab: Any) -> Any:
    database = TestDatabase(prefix="ce_sched")

    async def create() -> None:
        await database.create()
        db = Database(database.url, pool_size=1)
        from ce_db.models.videos import Video, VideoVersion

        async with db.transaction() as session:
            await seed_dev(session, repo_vocab)
            session.add(Video(id=ALEX.VIDEO_ID, org_id=ALEX.ORG_ID, project_id=ALEX.PROJECT_ID))
            await session.flush()
            session.add(VideoVersion(**example_version_row()))
        await db.dispose()

    asyncio.run(create())
    yield database
    asyncio.run(database.drop())


class FakeCompleter:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict[str, Any]]] = []
        self.failed: list[tuple[str, str]] = []
        self.cancel_tokens: set[str] = set()
        self.gone_tokens: set[str] = set()
        self.reported: list[str] = []

    async def complete(self, token: str, result: dict[str, Any]) -> None:
        self.completed.append((token, result))

    async def fail(self, token: str, error_class: str, message: str) -> None:
        self.failed.append((token, error_class))

    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None:
        if token in self.cancel_tokens:
            raise Cancelled(token)
        if token in self.gone_tokens:
            raise Gone(token)

    async def report_cancellation(self, token: str) -> None:
        self.reported.append(token)


@pytest_asyncio.fixture
async def sched(sched_db: TestDatabase) -> AsyncIterator[tuple[Scheduler, FakeCompleter]]:
    env = stack_env(sched_db.url)
    effective = load_effective(ROOT / "config", env)
    storage = create_storage(effective.settings)
    await storage.ensure_bucket(effective.settings.s3_bucket_artifacts)
    db = Database(sched_db.url, pool_size=4)
    completer = FakeCompleter()
    config = SchedulerConfig(heartbeat_s=1, lease_s=2, long_poll_s=0.5, poll_interval_s=0.1, max_infra_retries=1)
    s = Scheduler(
        db=db,
        storage=storage,
        bucket=effective.settings.s3_bucket_artifacts,
        registry=discover(app_env="test", include_mocks=True),
        completer=completer,
        config=config,
        registration_token="reg",
    )
    async with db.transaction() as session:  # one generation job and node to hang tasks on
        await session.execute(sa.delete(GpuTask))
    yield s, completer
    await db.dispose()


async def _task(
    s: Scheduler, *, adapter: str = "mock_voice", priority: int = 50, token: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with s.db.transaction() as session:
        job = GenerationJob(
            org_id=ALEX.ORG_ID,
            kind="generate",
            target_type="video_version",
            target_id=ALEX.VERSION_ID,
            video_version_id=ALEX.VERSION_ID,
        )
        session.add(job)
        await session.flush()
        node = ExecutionNode(
            org_id=ALEX.ORG_ID,
            job_id=job.id,
            version_id=ALEX.VERSION_ID,
            node_key=f"tts.segment:{uuid.uuid4().hex[:6]}",
            node_kind="tts.segment",
        )
        session.add(node)
        await session.flush()
        attempt = JobAttempt(
            org_id=ALEX.ORG_ID, node_id=node.id, attempt_no=1, reason="initial", seed=99, status="running"
        )
        session.add(attempt)
        await session.flush()
        task_id = await queue.enqueue(
            session,
            org_id=ALEX.ORG_ID,
            node_id=node.id,
            attempt_id=attempt.id,
            capability="voice.tts",
            model_key="mock-voice",
            priority=priority,
            vram_gb=0,
            est_seconds=1,
            constraints={"adapter_id": adapter},
            payload={
                "adapter_id": adapter,
                "seed": 99,
                "request": {"text": "hi", "language": "en"},
                "inputs": [],
                "task_token": token or uuid.uuid4().hex,
                "version_id": str(ALEX.VERSION_ID),
            },
        )
    return task_id, attempt.id


async def _worker(s: Scheduler) -> Any:
    reply = await s.register(
        RegisterBody(name="w", runtime_family="cpu_model", adapters=["mock_voice", "mock_avatar_global", "evil"]), "reg"
    )
    assert "evil" not in reply.adapters and "mock_voice" in reply.adapters
    return await s.authenticate(reply.token)


@pytest.mark.infra
async def test_registration_needs_the_token(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, _ = sched
    with pytest.raises(AuthError):
        await s.register(RegisterBody(name="w", runtime_family="cpu_model", adapters=[]), "wrong")
    with pytest.raises(AuthError):
        await s.authenticate("nope")


@pytest.mark.infra
async def test_lease_matches_adapters_priority_and_skip_locked(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, _ = sched
    worker = await _worker(s)
    low, _ = await _task(s, priority=10)
    high, _ = await _task(s, priority=90)
    await _task(s, adapter="mock_image")  # the worker did not claim mock_image
    first = await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))
    assert [t.task_id for t in first.tasks] == [str(high)]
    assert first.tasks[0].uploads and first.tasks[0].seed == 99
    # two concurrent leases never return the same task
    other = await _worker(s)
    a, b = await asyncio.gather(
        s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0)),
        s.lease(other, LeaseBody(adapters=["mock_voice"], wait_s=0)),
    )
    got = [t.task_id for t in a.tasks + b.tasks]
    assert got == [str(low)] and len(set(got)) == 1
    empty = await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))
    assert empty.tasks == []


@pytest.mark.infra
async def test_complete_verifies_outputs_and_completes_the_activity(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, completer = sched
    worker = await _worker(s)
    task_id, _ = await _task(s, token="tok-complete")
    leased = (await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))).tasks[0]
    data = b"fake audio bytes"
    sha = hashlib.sha256(data).hexdigest()
    slot = leased.uploads[0]
    async with httpx.AsyncClient() as http:
        assert (await http.put(slot.url, content=data, headers=slot.headers)).status_code == 200
    beat = await s.heartbeat(worker, HeartbeatBody(task_id=leased.task_id, progress=0.5))
    assert beat.state == "running" and not beat.cancel
    result = {"audio": {"sha256": sha, "kind": "audio"}, "duration_s": 1.0, "sample_rate": 48000}
    await s.complete(
        worker,
        CompleteBody(
            task_id=leased.task_id,
            result=result,
            outputs=[OutputDescriptor(slot_key=slot.key, sha256=sha, bytes=len(data), mime="audio/wav", kind="audio")],
            busy_seconds=1.5,
        ),
    )
    token, payload = completer.completed[-1]
    assert token == "tok-complete" and payload["outputs"][0]["storage_key"] == content_key(sha)
    assert await s.storage.get(s.bucket, content_key(sha)) == data
    assert not await s.storage.exists(s.bucket, slot.key)
    async with s.db.session() as session:
        task = await session.get_one(GpuTask, task_id)
        attempt = await session.get_one(JobAttempt, task.attempt_id)
    assert task.state == "succeeded" and attempt.status == "succeeded" and attempt.gpu_seconds == 1.5
    with pytest.raises(StaleTaskError):  # a late duplicate report is refused
        await s.complete(worker, CompleteBody(task_id=leased.task_id, result=result))


@pytest.mark.infra
async def test_a_bad_upload_is_an_infrastructure_retry(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, completer = sched
    worker = await _worker(s)
    task_id, first_attempt = await _task(s)
    leased = (await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))).tasks[0]
    slot = leased.uploads[0]
    async with httpx.AsyncClient() as http:
        await http.put(slot.url, content=b"other bytes", headers=slot.headers)
    lie = OutputDescriptor(slot_key=slot.key, sha256="0" * 64, bytes=11, mime="audio/wav", kind="audio")
    await s.complete(worker, CompleteBody(task_id=leased.task_id, result={}, outputs=[lie]))
    async with s.db.session() as session:
        task = await session.get_one(GpuTask, task_id)
        old = await session.get_one(JobAttempt, first_attempt)
        new = await session.get_one(JobAttempt, task.attempt_id)
    assert (
        task.state == "queued"
        and old.error_class == "retryable"
        and new.reason == "infra_retry"
        and new.seed == old.seed
    )
    assert completer.completed == [] or all(t != leased.task_id for t, _ in completer.completed)


@pytest.mark.infra
async def test_fatal_failures_fail_the_activity(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, completer = sched
    worker = await _worker(s)
    task_id, _ = await _task(s, token="tok-fatal")
    leased = (await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))).tasks[0]
    await s.fail(worker, FailBody(task_id=leased.task_id, error_class="fatal", message="bad request"))
    assert ("tok-fatal", "fatal") in completer.failed
    async with s.db.session() as session:
        assert (await session.get_one(GpuTask, task_id)).state == "failed"


def test_next_vram_class_is_the_smallest_larger_one() -> None:
    s = Scheduler.__new__(Scheduler)
    s.vram_classes = (24.0, 32.0, 96.0)
    assert s.next_vram_class(24.0) == 32.0 and s.next_vram_class(0.0) == 24.0 and s.next_vram_class(96.0) is None


@pytest.mark.infra
async def test_oom_requeues_on_the_next_vram_class(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, _ = sched
    s.vram_classes = (24.0, 32.0, 96.0)
    small = await s.authenticate(
        (
            await s.register(
                RegisterBody(
                    name="w24", runtime_family="cpu_model", adapters=["mock_voice"], hardware=HardwareInfo(vram_gb=24)
                ),
                "reg",
            )
        ).token
    )
    task_id, first_attempt = await _task(s)
    leased = (await s.lease(small, LeaseBody(adapters=["mock_voice"], free_vram_gb=24, wait_s=0))).tasks[0]
    await s.fail(small, FailBody(task_id=leased.task_id, error_class="oom", message="CUDA out of memory"))
    async with s.db.session() as session:
        task = await session.get_one(GpuTask, task_id)
        new = await session.get_one(JobAttempt, task.attempt_id)
    assert task.state == "queued" and task.vram_gb == 32.0 and new.reason == "infra_retry"
    assert task.payload["oom_escalations"] == [{"from_gb": 0.0, "to_gb": 32.0}]
    again = await s.lease(small, LeaseBody(adapters=["mock_voice"], free_vram_gb=24, wait_s=0))
    assert again.tasks == []  # the 24 GB worker no longer qualifies
    big = await _worker(s)
    moved = await s.lease(big, LeaseBody(adapters=["mock_voice"], free_vram_gb=32, wait_s=0))
    assert [t.task_id for t in moved.tasks] == [str(task_id)] and first_attempt != task.attempt_id


@pytest.mark.infra
async def test_expired_leases_are_requeued_then_failed_when_retries_run_out(
    sched: tuple[Scheduler, FakeCompleter],
) -> None:
    s, completer = sched
    worker = await _worker(s)
    task_id, _ = await _task(s, token="tok-expire")
    await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))
    s.clock = lambda: datetime.now(UTC) + timedelta(seconds=10)
    assert await s.reap_once() == 1
    async with s.db.session() as session:
        task = await session.get_one(GpuTask, task_id)
    assert task.state == "queued" and task.payload["infra_retries"] == 1
    with pytest.raises(StaleTaskError):  # the old holder lost the task
        await s.heartbeat(worker, HeartbeatBody(task_id=str(task_id)))
    s.clock = lambda: datetime.now(UTC)
    await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))
    s.clock = lambda: datetime.now(UTC) + timedelta(seconds=10)
    await s.reap_once()  # max_infra_retries=1: now it fails
    assert ("tok-expire", "retryable") in completer.failed
    async with s.db.session() as session:
        assert (await session.get_one(GpuTask, task_id)).state == "failed"


@pytest.mark.infra
async def test_cancelled_workflows_cancel_their_tasks(sched: tuple[Scheduler, FakeCompleter]) -> None:
    s, completer = sched
    worker = await _worker(s)
    task_id, _ = await _task(s, token="tok-cancel")
    gone_id, _ = await _task(s, token="tok-gone")
    leased = (await s.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0, max_tasks=1))).tasks[0]
    completer.cancel_tokens.add("tok-cancel")
    completer.gone_tokens.add("tok-gone")
    assert await s.probe_cancellations_once() >= 2
    assert "tok-cancel" in completer.reported
    async with s.db.session() as session:
        states = {
            (await session.get_one(GpuTask, task_id)).state,
            (await session.get_one(GpuTask, gone_id)).state,
        }
    assert states == {"cancelled"}
    beat = await s.heartbeat(worker, HeartbeatBody(task_id=leased.task_id)) if leased.task_id == str(task_id) else None
    if beat is not None:
        assert beat.cancel
