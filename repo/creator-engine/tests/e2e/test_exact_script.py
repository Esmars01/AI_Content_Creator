"""The exact-script verification loop (§21, Phase 7) through the workflow: a segment that does not
match its script is re-synthesized with attempt seeds up to the tier's per-node budget, then
flagged — the version ends `needs_review` with every artifact kept. Mock TTS misreads are injected
(`MOCK_TTS_MISREAD_RATE`); the real-engine run is `test_cpu_real.py`."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from ce_build.seeds import attempt_seed
from ce_db.models.assets import ExecutionNode, JobAttempt
from ce_db.models.videos import Render, VideoVersion
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]


async def test_misread_segments_are_retried_with_attempt_seeds_then_flagged(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_TTS_MISREAD_RATE", "1.0")  # every attempt says the last word wrong
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), 240)
    assert result.state == "needs_review", (result.failed, result.statuses)
    assert result.statuses["asr.verify:seg_1"] == "needs_review"
    assert result.statuses["asr.verify:seg_2"] == "needs_review"
    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, submitted.version_id)
        node = (
            await session.execute(
                sa.select(ExecutionNode).where(
                    ExecutionNode.version_id == submitted.version_id, ExecutionNode.node_key == "tts.segment:seg_1"
                )
            )
        ).scalar_one()
        attempts = list(
            (
                await session.execute(
                    sa.select(JobAttempt).where(JobAttempt.node_id == node.id).order_by(JobAttempt.attempt_no)
                )
            ).scalars()
        )
        render = (
            await session.execute(
                sa.select(Render).where(Render.version_id == submitted.version_id, Render.is_proxy.is_(False))
            )
        ).scalar_one()
    assert version.state == "needs_review"
    budget = stack.effective.bundle.qc_tiers["draft"].budgets.retries_per_node
    assert [a.reason for a in attempts] == ["initial"] + ["qc_retry"] * budget
    base = attempts[0].seed
    assert base is not None
    assert [a.seed for a in attempts] == [attempt_seed(base, n) for n in range(budget + 1)]
    assert render.status == "ready"  # flagged, not failed: the artifacts stay for review


async def test_matching_segments_pass_on_the_first_attempt(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_TTS_MISREAD_RATE", "0")
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), 240)
    assert result.state == "ready", (result.failed, result.statuses)
    async with stack.exec.db.session() as session:
        reasons = (
            (
                await session.execute(
                    sa.select(JobAttempt.reason)
                    .join(ExecutionNode, ExecutionNode.id == JobAttempt.node_id)
                    .where(ExecutionNode.version_id == submitted.version_id, ExecutionNode.node_kind == "tts.segment")
                )
            )
            .scalars()
            .all()
        )
    assert reasons and set(reasons) == {"initial"}
