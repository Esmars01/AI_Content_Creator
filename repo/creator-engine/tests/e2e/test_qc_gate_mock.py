"""Phase 11 DoD on the Compose infrastructure (§26, ADR 0056): failure injection proves that only
the failed nodes re-run, that budgets cap the retries, and that the version ends flagged with its
best attempt when the ladder runs out. QC failures are injected into the mock metrics
(`MOCK_QC_FAIL_NODES`: a node's gating metrics fail on its first n attempts)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from ce_build.seeds import attempt_seed
from ce_db.models.assets import Artifact, ExecutionNode, JobAttempt
from ce_db.models.behavior import QCReport
from ce_db.models.videos import BuildManifestEntry, Take, VideoVersion
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]


async def _build(stack: Stack, tier: str = "draft") -> tuple[Any, Any]:
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    spec["meta"]["quality_tier"] = tier
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), 400)
    return submitted, result


async def _attempts(stack: Stack, version_id: Any) -> dict[str, list[JobAttempt]]:
    async with stack.exec.db.session() as session:
        rows = (
            await session.execute(
                sa.select(ExecutionNode.node_key, JobAttempt)
                .join(JobAttempt, JobAttempt.node_id == ExecutionNode.id)
                .where(ExecutionNode.version_id == version_id)
                .order_by(JobAttempt.attempt_no)
            )
        ).all()
    out: dict[str, list[JobAttempt]] = {}
    for key, attempt in rows:
        out.setdefault(key, []).append(attempt)
    return out


async def _shot_report(stack: Stack, version_id: Any) -> dict[str, Any]:
    async with stack.exec.db.session() as session:
        report = (
            (
                await session.execute(
                    sa.select(QCReport).where(QCReport.version_id == version_id, QCReport.target_type == "shot")
                )
            )
            .scalars()
            .all()
        )
    by_shot = {r.checks.get("shot_key"): r for r in report}
    assert "sht_1" in by_shot, by_shot.keys()
    return {"verdict": by_shot["sht_1"].verdict, **by_shot["sht_1"].checks}


async def test_a_failed_take_reruns_only_its_own_nodes_then_passes(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_QC_FAIL_NODES", "qc.shot:sht_1:t1@1,qc.shot:sht_1:t2@1")
    submitted, result = await _build(stack)
    assert result.state == "ready", (
        result.failed,
        {k: v for k, v in result.statuses.items() if v not in ("succeeded", "cached")},
    )
    report = await _shot_report(stack, submitted.version_id)
    take = report["ladder"][0]["take"]  # the best-scoring failed take
    target = f"avatar.render:sht_1:c1:t{take}"
    attempts = await _attempts(stack, submitted.version_id)
    retried = sorted(k for k, rows in attempts.items() if any(a.reason != "initial" for a in rows))
    assert retried == [target], retried  # that take only; nothing else got a new attempt
    reasons = [a.reason for a in attempts[target]]
    assert reasons == ["initial", "qc_retry"]
    base = attempts[target][0].seed
    assert base is not None and attempts[target][1].seed == attempt_seed(base, 1)
    assert report["verdict"] == "pass"
    steps = [(h["step"], h["outcome"]) for h in report["ladder"]]
    assert steps == [("attempt_seed", "run"), ("pass", "passed")]
    async with stack.exec.db.session() as session:
        entry = (
            await session.execute(
                sa.select(BuildManifestEntry).where(
                    BuildManifestEntry.version_id == submitted.version_id, BuildManifestEntry.node_key == target
                )
            )
        ).scalar_one()
        node = (
            await session.execute(
                sa.select(ExecutionNode).where(
                    ExecutionNode.version_id == submitted.version_id, ExecutionNode.node_key == target
                )
            )
        ).scalar_one()
        states = dict(
            (
                await session.execute(
                    sa.select(Artifact.id, Artifact.qc_state).where(Artifact.produced_by_node_id == node.id)
                )
            ).all()
        )
        take = (
            await session.execute(
                sa.select(Take).where(Take.version_id == submitted.version_id, Take.take_key == f"tk_1_{take}")
            )
        ).scalar_one()
    # the BuildManifest pins the accepted attempt (its seed and artifact); the first one lost
    assert entry.effective_seed == attempt_seed(base, 1)
    assert entry.artifact_id is not None and states[entry.artifact_id] == "accepted"
    assert "qc_rejected" in states.values()
    assert len(take.artifact_ids) == 1 and states.get(take.artifact_ids[0]) == "accepted"


async def test_budgets_cap_the_retries_and_the_version_is_flagged_with_its_best_attempt(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_QC_FAIL_NODES", "qc.shot:sht_1:t1@99,qc.shot:sht_1:t2@99")
    submitted, result = await _build(stack)
    assert result.state == "needs_review", (result.failed, result.statuses)
    assert "needs_review" in {result.statuses["qc.shot:sht_1:t1"], result.statuses["qc.shot:sht_1:t2"]}
    budget = stack.effective.bundle.qc_tiers["draft"].budgets.retries_per_node
    attempts = await _attempts(stack, submitted.version_id)
    retries = [a for rows in attempts.values() for a in rows if a.reason != "initial"]
    assert len(retries) == budget and {a.reason for a in retries} <= {"qc_retry", "fallback"}
    report = await _shot_report(stack, submitted.version_id)
    assert report["verdict"] == "fail"
    assert report["ladder"][-1]["step"] == "needs_review"
    notes = " ".join(str(h.get("reason", "")) for h in report["ladder"])
    assert "budget" in notes
    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, submitted.version_id)
    assert version.state == "needs_review"


async def test_the_fallback_route_runs_after_the_seed_retry_in_the_final_tier(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # final: two re-runs per node — one attempt seed (still failing), then the route's fallback avatar
    # engine (the third attempt passes)
    monkeypatch.setenv("MOCK_QC_FAIL_NODES", "qc.shot:sht_1:t1@2,qc.shot:sht_1:t2@2")
    submitted, result = await _build(stack, tier="final")
    attempts = await _attempts(stack, submitted.version_id)
    reruns = {
        key: [a for a in rows if a.reason != "initial"]
        for key, rows in attempts.items()
        if key.startswith("avatar.render:sht_1:") and any(a.reason != "initial" for a in rows)
    }
    assert sorted(a.reason for rows in reruns.values() for a in rows) == ["fallback", "qc_retry"], reruns
    fallback = next(a for rows in reruns.values() for a in rows if a.reason == "fallback")
    fallback_key = next(k for k, rows in reruns.items() if fallback in rows)
    assert fallback.adapter_id != attempts[fallback_key][0].adapter_id
    assert result.state == "ready", (result.failed, result.statuses)
    report = await _shot_report(stack, submitted.version_id)
    assert [h["step"] for h in report["ladder"] if h.get("outcome") == "run"] == ["attempt_seed", "fallback_route"]
    async with stack.exec.db.session() as session:
        entry = (
            await session.execute(
                sa.select(BuildManifestEntry).where(
                    BuildManifestEntry.version_id == submitted.version_id, BuildManifestEntry.node_key == fallback_key
                )
            )
        ).scalar_one()
    assert entry.route is not None and entry.route["adapter_id"] == fallback.adapter_id


async def test_a_critical_vlm_defect_fails_the_take_in_the_final_tier_only(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_VLM_DEFECTS", "hands:critical@qc.shot:sht_2:t1")
    submitted, result = await _build(stack)
    assert result.state == "ready", (
        result.failed,
        {k: v for k, v in result.statuses.items() if v not in ("succeeded", "cached")},
    )  # draft: critical defects warn
    async with stack.exec.db.session() as session:
        reports = (
            (
                await session.execute(
                    sa.select(QCReport).where(
                        QCReport.version_id == submitted.version_id, QCReport.target_type == "take"
                    )
                )
            )
            .scalars()
            .all()
        )
    judged = [r for r in reports if (r.checks.get("vlm_judge") or {}).get("defects")]
    assert judged and judged[0].verdict == "warn"
