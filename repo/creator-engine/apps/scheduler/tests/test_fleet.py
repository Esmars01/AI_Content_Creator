"""The fleet manager on simulated providers (Phase 9 DoD): scale up and down, budget holds, provider
fallback, enrollment of provisioned workers, provisioning timeouts, stop and restart, fleet costs,
and the provider-plugin path through a third-party stub. No real provider is contacted."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_config.schemas import FleetConfig, GpuPool, GpuVariant, SchedulerConfig
from ce_contracts.plugins import discover
from ce_core.enums import RuntimeFamily
from ce_db import fleet as fleet_db
from ce_db import queue
from ce_db.execution import record_cost
from ce_db.models.assets import ExecutionNode, GenerationJob, GpuTask, JobAttempt, Notification
from ce_db.models.platform import FleetCost, GpuProvider, GpuWorker, WorkerEnrollmentToken
from ce_db.models.videos import Video
from ce_db.session import Database
from ce_gpu.provider import ProvisionSpec
from ce_plugin_gpu_example_cloud.provider import ExampleCloudProvider
from ce_plugin_gpu_mock.provider import MockGPUProvider
from ce_scheduler.fleet import FleetActionError, FleetManager, driver_at_least
from ce_scheduler.providers import FleetProvider, load_providers, resolve_credentials
from ce_scheduler.service import AuthError, Scheduler
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX
from ce_testing.seed import example_version_row, seed_dev
from ce_worker.protocol import HeartbeatBody, LeaseBody, RegisterBody

pytestmark = pytest.mark.infra

REGISTRY = discover(app_env="test", include_mocks=True)
MANIFESTS = {p.id: p.manifest for p in REGISTRY.plugins.values()}


@pytest.fixture(scope="module")
def fleet_db_url(repo_vocab: Any) -> Any:
    database = TestDatabase(prefix="ce_fleet")

    async def create() -> None:
        await database.create()
        db = Database(database.url, pool_size=1)
        from ce_db.models.videos import VideoVersion

        async with db.transaction() as session:
            await seed_dev(session, repo_vocab)
            session.add(Video(id=ALEX.VIDEO_ID, org_id=ALEX.ORG_ID, project_id=ALEX.PROJECT_ID))
            await session.flush()
            session.add(VideoVersion(**example_version_row()))
        await db.dispose()

    asyncio.run(create())
    yield database.url
    asyncio.run(database.drop())


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


class Completer:
    async def complete(self, token: str, result: dict[str, Any]) -> None: ...
    async def fail(self, token: str, error_class: str, message: str) -> None: ...
    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None: ...
    async def report_cancellation(self, token: str) -> None: ...


@pytest_asyncio.fixture
async def db(fleet_db_url: str) -> AsyncIterator[Database]:
    database = Database(fleet_db_url, pool_size=4)
    async with database.transaction() as session:  # a clean fleet: earlier workers gone, no queue
        await session.execute(sa.update(GpuTask).values(state="cancelled", held_reason=None))
        await session.execute(
            sa.update(GpuWorker)
            .where(GpuWorker.state.notin_(("stopped", "failed")))
            .values(state="stopped", stopped_at=sa.func.now())
        )
        await session.execute(sa.update(Video).values(budget_usd=None))
        await session.execute(  # rows earlier tests created stay referenced: retire them
            sa.update(GpuProvider).values(kind=sa.literal("retired-") + sa.cast(GpuProvider.id, sa.String),
                                          name=sa.literal("retired-") + sa.cast(GpuProvider.id, sa.String))
        )  # fmt: skip
    yield database
    await database.dispose()


class Storage:
    async def presign_put(self, bucket: str, key: str, **_: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(url=f"http://storage/{key}", headers={}, method="PUT")


def _scheduler(db: Database) -> Scheduler:
    return Scheduler(
        db=db, storage=Storage(), bucket="unused", registry=REGISTRY, completer=Completer(),  # type: ignore[arg-type]
        config=SchedulerConfig(long_poll_s=0.2, poll_interval_s=0.05), registration_token="shared",
    )  # fmt: skip


async def _task(db: Database, *, adapter: str = "mock_voice", priority: int = 50, est: float = 1.0) -> uuid.UUID:
    async with db.transaction() as session:
        job = GenerationJob(
            org_id=ALEX.ORG_ID, kind="generate", target_type="video_version", target_id=ALEX.VERSION_ID,
            video_version_id=ALEX.VERSION_ID,
        )  # fmt: skip
        session.add(job)
        await session.flush()
        node = ExecutionNode(
            org_id=ALEX.ORG_ID, job_id=job.id, version_id=ALEX.VERSION_ID,
            node_key=f"tts.segment:{uuid.uuid4().hex[:6]}", node_kind="tts.segment",
        )  # fmt: skip
        session.add(node)
        await session.flush()
        attempt = JobAttempt(org_id=ALEX.ORG_ID, node_id=node.id, attempt_no=1, reason="initial", seed=1)
        session.add(attempt)
        await session.flush()
        return await queue.enqueue(
            session, org_id=ALEX.ORG_ID, node_id=node.id, attempt_id=attempt.id, capability="voice.tts",
            model_key="mock-voice", priority=priority, vram_gb=0, est_seconds=est,
            constraints={"adapter_id": adapter},
            payload={"adapter_id": adapter, "seed": 1, "request": {}, "inputs": [],
                     "version_id": str(ALEX.VERSION_ID)},
        )  # fmt: skip


def _pool(**extra: Any) -> GpuPool:
    base: dict[str, Any] = dict(
        id="sim", gpu_classes=["mock_gpu"], providers=["mock"], families=[RuntimeFamily.CPU_MODEL], min=0, max=2,
        idle_timeout_s=60, target_latency_s=1, spot_ok=True, regions=["local"], enabled=True, autoscale=True,
    )  # fmt: skip
    return GpuPool(**{**base, **extra})


def _mock(price: float = 0.5, **extra: Any) -> MockGPUProvider:
    classes = {"mock_gpu": {"vram_gb": 96, "price_per_hour_usd": price}}
    return MockGPUProvider({"classes": classes, "regions": ["local", "eu-west"], **extra})


def _fleet(db: Database, providers: dict[str, Any], *, clock: Callable[[], datetime], **extra: Any) -> FleetManager:
    defaults: dict[str, Any] = dict(
        db=db, pools=[_pool()], providers=providers, manifests=MANIFESTS, budget_daily_usd=1_000_000.0,
        config=FleetConfig(provision_timeout_s=300, alert_cooldown_s=3600), scheduler_url="http://sched:8100",
        app_env="test", clock=clock,
    )  # fmt: skip
    return FleetManager(**{**defaults, **extra})


async def _register(sched: Scheduler, env: dict[str, str]) -> Any:
    body = RegisterBody(
        name=env["WORKER_NAME"], runtime_family=env["WORKER_RUNTIME_FAMILY"], adapters=["mock_voice"],
        gpu_type=env["WORKER_GPU_TYPE"], provider=env["WORKER_PROVIDER"], price_per_hour_usd=99.0,
    )  # fmt: skip
    return await sched.register(body, env["WORKER_TOKEN"])


async def _worker(db: Database, worker_id: Any) -> GpuWorker:
    async with db.session() as session:
        return await session.get_one(GpuWorker, uuid.UUID(str(worker_id)))


async def test_scale_up_enrolls_provisioned_workers_and_scale_down_records_fleet_costs(db: Database) -> None:
    clock = Clock()
    provider = _mock(price=0.5)
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    for _ in range(3):
        await _task(db)
    decision = (await fleet.tick())[0]
    assert (decision.desired, len(decision.provisioned)) == (2, 2)
    assert (await fleet.tick())[0].provisioned == []  # provisioning workers count: no double provisioning
    envs = [provider.instances[e] for e in decision.provisioned]
    assert all(i.state == "provisioning" or i.state == "running" for i in envs)
    async with db.session() as session:
        rows = list((await session.execute(sa.select(GpuWorker).where(GpuWorker.pool_id == "sim"))).scalars())
        tokens = list((await session.execute(sa.select(WorkerEnrollmentToken))).scalars())
    live = [r for r in rows if r.state == "provisioning"]
    assert len(live) == 2 and all(r.price_per_hour_usd == Decimal("0.5") for r in live)
    assert {t.worker_id for t in tokens} >= {r.id for r in live}

    # the provisioned host registers with the one-time token from its environment
    env = provider_env(provider, decision.provisioned[0])
    assert env["SCHEDULER_URL"] == "http://sched:8100" and env["WORKER_PROVIDER"] == "mock"
    reply = await _register(sched, env)
    row = await _worker(db, reply.worker_id)
    assert row.external_id == decision.provisioned[0] and row.state == "idle" and row.pool_id == "sim"
    assert row.price_per_hour_usd == Decimal("0.5")  # the provider's price, not what the worker claims
    assert row.registered_at is not None and row.provisioned_at is not None
    with pytest.raises(AuthError, match="used"):
        await _register(sched, env)  # one-time
    await _register(sched, provider_env(provider, decision.provisioned[1]))

    # no backlog, idle beyond the timeout → both terminated, their lifetimes recorded
    async with db.transaction() as session:
        await session.execute(sa.update(GpuTask).where(GpuTask.state == "queued").values(state="cancelled"))
    clock.offset = timedelta(seconds=120)
    decision = (await fleet.tick())[0]
    assert sorted(decision.stopped) == sorted(e.external_id for e in envs) and decision.desired == 0
    assert all(provider.instances[e.external_id].state == "terminated" for e in envs)
    async with db.session() as session:
        costs = list(
            (await session.execute(sa.select(FleetCost).where(FleetCost.worker_id.in_([r.id for r in live])))).scalars()
        )
    assert len(costs) == 2
    for cost in costs:
        assert (
            cost.pool_id == "sim" and cost.provisioned_seconds >= 120 and cost.idle_seconds == cost.provisioned_seconds
        )
        assert cost.idle_usd > 0 and cost.provisioned_usd == cost.idle_usd


def provider_env(provider: MockGPUProvider, external_id: str) -> dict[str, str]:
    """The mock keeps the instance; the environment the fleet passed is on its provision spec."""
    return provider.specs[external_id].env


async def test_static_workers_count_but_are_never_stopped(db: Database) -> None:
    clock = Clock()
    sched = _scheduler(db)
    static = await sched.register(
        RegisterBody(name="static", runtime_family="cpu_model", adapters=["mock_voice"], gpu_type="mock_gpu"), "shared"
    )
    fleet = _fleet(db, {"mock": _mock()}, clock=clock, pools=[_pool(min=0, max=1)])
    await _task(db)
    decision = (await fleet.tick())[0]
    assert decision.current == 1 and decision.provisioned == []  # the static worker covers the backlog
    async with db.transaction() as session:
        await session.execute(sa.update(GpuTask).where(GpuTask.state == "queued").values(state="cancelled"))
    clock.offset = timedelta(hours=1)
    assert (await fleet.tick())[0].stopped == []
    assert (await _worker(db, static.worker_id)).state == "idle"


async def test_provider_failure_falls_back_to_the_next_class_and_provider(db: Database) -> None:
    full = _mock(no_capacity=True)
    broken = _mock(provision_failure_rate=1.0)
    cloud = ExampleCloudProvider(
        {"quota": 1, "regions": {"eu-west": ["rtx_4090_24gb"]}, "prices": {"rtx_4090_24gb": 0.4},
         "vram": {"rtx_4090_24gb": 24}, "driver_version": "575.57"}
    )  # fmt: skip
    full.classes["rtx_4090_24gb"] = {"vram_gb": 24, "price_per_hour_usd": 0.3}
    broken.classes["rtx_4090_24gb"] = {"vram_gb": 24, "price_per_hour_usd": 0.3}
    pool = _pool(
        gpu_classes=["mock_gpu", "rtx_4090_24gb"], providers=["full", "broken", "example_cloud"], regions=["eu-west"],
        max=2,
    )  # fmt: skip
    fleet = _fleet(db, {"full": full, "broken": broken, "example_cloud": cloud}, clock=Clock(), pools=[pool])
    for _ in range(2):
        await _task(db)
    decision = (await fleet.tick())[0]
    assert decision.attempts[:4] == [
        "full/mock_gpu/eu-west: no capacity",
        "full/rtx_4090_24gb/eu-west: no capacity",
        "broken/mock_gpu/eu-west: error (mock provider: injected provision failure)",
        "broken/rtx_4090_24gb/eu-west: error (mock provider: injected provision failure)",
    ]
    assert decision.attempts[4] == "example_cloud/mock_gpu/eu-west: no capacity"  # not offered there
    assert decision.attempts[5].startswith("example_cloud/rtx_4090_24gb/eu-west: provisioned ec-")
    assert len(decision.provisioned) == 1  # the quota (1) stops the second one; every option was tried
    async with db.session() as session:
        live = sa.select(GpuWorker).where(GpuWorker.pool_id == "sim", GpuWorker.state == "provisioning")
        rows = list((await session.execute(live)).scalars())
        orphans = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(WorkerEnrollmentToken)
                .where(WorkerEnrollmentToken.worker_id.is_(None), WorkerEnrollmentToken.used_at.is_(None))
            )
        ).scalar_one()
    assert [(r.gpu_type, r.external_id) for r in rows] == [("rtx_4090_24gb", decision.provisioned[0])]
    assert orphans == 0  # refused provisions leave neither a worker row nor a live token
    assert rows[0].price_per_hour_usd == Decimal("0.4")


async def test_old_drivers_are_skipped() -> None:
    assert driver_at_least("575.57", "570") and not driver_at_least("565.1", "570")
    assert driver_at_least(None, "570") and driver_at_least("550", None)


async def test_driver_filter_skips_an_offer_below_the_pool_minimum(db: Database) -> None:
    cloud = ExampleCloudProvider(
        {"quota": 2, "regions": {"eu-west": ["rtx_4090_24gb"]}, "prices": {"rtx_4090_24gb": 0.4},
         "driver_version": "565.10"}
    )  # fmt: skip
    pool = _pool(gpu_classes=["rtx_4090_24gb"], providers=["example_cloud"], regions=["eu-west"],
                 min_driver_version="570")  # fmt: skip
    fleet = _fleet(db, {"example_cloud": cloud}, clock=Clock(), pools=[pool])
    await _task(db)
    decision = (await fleet.tick())[0]
    assert decision.provisioned == [] and decision.attempts == [
        "example_cloud/rtx_4090_24gb/eu-west: driver older than 570"
    ]
    assert cloud.machines == {}


async def test_budget_holds_low_priority_work_alerts_once_and_releases(db: Database) -> None:
    clock = Clock()
    published: list[tuple[uuid.UUID, str, dict[str, Any]]] = []

    async def publish(org: uuid.UUID, kind: str, data: dict[str, Any]) -> None:
        published.append((org, kind, data))

    async with db.transaction() as session:
        await record_cost(session, ALEX.ORG_ID, kind="gpu", quantity=3600, unit="gpu_second", unit_price_usd=0.001)
        spent = (await fleet_db.spend_report(session, now=clock(), horizon_h=1.0)).projected_usd
    low = await _task(db, priority=10)
    high = await _task(db, priority=80)
    fleet = _fleet(db, {"mock": _mock(price=5.0)}, clock=clock, budget_daily_usd=spent - 1.0, publish=publish)
    decision = (await fleet.tick())[0]
    async with db.session() as session:
        held = dict((await session.execute(sa.select(GpuTask.id, GpuTask.held_reason).where(
            GpuTask.id.in_([low, high])))).all())  # fmt: skip
        alerts = list(
            (await session.execute(sa.select(Notification).where(Notification.kind == "budget_alert"))).scalars()
        )
    assert held == {low: "budget_daily", high: None}
    assert decision.held and "BUDGET_DAILY_USD" in decision.held and decision.provisioned == []
    assert [(o, k, d["reason"]) for o, k, d in published] == [(ALEX.ORG_ID, "budget.alert", "budget_daily")]
    assert len(alerts) == 1 and alerts[0].org_id == ALEX.ORG_ID and alerts[0].payload["reason"] == "budget_daily"
    # a held task is never leased
    sched = _scheduler(db)
    reply = await sched.register(RegisterBody(name="w", runtime_family="cpu_model", adapters=["mock_voice"]), "shared")
    worker = await sched.authenticate(reply.token)
    leased = await sched.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0, max_tasks=4))
    assert [t.task_id for t in leased.tasks] == [str(high)]
    # still over budget: no second alert within the cooldown
    await _task(db, priority=5)
    await fleet.tick()
    assert len(published) == 1
    # the budget is raised → released on the next tick
    fleet.budget_daily_usd = spent + 1_000.0
    await fleet.tick()
    async with db.session() as session:
        assert (await session.get_one(GpuTask, low)).held_reason is None


async def test_a_video_over_its_budget_holds_all_its_work(db: Database) -> None:
    clock = Clock()
    async with db.transaction() as session:
        await record_cost(session, ALEX.ORG_ID, kind="gpu", quantity=10, unit="gpu_second", unit_price_usd=0.1,
                          version_id=ALEX.VERSION_ID, video_id=ALEX.VIDEO_ID)  # fmt: skip
        await session.execute(sa.update(Video).where(Video.id == ALEX.VIDEO_ID).values(budget_usd=Decimal("0.5")))
    task = await _task(db, priority=95)
    fleet = _fleet(db, {"mock": _mock()}, clock=clock)
    decision = (await fleet.tick())[0]
    async with db.session() as session:
        assert (await session.get_one(GpuTask, task)).held_reason == "budget_video"
    assert decision.backlog_seconds == 0 and decision.provisioned == []  # held work does not scale the fleet
    async with db.transaction() as session:
        await session.execute(sa.update(Video).where(Video.id == ALEX.VIDEO_ID).values(budget_usd=Decimal("100")))
    await fleet.tick()
    async with db.session() as session:
        assert (await session.get_one(GpuTask, task)).held_reason is None


async def test_a_provider_budget_stops_its_growth(db: Database) -> None:
    clock = Clock()
    async with db.transaction() as session:
        row = GpuProvider(kind="mock", name="sim-mock", enabled=True, budget_daily_usd=Decimal("0.0001"))
        session.add(row)
        await session.flush()
        row_id = row.id
    fp = FleetProvider(key="mock", provider=_mock(price=1.0), row_id=row_id, budget_daily_usd=0.0001)
    fleet = _fleet(db, {"mock": fp}, clock=clock)
    await _task(db)
    decision = (await fleet.tick())[0]
    assert len(decision.provisioned) == 1  # nothing spent yet under this provider
    await _task(db, est=5)
    decision = (await fleet.tick())[0]
    assert decision.provisioned == [] and decision.attempts == ["mock: provider budget_daily_usd reached"]


async def test_a_worker_that_never_registers_is_terminated_after_the_timeout(db: Database) -> None:
    clock = Clock()
    provider = _mock(price=0.5)
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    await _task(db)
    (external,) = (await fleet.tick())[0].provisioned
    clock.offset = timedelta(seconds=301)
    decision = (await fleet.tick())[0]
    assert decision.failed == [external] and provider.instances[external].state == "terminated"
    async with db.session() as session:
        row = (
            await session.execute(
                sa.select(GpuWorker).where(GpuWorker.external_id == external, GpuWorker.state == "failed")
                .order_by(GpuWorker.provisioned_at.desc()).limit(1)
            )
        ).scalar_one()  # fmt: skip
        cost = (await session.execute(sa.select(FleetCost).where(FleetCost.worker_id == row.id))).scalar_one()
    assert row.state == "failed" and cost.provisioned_seconds >= 300
    with pytest.raises(AuthError):  # its token expired with it
        await _register(_scheduler(db), provider.specs[external].env)
    assert len(decision.provisioned) == 1  # the backlog is still there: a replacement is provisioned


async def test_a_worker_the_reaper_failed_has_its_instance_terminated(db: Database) -> None:
    """Regression (audit W9): the reaper marked a stale fleet worker `failed` and stopped its cost,
    but nothing terminated the host, which kept billing outside the spend report."""
    clock = Clock()
    provider = _mock(price=0.5)
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    await _task(db)
    (external,) = (await fleet.tick())[0].provisioned
    registered = await _register(_scheduler(db), provider.specs[external].env)
    async with db.transaction() as session:  # what Scheduler.reap_once does to a silent worker
        row = await session.get_one(GpuWorker, uuid.UUID(str(registered.worker_id)))
        row.state, row.stopped_at = "failed", clock()
    assert provider.instances[external].state != "terminated"
    await fleet.tick()
    assert provider.instances[external].state == "terminated"
    assert (await _worker(db, registered.worker_id)).token_hash is None
    calls = len([i for i in provider.instances.values() if i.state == "terminated"])
    await fleet.tick()  # terminated once, not on every tick
    assert len([i for i in provider.instances.values() if i.state == "terminated"]) == calls


async def test_idle_action_stop_restarts_the_same_host_and_re_arms_its_token(db: Database) -> None:
    clock = Clock()
    provider = _mock(price=0.5)
    fleet = _fleet(db, {"mock": provider}, clock=clock, pools=[_pool(idle_action="stop", max=1)])
    sched = _scheduler(db)
    task = await _task(db)
    (external,) = (await fleet.tick())[0].provisioned
    env = provider.specs[external].env
    await _register(sched, env)
    async with db.transaction() as session:
        await session.execute(sa.update(GpuTask).where(GpuTask.id == task).values(state="cancelled"))
    clock.offset = timedelta(seconds=120)
    assert (await fleet.tick())[0].stopped == [external] and provider.instances[external].state == "stopped"
    await _task(db)
    clock.offset = timedelta(seconds=180)
    decision = (await fleet.tick())[0]
    assert decision.provisioned == [external] and provider.instances[external].state == "running"
    reply = await _register(sched, env)  # the same environment works again, once
    row = await _worker(db, reply.worker_id)
    assert row.external_id == external and row.state == "idle"
    async with db.session() as session:
        periods = (
            await session.execute(
                sa.select(sa.func.count()).select_from(FleetCost).where(FleetCost.worker_id == row.id)
            )
        ).scalar_one()
    assert periods == 1  # the stopped period; the running one is recorded when it ends


async def test_the_image_variant_follows_the_backlog(db: Database) -> None:
    provider = _mock()
    variants = {"mock_voice": GpuVariant(family=RuntimeFamily.CPU_MODEL, variant="voices"),
                "mock_image": GpuVariant(family=RuntimeFamily.CPU_MODEL, variant="images")}  # fmt: skip
    fleet = _fleet(db, {"mock": provider}, clock=Clock(), variants=variants, pools=[_pool(max=1)])
    await _task(db, adapter="mock_image", est=1)
    await _task(db, adapter="mock_voice", est=3)
    (external,) = (await fleet.tick())[0].provisioned
    spec = provider.specs[external]
    assert spec.variant == "voices" and spec.env["WORKER_ADAPTERS"] == "mock_voice"


async def test_providers_load_from_rows_with_credentials_references(db: Database, tmp_path: Path) -> None:
    secret = tmp_path / "key"
    secret.write_text("rp_secret_value\n")
    assert resolve_credentials(f"file:{secret}") == {"api_key": "rp_secret_value"}
    assert resolve_credentials("env:X", environ={"X": "v"}) == {"api_key": "v"}
    with pytest.raises(ValueError, match="env:MISSING") as info:
        resolve_credentials("env:MISSING", environ={})
    assert "rp_secret" not in str(info.value)

    loaded = await load_providers(db, app_env="test", include_mocks=True)
    assert {"mock", "example_cloud", "local", "local_docker"} <= set(loaded)
    assert "runpod_pod" not in loaded  # paid: needs an administrator's enabled row
    assert isinstance(loaded["example_cloud"].provider, ExampleCloudProvider)  # through its entry point

    async with db.transaction() as session:
        session.add(GpuProvider(kind="runpod_pod", name="rp", enabled=True, credentials_ref="env:RP_TEST_KEY",
                                config={"allow_paid": False}, budget_daily_usd=Decimal("2")))  # fmt: skip
        session.add(GpuProvider(kind="mock", name="mock-off", enabled=False))
    calls: list[str] = []

    def runpod(request: httpx.Request) -> httpx.Response:  # no request may leave this test
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(401, json={"error": "unauthorized"})

    reloaded = await load_providers(
        db, app_env="test", include_mocks=True, environ={"RP_TEST_KEY": "rp_x"}, previous=loaded,
        overrides={"runpod_pod": {"transport": httpx.MockTransport(runpod)}},
    )  # fmt: skip
    assert "mock" not in reloaded and reloaded["runpod_pod"].paid and reloaded["runpod_pod"].budget_daily_usd == 2
    assert reloaded["example_cloud"] is loaded["example_cloud"]  # unchanged → the same instance (its state)
    # the row did not approve spending: the plugin refuses before any request, the fleet moves on
    fleet = _fleet(db, reloaded, clock=Clock(), pools=[_pool(providers=["runpod_pod"], gpu_classes=["rtx_4090_24gb"],
                                                              regions=["eu"])])  # fmt: skip
    await _task(db)
    decision = (await fleet.tick())[0]
    assert decision.provisioned == [] and "allow_paid" in decision.attempts[0]
    status = await fleet.status()
    assert status["pools"][0]["desired"] == 1 and status["spend"]["budget_daily_usd"] == 1_000_000.0
    assert {p["key"]: p["paid"] for p in status["providers"]}["runpod_pod"] is True
    assert calls == ["GET /v1/pods"]  # the health probe only (answered by the fake): nothing was provisioned


async def test_a_stopped_worker_is_never_revived_by_its_own_calls(db: Database) -> None:
    """Regression: a worker the fleet stopped kept leasing (its token was cached) and its lease set
    the row back to idle. Now the lease is refused and the row stays stopped."""
    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    await _task(db)
    (external,) = (await fleet.tick())[0].provisioned
    reply = await _register(sched, provider.specs[external].env)
    worker = await sched.authenticate(reply.token)  # cached from now on
    assert await fleet.release(worker.worker_id) is not None
    with pytest.raises(AuthError, match="stopped"):
        await sched.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))
    assert (
        await _worker(db, worker.worker_id)
    ).state == "terminated"  # release terminates by default (cutover: distinct from stopped)
    with pytest.raises(AuthError):
        await sched.authenticate(reply.token)  # the cache entry is gone and the row has no token


# ---------------------------------------------------------------------- operations (production cutover)
class ListingMock(MockGPUProvider):
    """The mock provider with the optional instance listing (labels carry the fleet's worker id)."""

    async def list_instances(self) -> list[Any]:
        out = []
        for external_id, instance in self.instances.items():
            spec = self.specs.get(external_id)
            worker_id = spec.env.get("WORKER_ID") if spec else None
            out.append(
                instance.model_copy(update={"detail": {"worker_id": worker_id, "label": f"ce-worker-{worker_id}"}})
            )
        return out


async def _provisioned(db: Database, fleet: FleetManager, sched: Scheduler, provider: MockGPUProvider) -> Any:
    await _task(db)
    external = (await fleet.tick())[0].provisioned[0]
    reply = await _register(sched, provider.specs[external].env)
    return external, uuid.UUID(reply.worker_id), provider.specs[external].env


async def test_stopped_workers_stay_startable_and_terminate_is_distinct(db: Database) -> None:
    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    external, worker_id, env = await _provisioned(db, fleet, sched, provider)
    assert env["WORKER_ID"] == str(worker_id)  # providers label instances with it

    assert await fleet.release(worker_id, action="stop") is not None
    row = await _worker(db, worker_id)
    assert row.state == "stopped" and row.terminated_at is None and provider.instances[external].state == "stopped"
    assert await fleet.release(worker_id, action="stop") is None  # already stopped

    await fleet.start(worker_id)
    row = await _worker(db, worker_id)
    assert row.state == "provisioning" and provider.instances[external].state == "running"
    again = await _register(sched, env)  # the re-armed enrollment token works once more
    assert again.worker_id == str(worker_id)
    with pytest.raises(FleetActionError, match="only a stopped worker"):
        await fleet.start(worker_id)

    await fleet.release(worker_id, action="stop")
    assert await fleet.release(worker_id, action="terminate") is not None  # a stopped instance can be destroyed
    row = await _worker(db, worker_id)
    assert row.state == "terminated" and row.terminated_at is not None
    assert provider.instances[external].state == "terminated"
    assert await fleet.release(worker_id, action="terminate") is None  # nothing left


async def test_restart_reboots_an_idle_worker_and_refuses_a_busy_one(db: Database) -> None:
    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    _, worker_id, _ = await _provisioned(db, fleet, sched, provider)
    async with db.transaction() as session:
        await session.execute(sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(state="busy"))
    with pytest.raises(FleetActionError, match="idle or provisioning"):
        await fleet.restart(worker_id)
    async with db.transaction() as session:
        await session.execute(sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(state="idle"))
    await fleet.restart(worker_id)
    row = await _worker(db, worker_id)
    assert row.state == "provisioning" and row.registered_at is None
    async with db.session() as session:
        costs = (await session.execute(sa.select(FleetCost).where(FleetCost.worker_id == worker_id))).scalars().all()
    assert len(costs) == 1  # the ended lifetime is on record before the new one starts


async def test_reconcile_follows_the_provider(db: Database) -> None:
    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    external, worker_id, _ = await _provisioned(db, fleet, sched, provider)

    await provider.stop(external)  # stopped from the provider's console
    result = await fleet.refresh(worker_id)
    row = await _worker(db, worker_id)
    assert result["state"] == "stopped" and row.state == "stopped" and "outside the platform" in (row.last_error or "")
    assert row.provider_status["state"] == "stopped" and row.provider_checked_at is not None

    await provider.terminate(external)  # destroyed at the provider
    await fleet.refresh(worker_id)
    row = await _worker(db, worker_id)
    assert row.state == "terminated" and row.token_hash is None

    external2, worker2, _ = await _provisioned(db, fleet, sched, provider)
    provider.instances[external2] = provider.instances[external2].model_copy(update={"state": "terminated"})
    assert await fleet.reconcile() >= 1  # the leader's sweep finds a live worker whose instance is gone
    row = await _worker(db, worker2)
    assert row.state == "failed" and row.terminated_at is not None and "no longer has" in (row.last_error or "")


async def test_lost_instances_are_adopted_or_reported_as_orphans(db: Database) -> None:
    clock = Clock()
    provider = ListingMock({"classes": {"mock_gpu": {"vram_gb": 96, "price_per_hour_usd": 0.5}}, "regions": ["local"]})
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    external, worker_id, _ = await _provisioned(db, fleet, sched, provider)
    async with db.transaction() as session:  # mock instance ids restart per provider: retire earlier tests' rows
        await session.execute(sa.update(GpuWorker).where(GpuWorker.id != worker_id).values(state="terminated"))
    assert await fleet.orphans() == []  # tracked

    async with db.transaction() as session:  # a crash between the rental and its bookkeeping
        await session.execute(
            sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(external_id=None, state="provisioning")
        )
    assert await fleet.orphans() == []  # adopted, not rented again
    assert (await _worker(db, worker_id)).external_id == external

    stray = ProvisionSpec(gpu_class="mock_gpu", region="local", runtime_family="cpu_model",
                          env={"WORKER_ID": str(uuid.uuid4())})  # fmt: skip
    lost = await provider.provision(stray)
    orphans = await fleet.orphans()
    assert [o["external_id"] for o in orphans] == [lost.external_id]
    with pytest.raises(FleetActionError, match="not an orphan"):
        await fleet.terminate_orphan("mock", external)  # a tracked instance is never touched
    await fleet.terminate_orphan("mock", lost.external_id)
    assert provider.instances[lost.external_id].state == "terminated"
    assert await fleet.orphans() == []


async def test_leases_and_heartbeats_record_telemetry_and_task_phase(db: Database) -> None:
    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    task_id = await _task(db)
    (external,) = (await fleet.tick())[0].provisioned
    reply = await _register(sched, provider.specs[external].env)
    worker = await sched.authenticate(reply.token)
    telemetry = {"gpu_util_pct": 87.0, "vram_used_gb": 61.2, "disk": {"free_gb": 120.5, "total_gb": 200.0}}
    leased = await sched.lease(
        worker,
        LeaseBody(adapters=["mock_voice"], wait_s=0, telemetry=telemetry,
                  model_states={"mock-voice": {"state": "ready"}}),
    )  # fmt: skip
    assert leased.tasks
    row = await _worker(db, worker.worker_id)
    assert row.telemetry == telemetry and row.telemetry_at is not None
    assert row.model_states == {"mock-voice": {"state": "ready"}} and row.current_task_id is not None
    await sched.heartbeat(
        worker,
        HeartbeatBody(task_id=leased.tasks[0].task_id, progress=0.4, message="downloading",
                      phase="fetching_model", detail={"bytes_done": 10, "bytes_total": 100}),
    )  # fmt: skip
    async with db.session() as session:
        task = await session.get_one(GpuTask, uuid.UUID(leased.tasks[0].task_id))
    assert (task.phase, task.progress, task.progress_message) == ("fetching_model", 0.4, "downloading")
    assert task.progress_detail == {"bytes_done": 10, "bytes_total": 100}
    await sched.heartbeat(worker, HeartbeatBody(task_id=leased.tasks[0].task_id, phase="not-a-phase"))
    async with db.session() as session:
        assert (await session.get_one(GpuTask, uuid.UUID(leased.tasks[0].task_id))).phase is None
    # A report without telemetry keeps the last one (an older worker never erases it).
    await sched.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=0))
    assert (await _worker(db, worker.worker_id)).telemetry == telemetry
    del task_id


# ---------------------------------------------------------------------- model operations (production cutover)
def _talking_head(**extra: Any) -> Any:
    from ce_config.schemas import GpuProfile

    base: dict[str, Any] = dict(
        label="test talking head", gpu_class="mock_gpu", vram_gb=96, colocate=True, image="talking_head",
        components=[
            {"family": "wan", "variant": "infinitetalk", "adapters": ["infinitetalk"],
             "prepare": ["infinitetalk-single"]},
            {"family": "tts", "variant": "chatterbox", "adapters": ["chatterbox_turbo"],
             "prepare": ["chatterbox-turbo"]},
        ],
        scratch_gb=40, image_gb=35, staging_headroom=0.15,
    )  # fmt: skip
    return GpuProfile(**{**base, **extra})


def test_profile_sizing_comes_from_the_manifests() -> None:
    from ce_scheduler.profiles import size_profile

    sizing = size_profile(_talking_head(), MANIFESTS, {})
    assert [m.key for m in sizing.models] == ["infinitetalk-single", "chatterbox-turbo"]
    assert sizing.models_gb == round(86.92 + 3.77, 2)
    assert sizing.disk_gb == 180  # 90.69 + 13.6 staging + 40 scratch + 35 image = 179.3 → 180
    assert sizing.disk_gb > 80  # the old fixed default could not hold InfiniteTalk alone
    assert (sizing.vram_min_gb, sizing.vram_recommended_gb, sizing.fits) == (32.0, 64.0, True)
    assert any("declared" in n for n in sizing.notes) and not sizing.missing
    small = size_profile(_talking_head(vram_gb=24), MANIFESTS, {})
    assert small.fits is False
    bad = [{"family": "wan", "variant": "infinitetalk", "adapters": ["nope"], "prepare": ["x"]}]
    wrong = size_profile(_talking_head(components=bad), MANIFESTS, {})
    assert wrong.missing == ["adapter nope", "model x"]


async def test_a_colocated_profile_rents_one_instance_with_a_worker_per_family(db: Database) -> None:
    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock, profiles={"th": _talking_head()})
    sched = _scheduler(db)
    result = await fleet.provision_profile("th", _talking_head(), provider="mock", region="local")
    (instance,) = result["provisioned"]
    spec = provider.specs[instance["external_id"]]
    assert spec.disk_gb == 180 and spec.runtime_family == "profile" and spec.variant == "talking_head"
    env = spec.env
    assert env["WORKER_COMPONENTS"] == "wan,tts" and env["WORKER_PREPARE_WAN"] == "infinitetalk-single"
    assert env["WORKER_ADAPTERS_TTS"] == "chatterbox_turbo" and env["WORKER_PREPARE_WARM"] == "1"
    assert env["WORKER_TOKEN_WAN"] != env["WORKER_TOKEN_TTS"] and env["WORKER_ID"] == instance["workers"]["wan"]
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    sa.select(GpuWorker).where(
                        GpuWorker.external_id == instance["external_id"], GpuWorker.pool_id == "profile:th"
                    )
                )
            ).scalars()
        )
    assert sorted(r.runtime_family for r in rows) == ["tts", "wan"]
    assert {r.pool_id for r in rows} == {"profile:th"} and sum(float(r.price_per_hour_usd) for r in rows) == 0.5

    for family in ("wan", "tts"):  # each family's process registers with its own token
        body = RegisterBody(
            name=f"th-{family}", runtime_family=family, adapters=[], gpu_type="mock_gpu", provider="mock"
        )
        reply = await sched.register(body, env[f"WORKER_TOKEN_{family.upper()}"])
        assert reply.worker_id == instance["workers"][family]

    wan = uuid.UUID(instance["workers"]["wan"])
    await fleet.release(wan, action="stop")  # stopping the instance stops both of its workers
    async with db.session() as session:
        states = {
            r.runtime_family: r.state
            for r in (
                await session.execute(
                    sa.select(GpuWorker).where(
                        GpuWorker.external_id == instance["external_id"], GpuWorker.pool_id == "profile:th"
                    )
                )
            ).scalars()
        }
    assert states == {"wan": "stopped", "tts": "stopped"}
    await fleet.start(wan)
    async with db.session() as session:
        states = {
            r.runtime_family: r.state
            for r in (
                await session.execute(
                    sa.select(GpuWorker).where(
                        GpuWorker.external_id == instance["external_id"], GpuWorker.pool_id == "profile:th"
                    )
                )
            ).scalars()
        }
    assert states == {"wan": "provisioning", "tts": "provisioning"}
    await fleet.release(wan, action="terminate")
    assert provider.instances[instance["external_id"]].state == "terminated"

    with pytest.raises(FleetActionError, match="VRAM"):
        await fleet.provision_profile("th", _talking_head(vram_gb=24), provider="mock")
    with pytest.raises(FleetActionError, match="not configured"):
        await fleet.provision_profile("th", _talking_head(), provider="vast")


def test_family_provisions_are_sized_from_their_manifests() -> None:
    fleet = FleetManager(db=None, pools=[], providers={}, manifests=MANIFESTS, budget_daily_usd=0)  # type: ignore[arg-type]
    assert fleet.variant_disk_gb("wan", "infinitetalk", ["infinitetalk"]) == 160.0  # 86.92 × 1.15 + 60 → 160
    assert fleet.variant_disk_gb("cpu_model", None, ["mock_voice"]) is None  # nothing to fetch: the provider's own


async def test_prepare_requests_reach_the_worker_and_cancel_follows(db: Database) -> None:
    from ce_scheduler.service import worker_commands
    from ce_worker.protocol import StatusBody

    assert worker_commands({}, None) == []
    request = {"id": "r1", "models": ["m"], "warm": False}
    (command,) = worker_commands(request, None)
    assert (command.kind, command.id, command.models, command.warm) == ("prepare", "r1", ["m"], False)
    assert worker_commands(request, "r1") == []  # taken up
    assert worker_commands({**request, "cancel": True}, None) == []  # cancelled before it started
    assert worker_commands({**request, "cancel": True}, "r1")[0].kind == "cancel_prepare"

    clock = Clock()
    provider = _mock()
    fleet = _fleet(db, {"mock": provider}, clock=clock)
    sched = _scheduler(db)
    await _task(db)
    external = (await fleet.tick())[0].provisioned[0]
    registered = await _register(sched, provider.specs[external].env)
    worker_id = uuid.UUID(registered.worker_id)
    worker = await sched.authenticate(registered.token)
    async with db.transaction() as session:  # an empty queue: the lease answers with the command only
        await session.execute(sa.update(GpuTask).values(state="cancelled"))
        await session.execute(sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(prepare_request=request))
    reply = await sched.lease(worker, LeaseBody(adapters=["mock_voice"], wait_s=5))
    assert [c.kind for c in reply.commands] == ["prepare"] and not reply.tasks
    progress = {"m": {"state": "downloading", "bytes_done": 10, "bytes_total": 100}}
    status = await sched.status(worker, StatusBody(model_states=progress, prepare_seen="r1"))
    assert status.commands == []
    assert (await _worker(db, worker_id)).model_states == progress
    async with db.transaction() as session:
        await session.execute(
            sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(prepare_request={**request, "cancel": True})
        )
    status = await sched.status(worker, StatusBody(prepare_seen="r1"))
    assert [c.kind for c in status.commands] == ["cancel_prepare"]


async def test_a_rental_with_an_unknown_outcome_is_adopted_not_rented_twice(db: Database) -> None:
    from ce_gpu.provider import ProvisionOutcomeUnknown

    class TimesOut(ListingMock):
        async def provision(self, spec: ProvisionSpec) -> Any:
            await super().provision(spec)  # the rental went through…
            raise ProvisionOutcomeUnknown("timeout (the rental may have gone through)")  # …but the answer was lost

    clock = Clock()
    provider = TimesOut({"classes": {"mock_gpu": {"vram_gb": 96, "price_per_hour_usd": 0.5}}, "regions": ["local"]})
    fleet = _fleet(db, {"mock": provider}, clock=clock, pools=[_pool(max=1)])
    async with db.transaction() as session:  # mock instance ids restart per provider: retire earlier rows
        await session.execute(sa.update(GpuWorker).values(state="terminated"))
    await _task(db)
    decision = (await fleet.tick())[0]
    assert decision.provisioned == [] and "outcome unknown" in decision.attempts[-1]
    async with db.session() as session:
        (row,) = (
            await session.execute(
                sa.select(GpuWorker).where(GpuWorker.state == "provisioning", GpuWorker.pool_id == "sim")
            )
        ).scalars()
    assert row.external_id is None and "outcome unknown" in (row.last_error or "")
    assert len(provider.instances) == 1
    await fleet.tick()  # the sweep adopts the labeled instance; the provisioning row keeps the pool full
    assert len(provider.instances) == 1  # never a second rental
    assert (await _worker(db, row.id)).external_id == next(iter(provider.instances))
