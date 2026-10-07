"""Audit CAM-MOVES, end to end on the mock stack: a planned camera move that is not a punch-in (here a
pan) reaches the rendered shot. Before the fix `post.camera` read only punch-ins and handheld drift,
so a shot with a pan rebuilt (its spec changed) into a byte-identical mezzanine."""

from __future__ import annotations

import asyncio
import copy

import pytest
import sqlalchemy as sa
from ce_db.models.assets import Artifact, ExecutionNode
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]


async def _mezzanine(stack: Stack, version_id: object, node_key: str) -> str:
    async with stack.exec.db.session() as session:
        query = sa.select(ExecutionNode).where(
            ExecutionNode.version_id == version_id, ExecutionNode.node_key == node_key
        )
        node = (await session.execute(query)).scalar_one()
        artifact = await session.get_one(Artifact, node.artifact_ids[0])
        return artifact.sha256


async def test_a_planned_pan_changes_the_rendered_shot(stack: Stack) -> None:
    base = example_spec_dict()
    panned = copy.deepcopy(base)
    shot = panned["scenes"][0]["shots"][0]
    assert shot["key"] == "sht_1" and shot["type"] == "talking_head"
    shot["camera"]["moves"].append(
        {"key": "mv_pan", "type": "pan", "at": {"segment_key": "seg_1", "word": 1}, "scale": None,
         "transition": "smooth", "derived_from": []}
    )  # fmt: skip
    first, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, base)
    assert (await asyncio.wait_for(handle.result(), 300)).state == "ready"
    second, handle2 = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, panned)
    result = await asyncio.wait_for(handle2.result(), 300)
    assert result.state == "ready", (result.failed, result.statuses)
    assert result.statuses["post.camera:sht_1"] == "succeeded"  # rebuilt: its spec changed
    assert result.statuses["post.camera:sht_2"] == "cached"  # the other shot is untouched
    assert await _mezzanine(stack, second.version_id, "post.camera:sht_1") != await _mezzanine(
        stack, first.version_id, "post.camera:sht_1"
    )
