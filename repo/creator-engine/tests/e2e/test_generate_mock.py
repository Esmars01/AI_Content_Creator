"""Phase 2 DoD, in one process on the Compose infrastructure: the fixture spec becomes a playable
MP4 with captions and music on CPU in under three minutes; a second run is all cache hits; a
worker killed mid-task loses its lease and another worker completes the build."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from ce_db.models.assets import ExecutionNode, GpuTask, JobAttempt
from ce_db.models.videos import BuildManifestEntry, Render, Take, VideoVersion
from ce_render.ffmpeg import measure_loudness, probe
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]

DOD_SECONDS = 180


async def _render(stack: Stack, version_id: object) -> Render:
    async with stack.exec.db.session() as session:
        return (
            await session.execute(sa.select(Render).where(Render.version_id == version_id, Render.is_proxy.is_(False)))
        ).scalar_one()


async def test_fixture_spec_renders_then_second_run_is_all_cache_hits(stack: Stack, tmp_path: Path) -> None:
    spec = example_spec_dict()
    started = time.monotonic()
    first, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), DOD_SECONDS)
    elapsed = time.monotonic() - started
    assert result.state == "ready", (result.failed, result.statuses)
    assert elapsed < DOD_SECONDS
    assert set(result.statuses.values()) <= {"succeeded", "cached"}

    render = await _render(stack, first.version_id)
    assert render.status == "ready" and render.provenance_mode == "mock_dev" and render.artifact_id
    async with stack.exec.db.session() as session:
        artifact_key = (
            await session.execute(
                sa.text("select storage_key from artifacts where id = :id"), {"id": render.artifact_id}
            )
        ).scalar_one()
        version = await session.get_one(VideoVersion, first.version_id)
        manifest_rows = (
            await session.execute(sa.select(sa.func.count()).where(BuildManifestEntry.version_id == first.version_id))
        ).scalar_one()
        takes = list((await session.execute(sa.select(Take).where(Take.version_id == first.version_id))).scalars())
    assert version.state == "ready" and version.frozen_at is not None
    assert manifest_rows >= len(result.statuses)
    assert {t.take_key for t in takes if t.shot_key == "sht_1"} == {"tk_1_1", "tk_1_2"}
    assert sum(t.selected for t in takes if t.shot_key == "sht_1") == 1

    target = tmp_path / "final.mp4"
    await stack.exec.storage.download(stack.effective.settings.s3_bucket_artifacts, artifact_key, target)
    if keep := os.environ.get("CE_E2E_ARTIFACTS"):  # inspection: keep the rendered file
        Path(keep).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, Path(keep) / "final.mp4")
    info = await probe(target)
    assert (info.width, info.height, info.fps) == (1080, 1920, 30.0)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert 3.0 < info.duration_s < 12.0
    loudness = await measure_loudness(target)
    assert loudness.integrated_lufs == pytest.approx(-14.0, abs=1.5)

    # SSE progress (§30): node and job events on the org stream, ending with the ready render.
    events = [
        e
        for e in await stack.exec.events.replay(ALEX.ORG_ID, "0-0", limit=100_000)
        if e.data.get("job_id") == str(first.job_id) or e.data.get("version_id") == str(first.version_id)
    ]
    nodes = {e.data["node_key"]: e.data["status"] for e in events if e.type == "node.updated"}
    assert set(nodes) == set(result.statuses) and set(nodes.values()) <= {"succeeded", "cached"}
    progress = [e.data["progress"] for e in events if e.type == "job.updated"]
    assert progress[-1] == 1.0 and len(set(progress)) > 5  # progress moves while nodes finish
    assert [e.data["status"] for e in events if e.type == "job.updated"][-1] == "succeeded"
    assert any(e.type == "render.ready" and e.data["preset_id"] == render.preset_id for e in events)

    second, handle2 = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result2 = await asyncio.wait_for(handle2.result(), DOD_SECONDS)
    assert result2.state == "ready"
    assert set(result2.statuses.values()) == {"cached"}, {k: v for k, v in result2.statuses.items() if v != "cached"}
    assert (await _render(stack, second.version_id)).artifact_id == render.artifact_id
    async with stack.exec.db.session() as session:
        tasks = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(GpuTask)
                .join(ExecutionNode, ExecutionNode.id == GpuTask.node_id)
                .where(ExecutionNode.version_id == second.version_id)
            )
        ).scalar_one()
    assert tasks == 0  # nothing was sent to a worker


async def test_a_killed_worker_loses_its_lease_and_another_worker_finishes(stack: Stack) -> None:
    victim = stack.workers[0][0]
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())  # a fresh cache
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    for _ in range(2000):
        if victim.current is not None:
            break
        await asyncio.sleep(0.01)
    assert victim.current is not None, "the worker never started a task"
    await stack.kill_worker(victim)
    await stack.start_worker("rescuer")
    result = await asyncio.wait_for(handle.result(), DOD_SECONDS)
    assert result.state == "ready", (result.failed, result.statuses)
    async with stack.exec.db.session() as session:
        attempts = list(
            (
                await session.execute(
                    sa.select(JobAttempt)
                    .join(ExecutionNode, ExecutionNode.id == JobAttempt.node_id)
                    .where(ExecutionNode.version_id == submitted.version_id)
                )
            ).scalars()
        )
    expired = [a for a in attempts if a.error_class == "lease_expired"]
    assert expired, "the killed worker's lease never expired"
    retried = [a for a in attempts if a.node_id == expired[0].node_id and a.reason == "infra_retry"]
    assert retried and retried[0].status == "succeeded" and retried[0].seed == expired[0].seed


async def test_cancelling_a_build_cancels_its_version_and_gpu_tasks(stack: Stack) -> None:
    from ce_db.models.assets import GenerationJob
    from temporalio.client import WorkflowFailureError

    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)

    async def active_tasks() -> int:
        async with stack.exec.db.session() as session:
            return (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(GpuTask)
                    .join(ExecutionNode, ExecutionNode.id == GpuTask.node_id)
                    .where(
                        ExecutionNode.version_id == submitted.version_id,
                        GpuTask.state.in_(("queued", "leased", "running")),
                    )
                )
            ).scalar_one()

    for _ in range(1000):
        if await active_tasks():
            break
        await asyncio.sleep(0.02)
    await handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await asyncio.wait_for(handle.result(), 60)
    for _ in range(100):  # the scheduler's cancellation probe runs every temporal_heartbeat_s
        if await active_tasks() == 0:
            break
        await asyncio.sleep(0.2)
    assert await active_tasks() == 0
    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, submitted.version_id)
        job = await session.get_one(GenerationJob, submitted.job_id)
    assert version.state == "cancelled" and job.status == "cancelled"
