"""Server-sent events (§30): `GET /v1/events?scope=org|project&project_id=`, resumable via `Last-Event-ID`.

The endpoint authorizes and validates with the request's database session, then returns a
stream that holds no database connection: events come from the org's Valkey stream only.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from ce_core.errors import InvalidInputError, Issue
from ce_db.models.videos import Project
from ce_obs import get_logger
from ce_obs.events import valid_stream_id
from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ce_api.deps import DbSession, Reader, ServicesDep
from ce_api.events import Event, EventBus
from ce_api.versioning import get_scoped

router = APIRouter(tags=["events"])
_log = get_logger(__name__)

SSE_DOC = {
    200: {
        "description": (
            "An event stream. Each event has `id` (resume with `Last-Event-ID`), `event` (§30 type) and JSON `data`."
        ),
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
                "example": 'id: 1759500000000-0\nevent: job.updated\ndata: {"job_id": "…", "status": "running"}\n\n',
            }
        },
    }
}


def sse(
    *,
    data: Any = None,
    event: str | None = None,
    id: str | None = None,
    comment: str | None = None,
    retry: int | None = None,
) -> bytes:
    lines: list[str] = []
    if comment is not None:
        lines.append(f": {comment}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")
    if data is not None:
        lines += [f"data: {line}" for line in json.dumps(data, separators=(",", ":"), default=str).splitlines()]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _before(a: str, b: str) -> bool:
    """Stream id `a` is older than `b` and not its immediate predecessor (events may have been trimmed)."""
    try:
        ams, _, aseq = a.partition("-")
        bms, _, bseq = b.partition("-")
        pa, pb = (int(ams), int(aseq or 0)), (int(bms), int(bseq or 0))
    except ValueError:
        return False
    return pa < pb and not (pa[0] == pb[0] and pa[1] + 1 == pb[1])


async def _stream(
    request: Request,
    bus: EventBus,
    org_id: UUID,
    wanted: str | None,
    last_event_id: str | None,
    keepalive_s: int,
    replay_max: int,
    max_stream_s: int,
) -> AsyncIterator[bytes]:
    def visible(event: Event) -> bool:
        return wanted is None or event.project_id == wanted

    started = time.monotonic()
    cursor = last_event_id if last_event_id and valid_stream_id(last_event_id) else None
    if last_event_id and cursor is None:
        # a malformed Last-Event-ID would make every XREAD fail; say so and start from the tail
        yield sse(event="stream.gap", data={"after": last_event_id, "reason": "invalid_last_event_id"})
    if cursor:
        oldest = await bus.oldest_id(org_id)
        if oldest is not None and _before(cursor, oldest):
            yield sse(event="stream.gap", data={"after": cursor, "oldest": oldest})
        replayed = await bus.replay(org_id, cursor, limit=replay_max)
        # the cursor goes out first, so a reconnect during the replay resumes from it
        yield sse(comment="connected", retry=3000, id=cursor)
        for event in replayed:
            if visible(event):
                yield sse(id=event.id, event=event.type, data=event.data)
        cursor = replayed[-1].id if replayed else cursor
    else:
        cursor = await bus.last_id(org_id)
        # Sending the starting cursor as the event id makes the browser's reconnect carry it as
        # Last-Event-ID, so events published while it was disconnected are replayed, not skipped.
        yield sse(comment="connected", retry=3000, id=cursor)
    last_sent = time.monotonic()
    while not await request.is_disconnected():
        if time.monotonic() - started >= max_stream_s:
            # bounded lifetime: the browser reconnects with Last-Event-ID and is authorized again
            yield sse(comment="stream lifetime reached; reconnect", id=cursor)
            return
        try:
            fresh = await bus.wait(org_id, cursor, block_ms=keepalive_s * 1000)
        except (RedisTimeoutError, RedisConnectionError) as exc:
            # Valkey unreachable or slow: keep the HTTP stream open, back off, and retry from the cursor
            _log.warning("event stream read failed; retrying", org_id=str(org_id), error=str(exc))
            yield sse(comment="keepalive")
            await asyncio.sleep(1.0)
            continue
        if not fresh:
            if time.monotonic() - last_sent >= keepalive_s:
                yield sse(comment="keepalive")
                last_sent = time.monotonic()
            continue
        for event in fresh:
            cursor = event.id
            if visible(event):
                yield sse(id=event.id, event=event.type, data=event.data)
        last_sent = time.monotonic()


@router.get("/v1/events", response_class=StreamingResponse, responses=SSE_DOC)  # type: ignore[arg-type]
async def events(
    request: Request,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    scope: Literal["org", "project"] = "org",
    project_id: UUID | None = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    if scope == "project":
        if project_id is None:
            raise InvalidInputError("scope=project needs project_id", issues=[Issue("project_id", "required")])
        await get_scoped(session, Project, principal.ctx, project_id, "project")
    config = services.config.api
    body = _stream(
        request,
        services.events,
        principal.org_id,
        str(project_id) if scope == "project" else None,
        last_event_id,
        config.sse_keepalive_s,
        config.sse_replay_max,
        config.sse_max_stream_s,
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
