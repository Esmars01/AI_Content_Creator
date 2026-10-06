"""Research sources and the claim ledger (§30 "Research", Phase 12, ADR 0058).

- `POST /v1/projects/{project_id}/sources` registers a source — a URL, an uploaded document asset
  (PDF, DOCX, HTML, text, SRT/VTT transcript) or a pasted note — and starts its `research_ingest`
  job (`202`). The API reads nothing itself: fetching (SSRF-guarded), extraction and embedding run
  in `ResearchIngestWorkflow` (§9). URLs are checked here without the network (scheme, port,
  credentials, blocked IP literals, loopback names) so an obviously refused URL never becomes a
  job; resolution and the address checks happen when it is fetched.
- Sources and their facts are data, never instructions (I10).
- `GET /v1/versions/{version_id}/claims` reads the claim ledger: one row per `(video, claim_key)`,
  with the latest verdict, the evidence quotes and their sources, and the closed-book flags.
- `POST /v1/claims/{claim_id}:override {reason}` overrides an unsupported or uncertain claim in an
  open-book video (audit-logged). Closed-book claims cannot be overridden: edit the script or add
  a source that supports it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue
from ce_db.models.assets import Asset
from ce_db.models.research import Claim, ResearchFact, ResearchSource
from ce_db.models.videos import Project, VideoVersion
from ce_research.extract import kind_for_mime
from ce_research.ssrf import GuardedFetcher, SSRFBlocked
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from ce_api.common import Page, audit, begin_idempotent, finish_idempotent, page
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.jobs import start_studio_job
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["research"])


class SourceCreate(Body):
    model_config = examples(
        [
            {"kind": "url", "uri": "https://example.com/report"},
            {"kind": "note", "title": "Interview notes", "text": "Our customers save 3 hours a week."},
            {"kind": "pdf", "asset_id": "0192f0a0-0000-7000-8000-000000000099"},
        ]
    )
    kind: Literal["url", "pdf", "doc", "note", "transcript"]
    uri: Annotated[str | None, Field(max_length=2048)] = None
    asset_id: UUID | None = None
    text: Annotated[str | None, Field(max_length=200_000)] = None
    title: Annotated[str, Field(max_length=300)] = ""
    language: Annotated[str | None, Field(max_length=35)] = None

    @model_validator(mode="after")
    def _one_document(self) -> SourceCreate:
        given = [x for x in (self.uri, self.asset_id, self.text) if x]
        if len(given) != 1:
            raise ValueError("give exactly one of uri, asset_id or text")
        if self.kind == "url" and not self.uri:
            raise ValueError("a url source needs uri")
        if self.kind != "url" and self.uri:
            raise ValueError("only url sources take a uri")
        if self.text is not None and self.kind not in ("note", "transcript"):
            raise ValueError("pasted text is a note or a transcript")
        return self


class SourceOut(Out):
    id: UUID
    project_id: UUID | None
    kind: str
    uri: str | None
    asset_id: UUID | None
    title: str
    trust: str
    status: str
    mime: str | None
    bytes: int | None
    language: str | None
    fact_count: int
    embedding_model: str | None
    extract: dict[str, Any]
    error: dict[str, Any] | None
    job_id: UUID | None
    fetched_at: datetime | None
    created_at: datetime


class FactOut(Out):
    id: UUID
    chunk_index: int
    text: str
    quote_span: dict[str, Any]
    embedding_model: str | None


class SourceDetail(SourceOut):
    facts: list[FactOut]
    next_cursor: str | None = None


class SourceAccepted(Out):
    source_id: UUID
    job_id: UUID
    status: str = "pending"


class ClaimOut(Out):
    id: UUID
    video_id: UUID
    claim_key: str
    segment_key: str | None
    text: str
    verdict: str
    confidence: float
    evidence: list[Any]
    evidence_fact_ids: list[UUID]
    reasons: list[Any]
    closed_book: bool
    blocking: bool
    overridable: bool
    detected: bool
    first_version_id: UUID
    last_version_id: UUID | None
    override_by: UUID | None
    override_reason: str | None
    override_at: datetime | None
    in_version: bool = Field(default=False, description="the claim is in the requested version's script")


class OverrideBody(Body):
    model_config = examples([{"reason": "The figure comes from our own customer survey (internal)."}])
    reason: Annotated[str, Field(min_length=3, max_length=2000)]


def _check_url(services: Any, uri: str) -> None:
    try:
        GuardedFetcher(services.config.research.ingest.fetch).precheck(uri)
    except SSRFBlocked as exc:
        raise InvalidInputError(
            "the URL cannot be fetched", issues=[Issue("ssrf_blocked", str(exc), path="/uri")]
        ) from exc


async def _start_ingest(
    session: Any, services: Any, background: BackgroundTasks, principal: Any, source: ResearchSource
) -> Any:
    job = await start_studio_job(
        session,
        services,
        background,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kind=JobKind.RESEARCH_INGEST,
        target_type="research_source",
        target_id=source.id,
    )
    source.job_id = job.id
    source.status = "pending"
    await session.flush()
    return job


@router.post("/v1/projects/{project_id}/sources", status_code=202, response_model=SourceAccepted)
async def create_source(
    project_id: UUID,
    body: SourceCreate,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Registers a research source and starts its ingestion (`research_ingest` job)."""
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    await get_scoped(session, Project, principal.ctx, project_id, "project")
    ingest = services.config.research.ingest
    mime: str | None = None
    if body.uri:
        _check_url(services, body.uri)
    if body.asset_id is not None:
        asset = await get_scoped(session, Asset, principal.ctx, body.asset_id, "asset")
        if asset.status != "ready":
            raise ConflictError(f"the asset is {asset.status}; wait until it is validated")
        kind = kind_for_mime(asset.mime)
        compatible = {"pdf": {"pdf"}, "doc": {"doc", "note"}, "note": {"note"}, "transcript": {"transcript", "note"}}
        if kind is None or kind not in compatible.get(body.kind, set()):
            raise InvalidInputError(
                f"a {body.kind} source cannot be read from {asset.mime}",
                issues=[Issue("unsupported_document", asset.mime, path="/asset_id")],
            )
        if int(asset.bytes or 0) > ingest.max_upload_bytes:
            raise InvalidInputError(
                "the document is too large", issues=[Issue("too_large", str(asset.bytes), path="/asset_id")]
            )
        mime = asset.mime
    source = ResearchSource(
        org_id=principal.org_id,
        project_id=project_id,
        kind=body.kind,
        uri=body.uri,
        asset_id=body.asset_id,
        title=body.title,
        trust="web" if body.kind == "url" else "user_provided",
        status="pending",
        mime=mime,
        language=body.language,
        note_text=body.text,
        created_by=principal.user_id,
    )
    session.add(source)
    await session.flush()
    job = await _start_ingest(session, services, background, principal, source)
    await audit(session, principal, "research_source.create", "research_source", source.id, request=request)
    accepted = SourceAccepted(source_id=source.id, job_id=job.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    return accepted


@router.get("/v1/projects/{project_id}/sources", response_model=Page[SourceOut])
async def list_sources(
    project_id: UUID,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    status: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[SourceOut]:
    await get_scoped(session, Project, principal.ctx, project_id, "project")
    query = sa.select(ResearchSource).where(
        ResearchSource.org_id == principal.org_id, ResearchSource.project_id == project_id
    )
    if status:
        query = query.where(ResearchSource.status == status)
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, ResearchSource.id, cursor=cursor, limit=size)
    return Page[SourceOut](items=[SourceOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/sources/{source_id}", response_model=SourceDetail)
async def get_source(
    source_id: UUID,
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> SourceDetail:
    """A source with its facts (each with its character span in the extracted text)."""
    source = await get_scoped(session, ResearchSource, principal.ctx, source_id, "research source")
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    query = sa.select(ResearchFact).where(ResearchFact.org_id == principal.org_id, ResearchFact.source_id == source.id)
    rows, next_cursor = await page(session, query, ResearchFact.id, cursor=cursor, limit=size)
    facts = sorted((FactOut.model_validate(r) for r in rows), key=lambda f: f.chunk_index)
    return SourceDetail(**SourceOut.model_validate(source).model_dump(), facts=facts, next_cursor=next_cursor)


@router.post("/v1/sources/{source_id}:reingest", status_code=202, response_model=SourceAccepted)
async def reingest_source(
    source_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> SourceAccepted:
    """Fetches and extracts the source again (a page changed, a failed fetch to retry)."""
    source = await lock_scoped(session, ResearchSource, principal.ctx, source_id, "research source")
    job = await _start_ingest(session, services, background, principal, source)
    await audit(session, principal, "research_source.reingest", "research_source", source.id, request=request)
    return SourceAccepted(source_id=source.id, job_id=job.id)


@router.delete("/v1/sources/{source_id}", status_code=204)
async def delete_source(source_id: UUID, request: Request, principal: Writer, session: DbSession) -> None:
    """Deletes the source and its facts. Plans that cited them keep their claim ledger quotes."""
    source = await lock_scoped(session, ResearchSource, principal.ctx, source_id, "research source")
    await audit(
        session,
        principal,
        "research_source.delete",
        "research_source",
        source.id,
        request=request,
        before={"kind": source.kind, "uri": source.uri, "title": source.title},
    )
    await session.delete(source)


# ====================================================================== claim ledger
@router.get("/v1/versions/{version_id}/claims", response_model=list[ClaimOut])
async def version_claims(version_id: UUID, principal: Reader, session: DbSession) -> list[ClaimOut]:
    """The claim ledger of the version's video (stable per `(video, claim_key)` across versions);
    `in_version` marks the claims this version's script carries."""
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    keys = {c.get("key") for c in ((version.spec or {}).get("research") or {}).get("claims", [])}
    rows = (
        (
            await session.execute(
                sa.select(Claim)
                .where(Claim.org_id == principal.org_id, Claim.video_id == version.video_id)
                .order_by(Claim.claim_key)
            )
        )
        .scalars()
        .all()
    )
    return [ClaimOut.model_validate(r).model_copy(update={"in_version": r.claim_key in keys}) for r in rows]


@router.post("/v1/claims/{claim_id}:override", response_model=ClaimOut)
async def override_claim(
    claim_id: UUID, body: OverrideBody, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> ClaimOut:
    """Overrides an unsupported or uncertain claim (open book only); approval and export accept it."""
    claim = await lock_scoped(session, Claim, principal.ctx, claim_id, "claim")
    if claim.verdict == "supported":
        raise ConflictError("the claim is supported; there is nothing to override")
    if claim.closed_book or not claim.overridable:
        raise ConflictError(
            "closed-book claims cannot be overridden: edit the script or add a source that supports the claim",
            claim_key=claim.claim_key,
        )
    before = {"override_by": str(claim.override_by) if claim.override_by else None, "reason": claim.override_reason}
    claim.override_by, claim.override_reason, claim.override_at = principal.user_id, body.reason, services.clock()
    await session.flush()
    await audit(
        session,
        principal,
        "claim.override",
        "claim",
        claim.id,
        request=request,
        before=before,
        after={"verdict": claim.verdict, "reason": body.reason, "text": claim.text},
    )
    await session.refresh(claim)
    return ClaimOut.model_validate(claim).model_copy(update={"in_version": True})
