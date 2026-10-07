"""Audit OUT-LABELS, end to end on the mock stack: the requests carry what the mock engines print, so
an outfit, a world or a camera position shows in the frame. The keyframe request sent no wardrobe or
world label, and the plate request sent `camera`/`time` while the mock image engine prints
`camera_position`/`time_of_day` — an outfit change was invisible in mock mode."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
import sqlalchemy as sa
from ce_db.models.assets import ExecutionNode, GpuTask
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]


async def _request(stack: Stack, version_id: object, node_key: str) -> dict[str, Any] | None:
    async with stack.exec.db.session() as session:
        query = (
            sa.select(GpuTask.payload)
            .join(ExecutionNode, ExecutionNode.id == GpuTask.node_id)
            .where(ExecutionNode.version_id == version_id, ExecutionNode.node_key == node_key)
        )
        payload = (await session.execute(query)).scalars().first()
    return dict(payload["request"]) if payload else None


async def test_the_keyframe_request_carries_the_outfit_and_world_labels(stack: Stack) -> None:
    spec = copy.deepcopy(example_spec_dict())
    spec["scenes"][0]["cast"][0]["wardrobe_version_id"] = str(ALEX.WARDROBE_NAVY_VERSION_ID)
    built, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), 300)
    assert result.state == "ready", (result.failed, result.statuses)
    keyframe = await _request(stack, built.version_id, "image.keyframe:sht_1")
    assert keyframe is not None, "the keyframe ran from cache: no request to inspect"
    assert keyframe["labels"]["wardrobe"] == "navy sweater"
    assert keyframe["labels"]["world"] == "Alex's home office"
