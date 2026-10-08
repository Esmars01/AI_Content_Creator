"""A job's GPU stage (cutover §5): waiting for a GPU, renting one, booting, queued behind a live worker,
held by a budget, downloading or loading the model, generating, uploading — from the queue, the fleet
and the worker's heartbeats only. A private database: the fleet's workers are global."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services
from ce_db import queue
from ce_db.models.assets import ExecutionNode, GenerationJob, GpuTask, JobAttempt
from ce_db.models.platform import GpuWorker, Plugin
from ce_testing.database import TestDatabase

pytestmark = [pytest.mark.infra]


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[ApiHarness]:
    database = await TestDatabase(prefix="ce_stages").create()
    services = build_test_services(database.url, storage="local_fs", storage_root=tmp_path / "storage")
    h = ApiHarness(services)
    try:
        yield h
    finally:
        await h.aclose()
        await database.drop()


async def _job(harness: ApiHarness, tenant: ApiTenant, keys: list[str]) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    tasks: dict[str, uuid.UUID] = {}
    async with harness.services.db.transaction() as session:
        session.add(Plugin(plugin_key="stage_avatar", version="1", kind="model", manifest={}, status="sandbox",
                           validation="untested_on_gpu", runtime_family="wan"))  # fmt: skip
        job = GenerationJob(org_id=tenant.org_id, kind="identity_pack", status="running",
                            target_type="appearance_version", target_id=uuid.uuid4())  # fmt: skip
        session.add(job)
        await session.flush()
        for key in keys:
            node = ExecutionNode(org_id=tenant.org_id, job_id=job.id, node_key=key, node_kind="avatar.render",
                                 status="queued")  # fmt: skip
            session.add(node)
            await session.flush()
            attempt = JobAttempt(org_id=tenant.org_id, node_id=node.id, attempt_no=1, reason="initial", seed=1)
            session.add(attempt)
            await session.flush()
            task = await queue.enqueue(
                session, org_id=tenant.org_id, node_id=node.id, attempt_id=attempt.id, capability="avatar.a2v",
                model_key="m", priority=50, vram_gb=0, est_seconds=1, constraints={"adapter_id": "stage_avatar"},
                payload={"adapter_id": "stage_avatar"},
            )  # fmt: skip
            tasks[key] = task
        return job.id, tasks


async def _stages(tenant: ApiTenant, job_id: uuid.UUID) -> tuple[str | None, dict[str, Any]]:
    body = (await tenant.client.get(f"/v1/jobs/{job_id}")).json()
    return body["stage"], {n["node_key"]: n["gpu"] for n in body["nodes"]}


async def test_stages_follow_the_queue_the_fleet_and_the_heartbeats(harness: ApiHarness) -> None:
    tenant = await harness.new_tenant("owner")
    job_id, tasks = await _job(harness, tenant, ["a", "b"])
    stage, nodes = await _stages(tenant, job_id)
    assert stage == "waiting_for_gpu" and nodes["a"]["stage"] == "waiting_for_gpu"
    assert nodes["a"]["label"] == "Waiting for a GPU" and nodes["a"]["runtime_family"] == "wan"

    async with harness.services.db.transaction() as session:
        worker = GpuWorker(runtime_family="wan", gpu_type="a100_80gb", state="provisioning", provider_kind="vast",
                           external_id="7000001")  # fmt: skip
        session.add(worker)
        await session.flush()
        worker_id = worker.id
    assert (await _stages(tenant, job_id))[1]["a"]["stage"] == "provisioning"
    async with harness.services.db.transaction() as session:
        await session.execute(
            sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(provider_status={"state": "running"})
        )
    assert (await _stages(tenant, job_id))[1]["a"]["stage"] == "booting"
    async with harness.services.db.transaction() as session:
        await session.execute(sa.update(GpuWorker).where(GpuWorker.id == worker_id).values(state="idle"))
        await session.execute(sa.update(GpuTask).where(GpuTask.id == tasks["b"]).values(held_reason="budget_daily"))
    stage, nodes = await _stages(tenant, job_id)
    assert nodes["a"]["stage"] == "queued"
    assert nodes["b"]["stage"] == "held" and nodes["b"]["held_reason"] == "budget_daily"
    assert stage == "held"  # the job says what blocks it first

    async with harness.services.db.transaction() as session:
        await session.execute(sa.update(GpuTask).where(GpuTask.id == tasks["b"]).values(held_reason=None))
        await session.execute(
            sa.update(GpuTask)
            .where(GpuTask.id == tasks["a"])
            .values(state="running", lease_worker_id=worker_id, phase="fetching_model", progress=0.25,
                    progress_message="infinitetalk-single", progress_detail={"bytes_done": 25, "bytes_total": 100,
                                                                             "eta_s": 30.0})
        )  # fmt: skip
    stage, nodes = await _stages(tenant, job_id)
    assert nodes["a"]["stage"] == "downloading_model" and nodes["a"]["bytes_total"] == 100 and nodes["a"]["eta_s"] == 30
    assert nodes["a"]["worker_id"] == str(worker_id) and stage == "downloading_model"
    for phase, expected in (("verifying_model", "verifying"), ("loading_model", "loading_model"),
                            ("generating", "generating"), ("uploading", "uploading"), (None, "running")):  # fmt: skip
        async with harness.services.db.transaction() as session:
            await session.execute(sa.update(GpuTask).where(GpuTask.id == tasks["a"]).values(phase=phase))
        assert (await _stages(tenant, job_id))[1]["a"]["stage"] == expected

    async with harness.services.db.transaction() as session:
        await session.execute(sa.update(ExecutionNode).where(ExecutionNode.job_id == job_id).values(status="succeeded"))
        await session.execute(sa.update(GenerationJob).where(GenerationJob.id == job_id).values(status="succeeded"))
    stage, nodes = await _stages(tenant, job_id)
    assert stage == "completed" and nodes["a"] == {"stage": "completed"}
