"""Phase 4 end to end, in one process on the Compose infrastructure, through the HTTP API:

text → `POST /v1/projects/{id}/videos` → `PlanVideoWorkflow` (Director stages 1–11, fixture LLM)
→ `PrevizWorkflow` (mock TTS, verification, alignment, plates, keyframes) → `previz_ready` with
measured timings → `:approve` → `GenerateVersionWorkflow` → `ready`, reusing the previz nodes
from the cache, with usage events written for the repetition guard.

It also checks I7 across real planning: a memory change after planning reaches neither the
planned spec nor a replan that reuses the pinned snapshots; `refresh_memory` takes it in.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.services import build_services
from ce_api.testing import ApiHarness, ApiTenant
from ce_db.models.memory import MemorySnapshot
from ce_db.models.videos import Render, VideoVersion
from ce_testing.fixtures import ALEX
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]

AI_AGENTS = "Create a 30-second TikTok explaining why most people misunderstand AI agents."
# The planned 30-second video has a dozen shots; since Phase 7 each gets camera and realism post at
# 1080×1920 (deterministic FFmpeg work), which takes several minutes on 2 vCPUs.
TIMEOUT_S = 600


@pytest_asyncio.fixture
async def api(stack: Stack) -> AsyncIterator[ApiHarness]:
    """The API on the stack's configuration: its workflow starts reach the stack's workers."""
    harness = ApiHarness(build_services(stack.effective, pool_size=4))
    harness.services.workflows.before_start = None  # the stack runs the orchestrator worker
    yield harness
    await harness.aclose()


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def last_workflow(api: ApiHarness) -> Any:
    handle = api.services.workflows.handles[-1]
    return await asyncio.wait_for(handle.result(), TIMEOUT_S)


async def plan_and_previz(api: ApiHarness, tenant: ApiTenant, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await tenant.client.post(path, json=body, headers=key())
    assert response.status_code == 202, response.text
    result = await last_workflow(api)
    assert result["plan"]["status"] == "succeeded", result
    assert result["previz"] == {"state": "previz_ready", "failed": []}, result
    accepted: dict[str, Any] = response.json()
    return accepted


async def snapshot_items(stack: Stack, version_id: str) -> list[dict[str, Any]]:
    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, uuid.UUID(version_id))
        pin = version.spec["memory"]["snapshots"][0]["snapshot_id"]
        row = await session.get_one(MemorySnapshot, uuid.UUID(pin))
        return list(row.items)


async def test_text_to_previz_to_approved_video(stack: Stack, api: ApiHarness) -> None:
    editor = await api.add_member(ALEX.ORG_ID, "editor")
    project = (await editor.client.post("/v1/projects", json={"name": "Phase 4"})).json()["id"]

    planned = await plan_and_previz(api, editor, f"/v1/projects/{project}/videos", {"input": AI_AGENTS})
    version_id = planned["version_id"]
    previz = (await editor.client.get(f"/v1/versions/{version_id}/previz")).json()
    assert previz["state"] == "previz_ready" and previz["planner"] == "llm"
    assert previz["timing_source"] == "measured" and previz["blocking"] == []
    report = previz["plan_report"]
    assert report["state_timings"] and all(t["end_s"] > t["start_s"] for t in report["state_timings"])
    assert report["estimated_duration_s"] > 0 and previz["cost_estimate_usd"] is not None
    intent = (await editor.client.get(f"/v1/versions/{version_id}/intent")).json()
    assert any(s["decisions"] for s in intent["scenes"])
    assert report["event_timings"] and all(e["at_s"] <= report["estimated_duration_s"] for e in report["event_timings"])
    storyboard = (await editor.client.get(f"/v1/versions/{version_id}/storyboard")).json()
    talking = [s for s in storyboard["shots"] if s["type"] == "talking_head"]
    assert talking and all(s["keyframe"] for s in talking)  # previz made one keyframe per talking shot
    before = await snapshot_items(stack, version_id)

    # I7: memory written after planning does not reach the pinned snapshot or a reusing replan
    fact = {"subject": "Alex", "predicate": "drinks", "object": "oat flat whites"}
    added = await editor.client.post(
        f"/v1/creators/{ALEX.CREATOR_ID}/memory", json={"kind": "persona_fact", "value": fact}
    )
    assert added.status_code == 201, added.text
    reused = await plan_and_previz(api, editor, f"/v1/versions/{version_id}:replan", {"instruction": "punchier hook"})
    assert await snapshot_items(stack, version_id) == before
    assert await snapshot_items(stack, reused["version_id"]) == before
    refreshed = await plan_and_previz(api, editor, f"/v1/versions/{version_id}:replan", {"refresh_memory": True})
    fresh = await snapshot_items(stack, refreshed["version_id"])
    assert any(item.get("item_id") == added.json()["id"] for item in fresh)

    # approval → generation, which reuses what previz produced
    approved = await editor.client.post(f"/v1/versions/{version_id}:approve", json={}, headers=key())
    assert approved.status_code == 202, approved.text
    built = await last_workflow(api)
    assert built["state"] == "ready", (built["failed"], built["statuses"])
    reused_nodes = {k for k, s in built["statuses"].items() if s == "cached"}
    assert any(k.startswith("tts.segment:") for k in reused_nodes)
    assert any(k.startswith("align.segment:") for k in reused_nodes)
    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, uuid.UUID(version_id))
        render = (
            await session.execute(sa.select(Render).where(Render.version_id == version.id, Render.is_proxy.is_(False)))
        ).scalar_one()
    assert version.state == "ready" and render.status == "ready"
    usage = (await editor.client.get(f"/v1/creators/{ALEX.CREATOR_ID}/usage")).json()
    assert any(u["video_id"] == planned["video_id"] for u in usage)
