"""SSE (§30): org and project scopes, Last-Event-ID replay, auth. Runs a real uvicorn server,
because in-process ASGI clients buffer streaming responses."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from ce_api.testing import ApiHarness, ApiTenant

pytestmark = pytest.mark.infra


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture
async def server(harness: ApiHarness) -> AsyncIterator[str]:
    port = free_port()
    config = uvicorn.Config(harness.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    while not instance.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    await asyncio.wait_for(task, 10)


async def read_events(response: httpx.Response, count: int, timeout: float = 10) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    async def collect() -> None:
        async for line in response.aiter_lines():
            if line == "":
                if "event" in current:
                    events.append(dict(current))
                    if len(events) == count:
                        return
                current.clear()
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(": ")
                current[field] = json.loads(value) if field == "data" else value

    await asyncio.wait_for(collect(), timeout)
    return events


def stream_client(base: str, tenant: ApiTenant) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, cookies={"ce_session": tenant.client.cookies["ce_session"]}, timeout=30)


async def test_org_stream_delivers_live_events(harness: ApiHarness, owner: ApiTenant, server: str) -> None:
    bus = harness.services.events
    async with stream_client(server, owner) as client, client.stream("GET", "/v1/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        await asyncio.sleep(0.3)  # the stream starts from "now"
        ids = [
            await bus.publish(owner.org_id, "job.updated", {"job_id": str(n), "status": "running"}) for n in range(3)
        ]
        events = await read_events(response, 3)
    assert [e["id"] for e in events] == ids
    assert [e["event"] for e in events] == ["job.updated"] * 3
    assert [e["data"]["job_id"] for e in events] == ["0", "1", "2"]


async def test_last_event_id_replays_missed_events(harness: ApiHarness, owner: ApiTenant, server: str) -> None:
    bus = harness.services.events
    first = await bus.publish(owner.org_id, "notification", {"n": 1})
    missed = [await bus.publish(owner.org_id, "notification", {"n": n}) for n in (2, 3)]
    async with (
        stream_client(server, owner) as client,
        client.stream("GET", "/v1/events", headers={"Last-Event-ID": first}) as response,
    ):
        events = await read_events(response, 2)
    assert [e["id"] for e in events] == missed and [e["data"]["n"] for e in events] == [2, 3]


async def test_project_scope_filters_and_checks_tenancy(harness: ApiHarness, owner: ApiTenant, server: str) -> None:
    bus = harness.services.events
    mine = (await owner.client.post("/v1/projects", json={"name": "Mine"})).json()["id"]
    other = (await owner.client.post("/v1/projects", json={"name": "Other"})).json()["id"]
    import uuid

    start = await bus.publish(owner.org_id, "notification", {"marker": True})
    await bus.publish(owner.org_id, "version.updated", {"p": "other"}, project_id=uuid.UUID(other))
    wanted = await bus.publish(owner.org_id, "version.updated", {"p": "mine"}, project_id=uuid.UUID(mine))
    async with stream_client(server, owner) as client:
        async with client.stream(
            "GET", "/v1/events", params={"scope": "project", "project_id": mine}, headers={"Last-Event-ID": start}
        ) as response:
            events = await read_events(response, 1)
        assert [(e["id"], e["data"]["p"]) for e in events] == [(wanted, "mine")]
        missing = await client.get("/v1/events", params={"scope": "project"})
        assert missing.status_code == 422
    stranger = await harness.new_tenant()
    async with stream_client(server, stranger) as client:
        response = await client.get("/v1/events", params={"scope": "project", "project_id": mine})
        assert response.status_code == 404  # another org's project does not exist for this caller
    async with httpx.AsyncClient(base_url=server) as anonymous:
        assert (await anonymous.get("/v1/events")).status_code == 401


async def test_events_never_cross_orgs(harness: ApiHarness, owner: ApiTenant, server: str) -> None:
    bus = harness.services.events
    stranger = await harness.new_tenant()
    async with stream_client(server, stranger) as client, client.stream("GET", "/v1/events") as response:
        await asyncio.sleep(0.3)
        await bus.publish(owner.org_id, "notification", {"secret": "org A"})
        own = await bus.publish(stranger.org_id, "notification", {"secret": "org B"})
        events = await read_events(response, 1)
    assert [(e["id"], e["data"]["secret"]) for e in events] == [(own, "org B")]


async def test_idle_stream_outlives_the_redis_socket_timeout(
    harness: ApiHarness, owner: ApiTenant, server: str
) -> None:
    """Regression: XREAD blocked 15 s on a client whose socket timeout is 5 s, so every quiet period
    of 5 s raised TimeoutError inside the generator and dropped the connection (audit S1)."""
    bus = harness.services.events
    async with stream_client(server, owner) as client, client.stream("GET", "/v1/events") as response:
        assert response.status_code == 200
        await asyncio.sleep(6.5)  # longer than redis-py's default 5 s socket timeout, nothing published
        sent = await bus.publish(owner.org_id, "job.updated", {"job_id": "late", "status": "succeeded"})
        [event] = await read_events(response, 1)
    assert event["id"] == sent
    assert event["data"]["job_id"] == "late"


async def test_fresh_connection_sends_its_cursor_as_event_id(
    harness: ApiHarness, owner: ApiTenant, server: str
) -> None:
    """Regression: a fresh stream started at the tail without telling the browser, so a reconnect
    before the first event restarted at the new tail and skipped what was published in between (S2)."""
    bus = harness.services.events
    before = await bus.publish(owner.org_id, "job.updated", {"job_id": "before"})
    async with stream_client(server, owner) as client, client.stream("GET", "/v1/events") as response:
        first: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                break
            first.append(line)
    assert "retry: 3000" in first
    assert f"id: {before}" in first
    # the browser resumes from that id: what was published while it was away is replayed
    missed = await bus.publish(owner.org_id, "job.updated", {"job_id": "while-away"})
    async with (
        stream_client(server, owner) as client,
        client.stream("GET", "/v1/events", headers={"Last-Event-ID": before}) as response,
    ):
        [event] = await read_events(response, 1)
    assert event["id"] == missed


async def test_malformed_last_event_id_reports_a_gap_and_keeps_streaming(
    harness: ApiHarness, owner: ApiTenant, server: str
) -> None:
    """Regression: an invalid Last-Event-ID went straight into XREAD, which errors, so the browser
    reconnected with the same header in a loop (S10)."""
    bus = harness.services.events
    async with (
        stream_client(server, owner) as client,
        client.stream("GET", "/v1/events", headers={"Last-Event-ID": "not-an-id"}) as response,
    ):
        reading = asyncio.create_task(read_events(response, 2))  # one iteration of the stream for both
        await asyncio.sleep(0.5)
        sent = await bus.publish(owner.org_id, "job.updated", {"job_id": "after-gap"})
        gap, event = await reading
    assert gap["event"] == "stream.gap" and gap["data"]["reason"] == "invalid_last_event_id"
    assert event["id"] == sent
