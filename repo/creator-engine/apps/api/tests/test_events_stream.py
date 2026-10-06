"""The SSE generator without infrastructure: bounded lifetime and survival of Valkey errors."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from ce_api.routers import events as events_router
from ce_obs.events import Event
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


class FakeBus:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.cursors: list[str] = []

    async def last_id(self, org_id: Any) -> str:
        return "5-0"

    async def oldest_id(self, org_id: Any) -> str | None:
        return "1-0"

    async def replay(self, org_id: Any, after_id: str, *, limit: int) -> list[Event]:
        return []

    async def wait(self, org_id: Any, after_id: str, *, block_ms: int) -> list[Event]:
        self.cursors.append(after_id)
        step = self.script.pop(0) if self.script else []
        if isinstance(step, Exception):
            raise step
        return list(step)


async def collect(bus: FakeBus, *, max_stream_s: int, limit: int = 50, last_event_id: str | None = None) -> list[str]:
    out: list[str] = []
    gen = events_router._stream(FakeRequest(), bus, uuid4(), None, last_event_id, 15, 100, max_stream_s)  # type: ignore[arg-type]
    async for chunk in gen:
        out.append(chunk.decode())
        if len(out) >= limit:
            break
    return out


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(events_router.asyncio, "sleep", instant)


async def test_valkey_errors_keep_the_stream_open_and_resume_from_the_cursor() -> None:
    event = Event(id="6-0", type="job.updated", data={"status": "succeeded"}, project_id=None)
    bus = FakeBus([RedisTimeoutError("slow"), RedisConnectionError("down"), [event]])
    chunks = await collect(bus, max_stream_s=3600, limit=4)
    assert chunks[0].startswith(": connected") and "id: 5-0" in chunks[0]
    assert chunks[1] == ": keepalive\n\n" and chunks[2] == ": keepalive\n\n"
    assert "event: job.updated" in chunks[3] and "id: 6-0" in chunks[3]
    assert bus.cursors == ["5-0", "5-0", "5-0"]  # no event lost or skipped across the errors


async def test_stream_ends_after_its_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = [0.0, 0.0, 1.0, 2.0]

    def monotonic() -> float:
        return ticks.pop(0) if ticks else 10_000.0

    monkeypatch.setattr(events_router.time, "monotonic", monotonic)
    chunks = await collect(FakeBus([]), max_stream_s=900)
    assert chunks[-1].startswith(": stream lifetime reached") and "id: 5-0" in chunks[-1]
