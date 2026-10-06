"""Org event streams over Valkey/Redis Streams (§30 SSE), shared by the API, orchestrator and scheduler.

One stream per org (`ce:events:<org_id>`); a project-scoped subscriber filters on the
`project_id` field. Entry ids are the SSE ids, so `Last-Event-ID` resumes exactly after the
last delivered event. Streams are capped by length (`events.stream_maxlen`) and by age
(`SSE_STREAM_RETENTION_S`, trimmed with `MINID` on every publish).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

__all__ = ["Event", "EventBus", "EventType"]


class EventType(StrEnum):
    """§30 event types."""

    JOB_UPDATED = "job.updated"
    NODE_UPDATED = "node.updated"
    VERSION_UPDATED = "version.updated"
    PREVIZ_READY = "previz.ready"
    EDIT_PROPOSED = "edit.proposed"
    RENDER_READY = "render.ready"
    QC_FLAGGED = "qc.flagged"
    COVERAGE_UPDATED = "coverage.updated"
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_CONFLICT = "memory.conflict"
    BUDGET_ALERT = "budget.alert"
    WORKER_UPDATED = "worker.updated"
    NOTIFICATION = "notification"


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    data: dict[str, Any]
    project_id: str | None


def _decode(entry_id: str, fields: Mapping[str, str]) -> Event:
    return Event(
        id=entry_id,
        type=fields.get("type", ""),
        data=json.loads(fields.get("data", "{}")),
        project_id=fields.get("project_id") or None,
    )


def _valid_id(value: str) -> bool:
    ms, sep, seq = value.partition("-")
    return ms.isdigit() and (not sep or seq.isdigit())


class EventBus:
    def __init__(self, redis: Redis, *, maxlen: int, retention_s: int, clock: Callable[[], datetime]) -> None:
        self.redis = redis
        self.maxlen = maxlen
        self.retention_ms = retention_s * 1000
        self.clock = clock

    @staticmethod
    def stream_key(org_id: UUID | str) -> str:
        return f"ce:events:{org_id}"

    async def publish(
        self, org_id: UUID, type: EventType | str, data: Mapping[str, Any], *, project_id: UUID | None = None
    ) -> str:
        key = self.stream_key(org_id)
        fields = {"type": str(type), "data": json.dumps(data, default=str), "project_id": str(project_id or "")}
        entry_id = str(await self.redis.xadd(key, fields, maxlen=self.maxlen, approximate=True))  # type: ignore[arg-type]
        min_id = int(self.clock().timestamp() * 1000) - self.retention_ms
        if min_id > 0:
            await self.redis.xtrim(key, minid=f"{min_id}-0", approximate=True)
        return entry_id

    async def oldest_id(self, org_id: UUID) -> str | None:
        entries: Any = await self.redis.xrange(self.stream_key(org_id), "-", "+", count=1)
        return str(entries[0][0]) if entries else None

    async def last_id(self, org_id: UUID) -> str:
        entries: Any = await self.redis.xrevrange(self.stream_key(org_id), "+", "-", count=1)
        return str(entries[0][0]) if entries else "0-0"

    async def replay(self, org_id: UUID, after_id: str, *, limit: int) -> list[Event]:
        """Events strictly after `after_id`, oldest first."""
        if not _valid_id(after_id):
            return []
        entries: Any = await self.redis.xrange(self.stream_key(org_id), f"({after_id}", "+", count=limit)
        return [_decode(str(i), f) for i, f in entries]

    async def wait(self, org_id: UUID, after_id: str, *, block_ms: int, count: int = 100) -> list[Event]:
        """Block up to `block_ms` for events after `after_id`."""
        result: Any = await self.redis.xread({self.stream_key(org_id): after_id}, block=block_ms, count=count)
        events: list[Event] = []
        for _stream, entries in result or []:
            events += [_decode(str(i), f) for i, f in entries]
        return events
