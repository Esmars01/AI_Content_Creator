"""Phase 11 jobs on the Compose infrastructure (mock engines), through the HTTP API:

- a finished build starts `ConsistencyWorkflow` on its own; the report scores the creator's
  dimensions against the baselines (mock values labelled) and writes a rolling baseline;
- `CritiqueWorkflow` writes scores and findings; a finding with operations becomes an edit proposal
  that applies cleanly into a derived version (§26 "critique proposals apply cleanly");
- `BenchmarkWorkflow` runs the golden evaluation set on a mock engine next to the production
  default, stores checks and blind pairs, and the pairs' ratings decide `bench_passed`."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.services import build_services
from ce_api.testing import ApiHarness
from ce_db.models.assets import GenerationJob
from ce_db.models.creators import ConsistencyReport, CreatorBaseline
from ce_db.models.platform import Model, Plugin
from ce_db.models.videos import Critique
from ce_db.registry import sync_registry
from ce_qc.bench import blind_sides
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]

TIMEOUT_S = 600


@pytest_asyncio.fixture
async def api(stack: Stack) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(build_services(stack.effective, pool_size=4))
    harness.services.workflows.before_start = None  # the stack runs the orchestrator worker
    yield harness
    await harness.aclose()


async def _built(stack: Stack) -> tuple[Any, Any]:
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    assert result.state == "ready", (result.failed, result.statuses)
    return submitted, result


async def _job(stack: Stack, kind: str, version_id: Any) -> GenerationJob:
    for _ in range(240):
        async with stack.exec.db.session() as session:
            job = (
                await session.execute(
                    sa.select(GenerationJob).where(
                        GenerationJob.kind == kind, GenerationJob.video_version_id == version_id
                    )
                )
            ).scalars().first()  # fmt: skip
        if job is not None and job.status in ("succeeded", "failed"):
            return job
        await asyncio.sleep(1.0)
    raise AssertionError(f"no finished {kind} job for {version_id}")


async def test_a_built_version_gets_a_consistency_report(stack: Stack) -> None:
    submitted, _ = await _built(stack)
    job = await _job(stack, "consistency", submitted.version_id)
    assert job.status == "succeeded", job.error
    async with stack.exec.db.session() as session:
        reports = (
            await session.execute(
                sa.select(ConsistencyReport).where(ConsistencyReport.version_id == submitted.version_id)
            )
        ).scalars().all()  # fmt: skip
        rolling = (
            await session.execute(sa.select(CreatorBaseline).where(CreatorBaseline.source == "rolling"))
        ).scalars().all()  # fmt: skip
    assert len(reports) == 1
    dims = reports[0].metrics["dimensions"]
    assert set(dims) == set(stack.effective.bundle.qc_consistency.metrics)  # type: ignore[union-attr]
    assert reports[0].verdict in ("in_band", "warn", "out_of_band")
    features = reports[0].metrics["features"]
    assert "wpm" in features and features["wpm"]["value"] > 0  # speech style from the spoken segments
    assert any(r.stats.get("version_id") == str(submitted.version_id) for r in rolling)


async def test_a_critique_finding_applies_cleanly(stack: Stack, api: ApiHarness) -> None:
    submitted, _ = await _built(stack)
    editor = await api.add_member(ALEX.ORG_ID, "editor")
    run = await editor.client.post(f"/v1/versions/{submitted.version_id}:critique")
    assert run.status_code == 202, run.text
    job_id = run.json()["job_id"]
    await asyncio.wait_for(api.services.workflows.handles[-1].result(), TIMEOUT_S)
    job = (await editor.client.get(f"/v1/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", job.get("error")
    critiques = (await editor.client.get(f"/v1/versions/{submitted.version_id}/critiques")).json()
    critique = critiques[0]
    assert set(critique["scores"]) >= {"hook", "pacing", "realism", "behavior_believability", "_meta"}
    assert critique["scores"]["_meta"]["vlm_mock"] is True  # the VLM pass was a mock
    async with stack.exec.db.transaction() as session:  # a finding the critic would write for a weak shot
        row = await session.get_one(Critique, uuid.UUID(critique["id"]))
        row.findings = [
            *row.findings,
            {
                "id": "f_test",
                "category": "visual_quality",
                "issue": "shot sht_1 looks soft",
                "evidence": {"shot_key": "sht_1"},
                "proposed_ops": [
                    {"op": "regenerate", "scope": {"shot_keys": ["sht_1"]}, "components": ["avatar_video"]}
                ],
                "impact": "high",
            },
        ]
    proposed = await editor.client.post(f"/v1/critiques/{critique['id']}/findings/f_test:propose")
    assert proposed.status_code == 202, proposed.text
    await asyncio.wait_for(api.services.workflows.handles[-1].result(), TIMEOUT_S)
    edit_id = proposed.json()["edit_proposal_id"]
    edit = (await editor.client.get(f"/v1/edits/{edit_id}")).json()
    assert edit["status"] == "proposed", edit
    applied = await editor.client.post(f"/v1/edits/{edit_id}:apply", headers={"Idempotency-Key": str(uuid.uuid4())})
    assert applied.status_code == 202, applied.text
    for handle in list(api.services.workflows.handles[-1:]):
        await asyncio.wait_for(handle.result(), TIMEOUT_S)
    derived = applied.json()
    version = (await editor.client.get(f"/v1/versions/{derived['new_version_id']}")).json()
    assert version["parent_version_id"] == str(submitted.version_id)


async def test_a_benchmark_runs_the_eval_set_and_ratings_decide(stack: Stack, api: ApiHarness) -> None:
    admin = await api.add_member(ALEX.ORG_ID, "owner", is_platform_admin=True)
    async with stack.exec.db.transaction() as session:  # the registry rows of the installed mock engines
        await sync_registry(session, [p.manifest for p in stack.exec.registry.plugins.values()])
    async with stack.exec.db.session() as session:
        model_id = (
            await session.execute(
                sa.select(Model.id).join(Plugin, Plugin.id == Model.plugin_id).where(
                    Plugin.plugin_key == "mock_avatar_segment"
                )
            )
        ).scalar_one()  # fmt: skip
    run = await admin.client.post(f"/v1/admin/models/{model_id}:benchmark")
    assert run.status_code == 202, run.text
    await asyncio.wait_for(api.services.workflows.handles[-1].result(), TIMEOUT_S)
    job = (await admin.client.get(f"/v1/jobs/{run.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job.get("error")
    benchmarks = (await admin.client.get(f"/v1/models/{model_id}")).json()["benchmarks"]
    bench = next(b for b in benchmarks if b["metrics"].get("job_id") == run.json()["job_id"])
    assert bench["eval_set_version"] == "eval-v1" and bench["verdict"] == "pending"
    checks = bench["metrics"]["checks"]
    assert checks and all(c["passed"] for c in checks), checks
    assert bench["metrics"]["baselines"]["avatar.a2v"] == "mock_avatar_global"
    measured = bench["behavior_profile"]["measures"]
    assert any(run_.get("face", {}).get("measured") for runs in measured.values() for run_ in runs)
    pairs = (await admin.client.get(f"/v1/admin/benchmarks/{bench['id']}/pairs")).json()
    assert len(pairs) == bench["human_scores"]["pairs"] > 0
    decided = None
    for pair in pairs:  # the rater prefers the candidate, wherever it is shown
        left, _ = blind_sides(pair["id"], str(admin.user_id))
        response = await admin.client.post(
            f"/v1/admin/benchmarks/{bench['id']}/pairs/{pair['id']}:rate",
            json={"preferred": "left" if left == "a" else "right"},
        )
        assert response.status_code == 200, response.text
        decided = response.json()
    assert decided is not None and decided["verdict"] == "pass"
    model = (await admin.client.get(f"/v1/models/{model_id}")).json()
    assert model["validation"] == "bench_passed" and model["promotion_basis"] == "bench"
