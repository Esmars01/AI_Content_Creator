"""Phase 9 end to end on the simulated provider: a build's queued GPU work makes the fleet provision
a host; the host (an in-process worker started from the environment the provider received) enrolls
with its one-time token, runs the build's tasks and, once idle, is terminated with its lifetime in
`fleet_costs`. No real provider is involved."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_config.schemas import FleetConfig, GpuPool
from ce_core.enums import RuntimeFamily
from ce_db.models.assets import ExecutionNode, JobAttempt
from ce_db.models.platform import CostLedger, FleetCost, GpuWorker
from ce_plugin_gpu_mock.provider import MockGPUProvider
from ce_scheduler.fleet import FleetManager
from ce_scheduler.providers import FleetProvider
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack, stack_env

pytestmark = [pytest.mark.infra]


@pytest_asyncio.fixture
async def bare_stack(e2e_db: TestDatabase) -> AsyncIterator[Stack]:
    """The stack without a static worker: only the fleet can bring capacity."""
    from ce_testing.seed import placeholder_objects

    async with Stack.run(stack_env(e2e_db.url), workers=0) as s:
        bucket = s.effective.settings.s3_bucket_assets
        for item in placeholder_objects():
            await s.exec.storage.put(bucket, item.key, item.data, content_type=item.mime)
        yield s


class Clock:
    offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


async def test_queued_work_provisions_an_enrolled_host_that_builds_then_scales_down(bare_stack: Stack) -> None:
    from ce_contracts.plugins import discover
    from ce_worker import WorkerRuntime, usable_registry
    from ce_worker.__main__ import config_from_env

    stack = bare_stack
    settings = stack.effective.settings
    provider = MockGPUProvider({"classes": {"mock_gpu": {"vram_gb": 96, "price_per_hour_usd": 0.5}}})
    pool = GpuPool(
        id="e2e", gpu_classes=["mock_gpu"], providers=["sim"], families=[RuntimeFamily.CPU_MODEL], min=0, max=1,
        idle_timeout_s=600, target_latency_s=30, spot_ok=True, regions=["local"], enabled=True, autoscale=True,
    )  # fmt: skip
    clock = Clock()
    fleet = FleetManager(
        db=stack.exec.db,
        pools=[pool],
        providers={"sim": FleetProvider(key="sim", provider=provider)},  # its own key: the app's fleet leaves it be
        manifests={p.id: p.manifest for p in discover(app_env="test", include_mocks=True).plugins.values()},
        budget_daily_usd=1_000.0,
        config=FleetConfig(provision_timeout_s=600),
        scheduler_url="http://scheduler.test",
        app_env=settings.app_env,
        clock=clock,
    )
    registry = usable_registry(
        discover(app_env=settings.app_env, include_mocks=True, families=["cpu_model"]),
        settings.cpu_real_engines,
        settings.model_cache_dir,
    )
    hosts: dict[str, tuple[WorkerRuntime, asyncio.Task[Any]]] = {}

    async def boot(external_id: str) -> None:
        """What the provider's host does: start `python -m ce_worker` with the environment it was given."""
        env = {**provider.specs[external_id].env, "MODEL_CACHE_DIR": settings.model_cache_dir}
        config = config_from_env(env)
        runtime = WorkerRuntime(config, registry, client=stack.scheduler_client())
        await runtime.register()
        hosts[external_id] = (runtime, asyncio.create_task(runtime.run_forever()))

    async def fleet_loop() -> None:
        while True:
            await fleet.tick()
            for external_id in list(provider.specs):
                if external_id not in hosts and provider.instances[external_id].state != "terminated":
                    await boot(external_id)
            await asyncio.sleep(0.5)

    loop = asyncio.create_task(fleet_loop())
    try:
        spec = example_spec_dict()
        spec["generation"]["seed_namespace"] = str(uuid.uuid4())  # a fresh cache: the work must run
        submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
        result = await asyncio.wait_for(handle.result(), 300)
        assert result.state == "ready", (result.failed, result.statuses)
        assert len(hosts) == 1, "one host was provisioned for the backlog (pool max 1)"
        (external_id,) = hosts
        async with stack.exec.db.session() as session:
            worker = (await session.execute(sa.select(GpuWorker).where(GpuWorker.pool_id == "e2e"))).scalar_one()
            ran_on = set(
                (
                    await session.execute(
                        sa.select(JobAttempt.worker_id)
                        .join(ExecutionNode, ExecutionNode.id == JobAttempt.node_id)
                        .where(ExecutionNode.version_id == submitted.version_id, JobAttempt.status == "succeeded")
                    )
                ).scalars()
            )
            billed = (
                await session.execute(sa.select(sa.func.count()).where(CostLedger.worker_id == worker.id))
            ).scalar_one()
        assert worker.external_id == external_id and worker.registered_at and worker.provisioned_at
        assert ran_on == {worker.id} and billed > 0  # the build's GPU tasks ran (and were billed) on the host

        clock.offset = timedelta(hours=1)  # idle beyond idle_timeout_s, no backlog
        cost = None
        for _ in range(80):  # terminate first, then the release and its cost are committed
            async with stack.exec.db.session() as session:
                cost = (
                    await session.execute(sa.select(FleetCost).where(FleetCost.worker_id == worker.id))
                ).scalar_one_or_none()
                released = await session.get_one(GpuWorker, worker.id)
            if cost is not None:
                break
            await asyncio.sleep(0.25)
        assert provider.instances[external_id].state == "terminated" and cost is not None
        assert released.state == "terminated" and released.terminated_at  # the row matches the provider
        assert cost.pool_id == "e2e" and cost.busy_seconds > 0 and cost.provisioned_seconds >= cost.busy_seconds
        assert float(cost.provisioned_usd) > 0
    finally:
        loop.cancel()
        with contextlib.suppress(BaseException):
            await loop
        for runtime, task in hosts.values():
            runtime.stopping.set()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            with contextlib.suppress(Exception):
                await runtime.aclose()
