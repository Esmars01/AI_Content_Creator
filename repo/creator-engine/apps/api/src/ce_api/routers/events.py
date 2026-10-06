"""Server-sent events (§30): `GET /v1/events?scope=org|project&project_id=`, resumable via `Last-Event-ID`.

The endpoint authorizes and validates with the request's database session, then returns a
stream that holds no database connection: events come from the org's Valkey stream only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from ce_core.errors import InvalidInputError, Issue
from ce_db.models.videos import Project
from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from ce_api.deps import DbSession, Reader, ServicesDep
from ce_api.events import Event, EventBus
from ce_api.versioning import get_scoped

router = APIRouter(tags=["events"])

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
) -> AsyncIterator[bytes]:
    def visible(event: Event) -> bool:
        return wanted is None or event.project_id == wanted

    cursor = last_event_id
    yield sse(comment="connected", retry=3000)
    if cursor:
        oldest = await bus.oldest_id(org_id)
        if oldest is not None and _before(cursor, oldest):
            yield sse(event="stream.gap", data={"after": cursor, "oldest": oldest})
        replayed = await bus.replay(org_id, cursor, limit=replay_max)
        for event in replayed:
            if visible(event):
                yield sse(id=event.id, event=event.type, data=event.data)
        cursor = replayed[-1].id if replayed else cursor
    else:
        cursor = await bus.last_id(org_id)
    while not await request.is_disconnected():
        fresh = await bus.wait(org_id, cursor, block_ms=keepalive_s * 1000)
        if not fresh:
            yield sse(comment="keepalive")
            continue
        for event in fresh:
            cursor = event.id
            if visible(event):
                yield sse(id=event.id, event=event.type, data=event.data)


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
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
