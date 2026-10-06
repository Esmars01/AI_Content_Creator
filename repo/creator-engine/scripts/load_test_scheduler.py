"""Scheduler load test (Phase 14, §25): thousands of tasks, many simulated workers, on the real
Postgres queue and the real object store (SeaweedFS), through the scheduler's own service layer
(the worker API handlers call exactly these methods).

What it exercises, and what it checks at the end:

- **throughput and latency** — lease-call latency, queue wait (created → leased), end-to-end time;
- **fairness** — a large org enqueues its backlog first, two smaller orgs after it: their waits;
- **capacity changes** — half the workers at the start, the rest join mid-run (one group through
  the fleet manager: a provider that always fails, then fallback to a working one, one-time
  enrollment tokens), some workers leave mid-run;
- **failures** — retryable failures, OOM (with VRAM-class escalation), fatal failures, crashed
  workers (lease expiry → reaper → new attempt), zombie workers completing after their lease was
  reaped, corrupted uploads (sha mismatch → refused and retried), duplicate completions;
- **cancellation** — workflows cancelled mid-run (the probe cancels; the worker sees it at its
  heartbeat);
- **budget holds** — low-priority work held mid-run (no held task is leased), then released;
- **correctness** — every task ends in exactly one terminal state, no task is completed twice,
  no lease of a task overlapped another live lease, every succeeded task's outputs exist under
  their content address with the right hash, retries never exceed the budget.

Every number in the report is measured in this run; nothing is estimated. Usage:

    uv run python scripts/load_test_scheduler.py --tasks 3000 --workers 40 --out .data/load
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import platform
import random
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "scheduler" / "src"))

from ce_config.schemas import FleetConfig, GpuPool, PlacementConfig, SchedulerConfig  # noqa: E402
from ce_config.settings import load_effective  # noqa: E402
from ce_contracts.plugins import discover  # noqa: E402
from ce_core.enums import RuntimeFamily  # noqa: E402
from ce_core.vocab import load_vocabulary  # noqa: E402
from ce_db import fleet as fleet_db  # noqa: E402
from ce_db import queue  # noqa: E402
from ce_db.models.assets import ExecutionNode, GenerationJob, GpuTask, JobAttempt  # noqa: E402
from ce_db.models.platform import GpuWorker  # noqa: E402
from ce_db.models.tenancy import Organization  # noqa: E402
from ce_db.session import Database  # noqa: E402
from ce_plugin_gpu_mock.provider import MockGPUProvider  # noqa: E402
from ce_scheduler.completion import Cancelled  # noqa: E402
from ce_scheduler.fleet import FleetManager  # noqa: E402
from ce_scheduler.service import AuthError, Scheduler, StaleTaskError, WorkerIdentity  # noqa: E402
from ce_storage import content_key, create_storage  # noqa: E402
from ce_testing.database import TestDatabase  # noqa: E402
from ce_testing.fixtures import ALEX  # noqa: E402
from ce_testing.seed import seed_dev  # noqa: E402
from ce_testing.stack import stack_env  # noqa: E402
from ce_worker.protocol import (  # noqa: E402
    CompleteBody,
    FailBody,
    HeartbeatBody,
    LeaseBody,
    OutputDescriptor,
    RegisterBody,
)

ADAPTERS = ["mock_voice", "mock_image", "mock_avatar_global"]
OUTCOMES = {  # per leased attempt; the rest succeeds
    "retryable": 0.03,
    "oom": 0.01,
    "fatal": 0.005,
    "crash": 0.01,
    "zombie": 0.005,
    "corrupt": 0.01,
}


PRIORITY_OF: dict[str, int] = {}


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return round(ordered[k], 4)


def summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4) if values else None,
        "p50": pct(values, 0.5),
        "p95": pct(values, 0.95),
        "p99": pct(values, 0.99),
        "max": round(max(values), 4) if values else None,
    }


class RecordingCompleter:
    """Stands in for Temporal: records every completion and failure per task token; cancelled
    workflows answer `Cancelled` to the probe's heartbeat."""

    def __init__(self) -> None:
        self.completed: Counter[str] = Counter()
        self.failed: Counter[str] = Counter()
        self.cancel_tokens: set[str] = set()

    async def complete(self, token: str, result: dict[str, Any]) -> None:
        self.completed[token] += 1

    async def fail(self, token: str, error_class: str, message: str) -> None:
        self.failed[token] += 1

    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None:
        if token in self.cancel_tokens:
            raise Cancelled(token)

    async def report_cancellation(self, token: str) -> None:
        return None


@dataclass
class Stats:
    lease_calls: list[float] = field(default_factory=list)  # calls that returned a task
    empty_polls: list[float] = field(default_factory=list)  # long polls that returned nothing (≤ wait_s)
    queue_wait: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    # the fair comparison: waits of each org's tasks at one priority
    queue_wait_by_priority: dict[tuple[str, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    first_lease_at: dict[str, float] = field(default_factory=dict)
    outcomes: Counter[str] = field(default_factory=Counter)
    stale_rejections: Counter[str] = field(default_factory=Counter)
    leases: list[tuple[str, str, float, float]] = field(default_factory=list)  # task, worker, start, end (estimated)
    # task, worker, lease returned at, start of the last call the scheduler accepted from this holder
    accepted: list[tuple[str, str, float, float]] = field(default_factory=list)
    held_leased: int = 0
    outputs: list[str] = field(default_factory=list)  # sha256 of every output a completion reported
    errors: list[str] = field(default_factory=list)


@dataclass
class Run:
    sched: Scheduler
    completer: RecordingCompleter
    stats: Stats
    org_of_task: dict[str, str]
    start: float
    rng: random.Random
    work_s: tuple[float, float]
    stop: asyncio.Event
    held_now: set[str] = field(default_factory=set)
    # (hold committed, release starting): a held task whose lease call started and returned inside
    # this window was leased while held
    hold_window: tuple[float, float] = (float("inf"), float("inf"))
    fleet_log: list[str] = field(default_factory=list)
    spawned: list[asyncio.Task[Any]] = field(default_factory=list)


async def setup_database(args: argparse.Namespace) -> TestDatabase:
    database = TestDatabase(prefix="ce_load")
    await database.create()
    db = Database(database.url, pool_size=2)
    async with db.transaction() as session:
        await seed_dev(session, load_vocabulary(ROOT / "config" / "vocab"))
    await db.dispose()
    return database


async def enqueue_all(db: Database, args: argparse.Namespace, rng: random.Random) -> dict[str, str]:
    """A large org enqueues first, then two small ones; priorities 10–90."""
    orgs = {"large": ALEX.ORG_ID}
    async with db.transaction() as session:
        for name in ("small_a", "small_b"):
            org = Organization(name=f"load {name}")
            session.add(org)
            await session.flush()
            orgs[name] = org.id
    plan = [("large", int(args.tasks * 0.7)), ("small_a", int(args.tasks * 0.15))]
    plan.append(("small_b", args.tasks - sum(n for _, n in plan)))
    org_of: dict[str, str] = {}
    for name, count in plan:
        async with db.transaction() as session:
            job = GenerationJob(org_id=orgs[name], kind="generate", target_type="video_version", target_id=uuid.uuid4())
            session.add(job)
            await session.flush()
            for start in range(0, count, 500):
                batch = range(start, min(count, start + 500))
                nodes = [
                    ExecutionNode(
                        org_id=orgs[name], job_id=job.id, node_key=f"load:{name}:{i}", node_kind="tts.segment"
                    )
                    for i in batch
                ]
                session.add_all(nodes)
                await session.flush()
                attempts = [
                    JobAttempt(
                        org_id=orgs[name], node_id=n.id, attempt_no=1, reason="initial", seed=i, status="running"
                    )
                    for i, n in zip(batch, nodes, strict=True)
                ]
                session.add_all(attempts)
                await session.flush()
                for node, attempt in zip(nodes, attempts, strict=True):
                    adapter = rng.choice(ADAPTERS)
                    priority = rng.choice([10, 30, 50, 50, 70, 90])
                    token = uuid.uuid4().hex
                    task_id = await queue.enqueue(
                        session,
                        org_id=orgs[name],
                        node_id=node.id,
                        attempt_id=attempt.id,
                        capability="load.test",
                        model_key=f"{adapter}-model",
                        priority=priority,
                        vram_gb=0,
                        est_seconds=1,
                        constraints={"adapter_id": adapter},
                        payload={"adapter_id": adapter, "seed": 1, "request": {}, "inputs": [], "task_token": token},
                    )
                    org_of[str(task_id)] = name
                    PRIORITY_OF[str(task_id)] = priority
    return org_of


async def worker_loop(run: Run, name: str, identity: WorkerIdentity, *, leave_at: float | None, vram: float) -> None:
    sched, stats = run.sched, run.stats
    async with httpx.AsyncClient(timeout=30) as http:
        while not run.stop.is_set():
            if leave_at is not None and time.monotonic() - run.start >= leave_at:
                return
            t0 = time.monotonic()
            try:
                reply = await sched.lease(
                    identity, LeaseBody(adapters=ADAPTERS, wait_s=0.5, max_tasks=1, free_vram_gb=vram)
                )
            except AuthError:
                return
            (stats.lease_calls if reply.tasks else stats.empty_polls).append(time.monotonic() - t0)
            for task in reply.tasks:
                leased_at = time.monotonic()
                org = run.org_of_task.get(task.task_id, "?")
                held_from, held_until = run.hold_window
                if task.task_id in run.held_now and held_from < t0 and leased_at < held_until:
                    stats.held_leased += 1
                async with sched.db.session() as session:
                    created = (
                        await session.execute(
                            sa.select(GpuTask.created_at).where(GpuTask.id == uuid.UUID(task.task_id))
                        )
                    ).scalar_one()
                waited = (datetime.now(UTC) - created).total_seconds()
                stats.queue_wait[org].append(waited)
                stats.queue_wait_by_priority[(org, PRIORITY_OF.get(task.task_id, -1))].append(waited)
                stats.first_lease_at.setdefault(org, leased_at - run.start)
                await run_task(run, http, identity, task, leased_at)


async def run_task(run: Run, http: httpx.AsyncClient, identity: WorkerIdentity, task: Any, leased_at: float) -> None:
    sched, stats, rng = run.sched, run.stats, run.rng
    roll, acc, outcome = rng.random(), 0.0, "success"
    for kind, p in OUTCOMES.items():
        acc += p
        if roll < acc:
            outcome = kind
            break
    end = leased_at
    accepted = [leased_at]  # start times of this holder's calls the scheduler accepted

    async def call(fn: Any, *args: Any) -> Any:
        started = time.monotonic()
        result = await fn(identity, *args)
        accepted.append(started)
        return result

    abandoned = False  # crashed or zombie: the lease ends when it expires, whatever the worker does later
    try:
        beat = await call(sched.heartbeat, HeartbeatBody(task_id=task.task_id, progress=0.1))
        last_beat = time.monotonic()
        if beat.cancel:
            await call(sched.fail, FailBody(task_id=task.task_id, error_class="cancelled", message="cancelled"))
            stats.outcomes["cancelled_seen"] += 1
            return
        await asyncio.sleep(rng.uniform(*run.work_s))
        if outcome in ("crash", "zombie"):
            stats.outcomes[outcome] += 1
            abandoned = True
            end = last_beat + sched.config.lease_s  # the last heartbeat extended the lease this far
            if outcome == "zombie":  # comes back after the reaper requeued its task
                await asyncio.sleep(sched.config.lease_s + sched.config.reaper_interval_s + 1.5)
                try:
                    await call(sched.complete, CompleteBody(task_id=task.task_id, result={}, outputs=[]))
                    stats.errors.append(f"zombie completion accepted for {task.task_id}")
                except StaleTaskError:
                    stats.stale_rejections["zombie_complete"] += 1
            return
        beat = await call(sched.heartbeat, HeartbeatBody(task_id=task.task_id, progress=0.9))
        if beat.cancel:
            await call(sched.fail, FailBody(task_id=task.task_id, error_class="cancelled", message="cancelled"))
            stats.outcomes["cancelled_seen"] += 1
            return
        if outcome in ("retryable", "oom", "fatal"):
            await call(sched.fail, FailBody(task_id=task.task_id, error_class=outcome, message=f"injected {outcome}"))
            stats.outcomes[outcome] += 1
            return
        data = f"{task.task_id}:{task.attempt_id}:{uuid.uuid4().hex}".encode()
        sha = hashlib.sha256(data).hexdigest()
        slot = task.uploads[0]
        put = await http.put(slot.url, content=data, headers=slot.headers)
        if put.status_code >= 300:
            stats.errors.append(f"upload failed {put.status_code}")
        claimed = hashlib.sha256(b"something else").hexdigest() if outcome == "corrupt" else sha
        body = CompleteBody(
            task_id=task.task_id,
            result={"ok": True},
            outputs=[
                OutputDescriptor(
                    slot_key=slot.key, sha256=claimed, bytes=len(data), mime="application/octet-stream", kind="other"
                )
            ],
            busy_seconds=0.1,
        )
        await call(sched.complete, body)
        stats.outcomes["corrupt" if outcome == "corrupt" else "success"] += 1
        if outcome != "corrupt":
            stats.outputs.append(sha)
        if outcome == "success" and rng.random() < 0.02:  # a duplicate completion (network retry)
            try:
                await call(sched.complete, body)
                stats.errors.append(f"duplicate completion accepted for {task.task_id}")
            except StaleTaskError:
                stats.stale_rejections["duplicate_complete"] += 1
    except StaleTaskError:
        stats.stale_rejections["stale_during_run"] += 1
    finally:
        stats.leases.append(
            (task.task_id, str(identity.worker_id), leased_at, end if abandoned else max(end, time.monotonic()))
        )
        stats.accepted.append((task.task_id, str(identity.worker_id), leased_at, max(accepted)))


async def register(sched: Scheduler, name: str, token: str, family: str = "cpu_model") -> WorkerIdentity:
    reply = await sched.register(RegisterBody(name=name, runtime_family=family, adapters=ADAPTERS), token)
    return await sched.authenticate(reply.token)


async def loops(run: Run) -> None:
    """The scheduler's leader loops at their configured intervals: reaper, cancellation probe."""
    sched = run.sched
    last_probe = 0.0
    while not run.stop.is_set():
        await sched.reap_once()
        if time.monotonic() - last_probe >= sched.config.temporal_heartbeat_s:
            await sched.probe_cancellations_once()
            last_probe = time.monotonic()
        await asyncio.sleep(sched.config.reaper_interval_s)


async def chaos(run: Run, args: argparse.Namespace, fleet: FleetManager, provider: MockGPUProvider) -> None:
    """Mid-run events: cancellations, a budget hold and its release, fleet scale-up with fallback."""
    sched = run.sched
    await asyncio.sleep(args.event_at)
    # cancel 2 % of the queued work's workflows
    async with sched.db.session() as session:
        rows = (
            await session.execute(sa.select(GpuTask.payload).where(GpuTask.state == "queued").limit(args.tasks // 50))
        ).scalars().all()  # fmt: skip
    for payload in rows:
        run.completer.cancel_tokens.add(payload["task_token"])
    # the fleet adds workers: the first provider always fails, the fallback provisions
    decisions = await fleet.tick()
    for d in decisions:
        run.fleet_log.extend(d.attempts)
        for external_id in d.provisioned:
            env = provider.specs[external_id].env
            identity = await register(sched, env["WORKER_NAME"], env["WORKER_TOKEN"])
            run.fleet_log.append(f"fleet worker {external_id} registered with its one-time token")
            run.spawned.append(
                asyncio.create_task(worker_loop(run, env["WORKER_NAME"], identity, leave_at=None, vram=96))
            )
    # then hold low-priority work (as an exceeded daily budget does) and release it. After the fleet
    # tick: a tick recomputes holds from the real spend, which here is under budget
    async with sched.db.transaction() as session:
        change = await fleet_db.apply_holds(session, daily_exceeded=True, low_priority_max=25)
        held = (await session.execute(sa.select(GpuTask.id).where(GpuTask.held_reason.is_not(None)))).scalars().all()
    run.held_now.update(str(t) for t in held)
    run.hold_window = (time.monotonic(), float("inf"))
    run.fleet_log.append(f"budget hold: {change.held} queued tasks held")
    await asyncio.sleep(args.hold_s)
    run.hold_window = (run.hold_window[0], time.monotonic())
    async with sched.db.session() as session:  # database truth: held tasks that left the queue
        rows = (
            await session.execute(
                sa.select(GpuTask.state, sa.func.count())
                .where(GpuTask.id.in_([uuid.UUID(t) for t in run.held_now]))
                .group_by(GpuTask.state)
            )
        ).all()
        still_held = (
            await session.execute(
                sa.select(sa.func.count()).select_from(GpuTask).where(GpuTask.held_reason.is_not(None))
            )
        ).scalar_one()
    run.fleet_log.append(f"at release: held tasks by state {dict(rows)}; rows still marked held: {still_held}")
    left = sum(n for state, n in rows if state not in ("queued", "cancelled"))  # cancelling a held task is fine
    if left or still_held != len(run.held_now):
        run.stats.errors.append(f"budget hold broken: {left} held tasks left the queue, {still_held} still marked held")
    async with sched.db.transaction() as session:
        released = await fleet_db.apply_holds(session, daily_exceeded=False, low_priority_max=25)
    run.held_now.clear()
    run.fleet_log.append(f"budget released: {released.released} tasks")


async def wait_done(run: Run, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        async with run.sched.db.session() as session:
            active = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(GpuTask).where(GpuTask.state.in_(queue.ACTIVE_STATES))
                )
            ).scalar_one()
        if active == 0:
            return
        await asyncio.sleep(1.0)
    run.stats.errors.append("timeout: tasks still active")


async def verify(run: Run, storage: Any, bucket: str) -> dict[str, Any]:
    sched, stats, completer = run.sched, run.stats, run.completer
    async with sched.db.session() as session:
        tasks = (await session.execute(sa.select(GpuTask))).scalars().all()
        attempts = (await session.execute(sa.select(JobAttempt.node_id, JobAttempt.status, JobAttempt.reason))).all()
    states = Counter(t.state for t in tasks)
    problems: list[str] = list(stats.errors)
    if stats.held_leased:
        problems.append(f"{stats.held_leased} held tasks leased while held")
    if states.keys() - {"succeeded", "failed", "cancelled"}:
        problems.append(f"non-terminal tasks: {dict(states)}")
    succeeded_attempts = Counter(node for node, status, _ in attempts if status == "succeeded")
    doubled = [n for n, c in succeeded_attempts.items() if c > 1]
    if doubled:
        problems.append(f"{len(doubled)} nodes with two succeeded attempts")
    tokens = {str(t.id): str((t.payload or {}).get("task_token") or "") for t in tasks}
    over = [tok for tok, c in completer.completed.items() if c > 1]
    if over:
        problems.append(f"{len(over)} activities completed twice")
    for t in tasks:
        if t.state == "succeeded" and completer.completed[tokens[str(t.id)]] != 1:
            problems.append(f"succeeded task {t.id} delivered {completer.completed[tokens[str(t.id)]]} completions")
            break
    retries = [int((t.payload or {}).get("infra_retries", 0)) for t in tasks]
    if max(retries, default=0) > sched.config.max_infra_retries:
        problems.append("a task exceeded the infrastructure retry budget")
    oom = sum(1 for t in tasks if (t.payload or {}).get("oom_escalations"))
    # overlapping live leases of one task
    # Double ownership, decided only on what the scheduler accepted: a holder's call that started
    # after another worker's lease of the same task had already returned. (Worker-side lease-end
    # estimates are also reported, as `estimated_overlaps`: a crashed holder's end is estimated from
    # its last heartbeat's return, which can lag the server's expiry by the call's latency.)
    by_task: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for task_id, worker, leased, last_ok in stats.accepted:
        by_task[task_id].append((leased, last_ok, worker))
    overlaps = 0
    for spans in by_task.values():
        spans.sort()
        for i, (_l1, last_ok, w1) in enumerate(spans):
            overlaps += sum(1 for l2, _o, w2 in spans[i + 1 :] if w2 != w1 and last_ok > l2)
    estimated: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for task_id, worker, s, e in stats.leases:
        estimated[task_id].append((s, e, worker))
    estimated_overlaps = 0
    for spans in estimated.values():
        spans.sort()
        for (_s1, e1, w1), (s2, _e2, w2) in itertools.pairwise(spans):
            if s2 < e1 and w1 != w2:
                estimated_overlaps += 1
    if overlaps:
        problems.append(f"{overlaps} calls accepted from a holder after another worker leased the task")
    # every reported output exists at its content address, with its hash; corrupted uploads do not
    missing = 0
    for sha in stats.outputs:
        if not await storage.exists(bucket, content_key(sha)):
            missing += 1
    corrupt_stored = 0
    if await storage.exists(bucket, content_key(hashlib.sha256(b"something else").hexdigest())):
        corrupt_stored = 1
    if missing:
        problems.append(f"{missing} outputs missing from the content store")
    if corrupt_stored:
        problems.append("a corrupted upload reached the content store")
    return {
        "task_states": dict(states),
        "attempts_by_reason": dict(Counter(r for _, _, r in attempts)),
        "attempts_by_status": dict(Counter(s for _, s, _ in attempts)),
        "oom_escalated_tasks": oom,
        "max_infra_retries_seen": max(retries, default=0),
        "activities_completed": sum(completer.completed.values()),
        "activities_failed": sum(completer.failed.values()),
        "double_ownership": overlaps,
        "estimated_overlaps": estimated_overlaps,
        "outputs_checked": len(stats.outputs),
        "outputs_missing": missing,
        "problems": problems,
    }


def _load_env_defaults() -> None:
    """`.env` (else `.env.example`) as defaults, like the test suite's conftest."""
    import os

    env_file = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


async def main(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)  # noqa: S311 - reproducible simulation, not security
    database = await setup_database(args)
    try:
        env = stack_env(database.url)
        effective = load_effective(ROOT / "config", env)
        storage = create_storage(effective.settings)
        bucket = effective.settings.s3_bucket_artifacts
        await storage.ensure_bucket(bucket)
        db = Database(database.url, pool_size=args.pool)
        org_of = await enqueue_all(db, args, rng)
        completer = RecordingCompleter()
        config = SchedulerConfig(
            heartbeat_s=1,
            lease_s=args.lease_s,
            long_poll_s=0.5,
            poll_interval_s=0.1,
            max_infra_retries=3,
            reaper_interval_s=1.0,
            temporal_heartbeat_s=3.0,
            upload_slots=1,
            placement=PlacementConfig(fair_share=args.fair_share, per_org_candidates=args.per_org_candidates),
        )
        sched = Scheduler(
            db=db,
            storage=storage,
            bucket=bucket,
            registry=discover(app_env="test", include_mocks=True),
            completer=completer,
            config=config,
            registration_token="load-test",  # noqa: S106 - a throwaway database
            vram_classes=(24.0, 48.0, 96.0),
        )
        run = Run(
            sched, completer, Stats(), org_of, time.monotonic(), rng, (args.work_min, args.work_max), asyncio.Event()
        )
        broken = MockGPUProvider(
            {
                "classes": {"mock_gpu": {"vram_gb": 96, "price_per_hour_usd": 0.3}},
                "regions": ["local"],
                "provision_failure_rate": 1.0,
            }
        )
        working = MockGPUProvider(
            {"classes": {"mock_gpu": {"vram_gb": 96, "price_per_hour_usd": 0.5}}, "regions": ["local"]}
        )
        pool = GpuPool(
            id="load", gpu_classes=["mock_gpu"], providers=["broken", "working"], families=[RuntimeFamily.CPU_MODEL],
            min=0, max=args.fleet_workers, idle_timeout_s=60, target_latency_s=1, spot_ok=True, regions=["local"],
            enabled=True, autoscale=True,
        )  # fmt: skip
        fleet = FleetManager(
            db=db, pools=[pool], providers={"broken": broken, "working": working},
            manifests={p.id: p.manifest for p in sched.registry.plugins.values()}, budget_daily_usd=1_000_000.0,
            config=FleetConfig(provision_timeout_s=600), scheduler_url="http://scheduler", app_env="test",
        )  # fmt: skip
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(loops(run)),
            asyncio.create_task(chaos(run, args, fleet, working)),
        ]
        initial = args.workers // 2
        for i in range(args.workers):
            vram = rng.choice([24.0, 48.0, 96.0])
            identity = await register(sched, f"w{i}", "load-test")
            delay = 0.0 if i < initial else args.event_at  # the second half joins mid-run
            leave = args.leave_at if i % 5 == 4 else None  # every fifth worker leaves mid-run

            async def start(
                identity: WorkerIdentity = identity,
                delay: float = delay,
                leave: float | None = leave,
                vram: float = vram,
                name: str = f"w{i}",
            ) -> None:
                await asyncio.sleep(delay)
                await worker_loop(run, name, identity, leave_at=leave, vram=vram)

            tasks.append(asyncio.create_task(start()))
        t0 = time.monotonic()
        await wait_done(run, args.timeout)
        elapsed = time.monotonic() - t0
        run.stop.set()
        await asyncio.gather(*tasks, *run.spawned, return_exceptions=True)
        checks = await verify(run, storage, bucket)
        async with db.session() as session:
            registered = (await session.execute(sa.select(sa.func.count()).select_from(GpuWorker))).scalar_one()
        stats = run.stats
        report = {
            "when": datetime.now(UTC).isoformat(timespec="seconds"),
            "environment": {
                "python": platform.python_version(),
                "machine": platform.machine(),
                "cpus": __import__("os").cpu_count(),
            },
            "parameters": vars(args),
            "elapsed_s": round(elapsed, 2),
            "throughput_tasks_per_s": round(args.tasks / elapsed, 2) if elapsed else None,
            "lease_call_s": summary(stats.lease_calls),
            "empty_polls": {"n": len(stats.empty_polls), "mean_s": summary(stats.empty_polls)["mean"]},
            "queue_wait_s": {org: summary(v) for org, v in sorted(stats.queue_wait.items())},
            "queue_wait_s_by_priority": {
                f"p{prio}": {o: summary(v) for (o, pr), v in sorted(stats.queue_wait_by_priority.items()) if pr == prio}
                for prio in sorted({pr for _, pr in stats.queue_wait_by_priority})
            },
            "first_lease_after_s": {k: round(v, 2) for k, v in sorted(stats.first_lease_at.items())},
            "worker_outcomes": dict(stats.outcomes),
            "stale_rejections": dict(stats.stale_rejections),
            "held_tasks_leased_while_held": stats.held_leased,
            "workers_registered": registered,
            "fleet": run.fleet_log,
            "checks": checks,
        }
        await db.dispose()
        return report
    finally:
        await database.drop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tasks", type=int, default=3000)
    parser.add_argument("--workers", type=int, default=40)
    parser.add_argument("--fleet-workers", type=int, default=4)
    parser.add_argument("--pool", type=int, default=30, help="database connections")
    parser.add_argument("--lease-s", type=float, default=4.0)
    parser.add_argument("--work-min", type=float, default=0.02)
    parser.add_argument("--work-max", type=float, default=0.2)
    parser.add_argument("--event-at", type=float, default=15.0, help="seconds: workers join, cancellations, holds")
    parser.add_argument("--leave-at", type=float, default=30.0, help="seconds: every fifth worker leaves")
    parser.add_argument("--hold-s", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fair-share", type=float, default=None, help="placement.fair_share (default: config)")
    parser.add_argument("--per-org-candidates", type=int, default=None, help="placement.per_org_candidates")
    parser.add_argument("--out", type=Path, default=ROOT / ".data" / "load")
    arguments = parser.parse_args()
    defaults = PlacementConfig()  # the shipped defaults when not overridden
    arguments.fair_share = defaults.fair_share if arguments.fair_share is None else arguments.fair_share
    if arguments.per_org_candidates is None:
        arguments.per_org_candidates = defaults.per_org_candidates
    _load_env_defaults()
    result = asyncio.run(main(arguments))
    arguments.out.mkdir(parents=True, exist_ok=True)
    path = arguments.out / f"scheduler-load-{result['when'].replace(':', '')}.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    print(f"report: {path}")
    sys.exit(1 if result["checks"]["problems"] else 0)
