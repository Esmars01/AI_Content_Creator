"""Creator Memory (§18, §30): authoring, conflicts, actions, history, hard delete with tombstone."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from ce_api.events import EventType
from ce_api.testing import ApiHarness, ApiTenant
from ce_testing.fixtures import alex_creator_dna

pytestmark = pytest.mark.infra


async def creator(tenant: ApiTenant) -> str:
    response = await tenant.client.post(
        "/v1/creators", json={"name": "Mo", "dna": alex_creator_dna().model_dump(mode="json")}
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def fact(obj: str) -> dict[str, Any]:
    return {
        "kind": "persona_fact",
        "value": {"subject": "Mo", "predicate": "lives_in", "object": obj},
        "text": f"Mo lives in {obj}.",
    }


async def test_authored_items_and_duplicates(owner: ApiTenant) -> None:
    creator_id = await creator(owner)
    created = await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Lisbon"))
    assert created.status_code == 201, created.text
    item = created.json()
    assert (item["status"], item["source"], item["confidence"], item["category"]) == (
        "active",
        {"type": "authored"},
        1.0,
        "persona_fact",
    )
    assert item["key"] == "mo|lives_in" and item["conflict_state"] == "none"
    same = await owner.client.post(f"/v1/creators/{creator_id}/memory", json={**fact("Lisbon"), "text": "again"})
    assert same.status_code == 409


async def test_invalid_memory_values(owner: ApiTenant) -> None:
    creator_id = await creator(owner)
    unknown = await owner.client.post(f"/v1/creators/{creator_id}/memory", json={"kind": "superpower", "value": {}})
    assert unknown.status_code == 422 and unknown.json()["issues"][0]["path"] == "/kind"
    bad = await owner.client.post(
        f"/v1/creators/{creator_id}/memory", json={"kind": "persona_fact", "value": {"subject": "Mo"}}
    )
    assert bad.status_code == 422 and {i["path"] for i in bad.json()["issues"]} == {"/value/predicate", "/value/object"}


async def test_conflicts_are_flagged_and_resolved(harness: ApiHarness, owner: ApiTenant) -> None:
    creator_id = await creator(owner)
    since = await harness.services.events.last_id(owner.org_id)
    a = (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Lisbon"))).json()
    b = (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Porto"))).json()
    assert b["conflict_state"] == "unresolved" and b["conflict_ids"] == [a["id"]]
    a = (await owner.client.get(f"/v1/creators/{creator_id}/memory", params={"q": "Lisbon"})).json()["items"][0]
    assert a["conflict_state"] == "unresolved" and a["conflict_ids"] == [b["id"]]
    events = await harness.services.events.replay(owner.org_id, since, limit=10)
    assert [(e.type, sorted(e.data["memory_item_ids"])) for e in events] == [
        (EventType.MEMORY_CONFLICT.value, sorted([a["id"], b["id"]]))
    ]
    resolved = await owner.client.patch(
        f"/v1/memory-items/{b['id']}", json={"action": "resolve_conflict", "keep": a["id"]}
    )
    assert resolved.status_code == 200, resolved.text
    assert (resolved.json()["status"], resolved.json()["superseded_by_id"], resolved.json()["conflict_state"]) == (
        "superseded",
        a["id"],
        "resolved",
    )
    kept = (await owner.client.get(f"/v1/creators/{creator_id}/memory", params={"status": "active"})).json()["items"]
    assert [(i["id"], i["conflict_state"]) for i in kept] == [(a["id"], "resolved")]
    # the superseded value can be authored again later: uniqueness is among live items only
    assert (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Porto"))).status_code == 201


async def test_actions_and_history(owner: ApiTenant) -> None:
    creator_id = await creator(owner)
    item = (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Rome"))).json()
    url = f"/v1/memory-items/{item['id']}"
    assert (await owner.client.patch(url, json={"action": "pin"})).json()["pinned"] is True
    assert (await owner.client.patch(url, json={"action": "activate"})).status_code == 409  # already active
    edited = await owner.client.patch(
        url,
        json={
            "action": "edit",
            "value": {"subject": "Mo", "predicate": "lives_in", "object": "Milan"},
            "text": "Mo moved to Milan.",
        },
    )
    assert edited.status_code == 200
    assert (edited.json()["value"]["object"], edited.json()["text"]) == ("Milan", "Mo moved to Milan.")
    assert edited.json()["key"] == item["key"]
    forgotten = await owner.client.patch(url, json={"action": "forget"})
    assert (forgotten.json()["status"], forgotten.json()["pinned"]) == ("forgotten", False)
    assert (await owner.client.patch(url, json={"action": "pin"})).status_code == 409
    history = (await owner.client.get(f"{url}/history")).json()
    assert [h["action"] for h in history] == [
        "memory_item.create",
        "memory_item.pin",
        "memory_item.edit",
        "memory_item.forget",
    ]
    assert history[2]["before"]["value"]["object"] == "Rome" and history[2]["after"]["value"]["object"] == "Milan"


async def test_hard_delete_leaves_a_tombstone(harness: ApiHarness, owner: ApiTenant) -> None:
    creator_id = await creator(owner)
    item = (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Oslo"))).json()
    response = await owner.client.delete(f"/v1/memory-items/{item['id']}")
    assert response.status_code == 202 and response.json()["memory_item_id"] == item["id"]
    await harness.services.drain()
    rows = (await owner.client.get(f"/v1/creators/{creator_id}/memory")).json()["items"]
    tomb = next(r for r in rows if r["id"] == item["id"])
    assert (tomb["value"], tomb["text"], tomb["status"]) == (None, None, "forgotten") and tomb["deleted_at"]
    assert (await owner.client.patch(f"/v1/memory-items/{item['id']}", json={"action": "pin"})).status_code == 404
    assert (await owner.client.delete(f"/v1/memory-items/{item['id']}")).status_code == 404
    assert "memory_item.delete" in [
        h["action"] for h in (await owner.client.get(f"/v1/memory-items/{item['id']}/history")).json()
    ]


async def test_supersede_requires_the_same_key(owner: ApiTenant) -> None:
    creator_id = await creator(owner)
    a = (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=fact("Bern"))).json()
    other = {"kind": "stance", "value": {"topic": "remote work", "position": "for", "strength": 0.7}}
    b = (await owner.client.post(f"/v1/creators/{creator_id}/memory", json=other)).json()
    assert (
        await owner.client.patch(f"/v1/memory-items/{a['id']}", json={"action": "supersede", "by": b["id"]})
    ).status_code == 422
    assert (
        await owner.client.patch(f"/v1/memory-items/{a['id']}", json={"action": "supersede", "by": str(uuid.uuid4())})
    ).status_code == 404
