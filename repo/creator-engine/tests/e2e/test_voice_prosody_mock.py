"""Audit OUT-PROSODY, end to end on the mock stack: the cast member's per-video voice offset
(`cast[].voice_prosody`, §10.6) changes the voice. Before the fix it was hashed into the voice nodes'
keys — so they re-ran — but never applied: the re-synthesized speech was bit-identical."""

from __future__ import annotations

import asyncio
import copy

import pytest
import sqlalchemy as sa
from ce_db.models.assets import Artifact, ExecutionNode
from ce_render.ffmpeg import probe
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]


async def _artifact(stack: Stack, version_id: object, node_key: str) -> Artifact:
    async with stack.exec.db.session() as session:
        where = (ExecutionNode.version_id == version_id, ExecutionNode.node_key == node_key)
        node = (await session.execute(sa.select(ExecutionNode).where(*where))).scalar_one()
        found = await session.execute(sa.select(Artifact).where(Artifact.id.in_(node.artifact_ids)))
        return next(a for a in found.scalars() if a.kind == "audio")


async def test_a_faster_higher_voice_offset_changes_the_speech(stack: Stack, tmp_path: object) -> None:
    base = example_spec_dict()
    faster = copy.deepcopy(base)
    faster["cast"][0]["voice_prosody"] = {"rate": 1.3, "pitch_semitones": 3.0, "energy": 0.5}
    first, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, base)
    assert (await asyncio.wait_for(handle.result(), 300)).state == "ready"
    second, handle2 = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, faster)
    result = await asyncio.wait_for(handle2.result(), 300)
    assert result.state == "ready", (result.failed, result.statuses)
    assert result.statuses["tts.segment:seg_1"] == "succeeded"
    before = await _artifact(stack, first.version_id, "tts.segment:seg_1")
    after = await _artifact(stack, second.version_id, "tts.segment:seg_1")
    assert after.sha256 != before.sha256
    durations = []
    for artifact in (before, after):
        path = tmp_path / f"{artifact.sha256}.wav"  # type: ignore[operator]
        await stack.exec.storage.download(stack.effective.settings.s3_bucket_artifacts, artifact.storage_key, path)
        durations.append((await probe(path)).duration_s or 0.0)
    assert durations[1] < durations[0] * 0.9  # 1.3× the rate: clearly shorter speech
