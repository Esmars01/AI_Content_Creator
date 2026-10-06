"""`ResearchIngestWorkflow` (§9 `research_ingest`, Phase 12, ADR 0058): a persistent research source
→ text → facts → embeddings.

Stages (studio loop, ADR 0055):
- `research_ingest/start`: read the document — a URL through the **SSRF guard** with the ingestion
  limits (`research.ingest.fetch`: PDFs allowed, larger bodies), an uploaded asset from storage, or
  a note's text — extract plain text (`ce_research.extract`: HTML, PDF, DOCX, SRT/VTT transcripts,
  text), cut it into facts with character spans (`ce_research.ingest`), replace the source's facts
  and mark it `ingested`; then embed the facts (`embed.text` model calls, pinned to the route
  `ce_exec.embeddings` chooses);
- `research_ingest/embed_store`: store the vectors with their model.

Everything read is **data** (I10): it reaches prompts only inside data blocks, and nothing in a
document can change what the system does. A refused fetch (SSRF guard), an unreadable document
or an empty one marks the source `failed` with the reason, and the job fails with it.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_db.models.assets import Asset
from ce_db.models.research import ResearchFact, ResearchSource
from ce_obs import get_logger
from ce_research.extract import ExtractError, ExtractLimits, extract
from ce_research.ingest import facts_from_text
from ce_research.ssrf import FetchError, GuardedFetcher, SSRFBlocked

from ce_exec.context import ExecServices
from ce_exec.embeddings import embed_request, embedding_route, model_string
from ce_exec.studio import ModelCall, StudioContext, StudioError, StudioStep, stage

__all__ = ["research_embed_store", "research_start"]

_log = get_logger("ce.exec.research")


async def _fail(svc: ExecServices, org_id: UUID, source_id: UUID, code: str, message: str) -> StudioError:
    async with svc.db.transaction() as session:
        await session.execute(
            sa.update(ResearchSource)
            .where(ResearchSource.org_id == org_id, ResearchSource.id == source_id)
            .values(status="failed", error={"code": code, "message": message[:1000]})
        )
    return StudioError(f"{code}: {message}"[:2000])


async def _document(svc: ExecServices, source: ResearchSource) -> tuple[bytes, str, dict[str, Any]]:
    """(bytes, media type, fetch facts) of the source's document."""
    ingest = svc.bundle.app.research.ingest
    if source.kind == "url":
        if not source.uri:
            raise StudioError("a url source needs a uri")
        fetcher = GuardedFetcher(ingest.fetch, transport=svc.fetch_transport, resolver=svc.fetch_resolver)
        document = await fetcher.fetch(source.uri)
        return (
            document.body or document.text.encode("utf-8"),
            document.content_type,
            {"final_url": document.final_url, "address": document.address, "redirects": document.redirects},
        )
    if source.asset_id is not None:
        async with svc.db.session() as session:
            asset = (
                await session.execute(
                    sa.select(Asset).where(Asset.org_id == source.org_id, Asset.id == source.asset_id)
                )
            ).scalar_one_or_none()
        if asset is None or asset.status != "ready":
            raise StudioError("the source's asset is not a ready asset of this organization")
        if int(asset.bytes or 0) > ingest.max_upload_bytes:
            raise StudioError(f"the document is larger than {ingest.max_upload_bytes} bytes")
        data = await svc.storage.get(svc.settings.s3_bucket_assets, asset.storage_key)
        return data, asset.mime, {"asset_id": str(asset.id)}
    if source.note_text is not None:
        return source.note_text.encode("utf-8"), "text/plain", {}
    raise StudioError("the source has no document (uri, asset or text)")


@stage("research_ingest", "start")
async def research_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, source_id = UUID(ctx.org_id), UUID(ctx.target_id)
    ingest = svc.bundle.app.research.ingest
    async with svc.db.session() as session:
        source = (
            await session.execute(
                sa.select(ResearchSource).where(ResearchSource.org_id == org_id, ResearchSource.id == source_id)
            )
        ).scalar_one_or_none()
    if source is None:
        raise StudioError("the research source does not exist")
    try:
        raw, mime, fetched = await _document(svc, source)
    except SSRFBlocked as exc:
        raise await _fail(svc, org_id, source_id, "ssrf_blocked", str(exc)) from exc
    except FetchError as exc:
        raise await _fail(svc, org_id, source_id, "fetch_failed", str(exc)) from exc
    except StudioError as exc:
        raise await _fail(svc, org_id, source_id, "unreadable", str(exc)) from exc
    limits = ExtractLimits(
        max_chars=ingest.max_chars, max_pdf_pages=ingest.max_pdf_pages, max_archive_bytes=ingest.max_archive_bytes
    )
    try:
        extracted = extract(raw, kind=source.kind, mime=mime, limits=limits)
    except ExtractError as exc:
        raise await _fail(svc, org_id, source_id, "extract_failed", str(exc)) from exc
    facts = facts_from_text(source_id, extracted.text, words=ingest.fact_words, overlap=ingest.fact_overlap_words)
    truncated = len(facts) > ingest.max_facts
    facts = facts[: ingest.max_facts]
    digest = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
    warnings = list(extracted.warnings) + ([f"only the first {ingest.max_facts} facts were kept"] if truncated else [])
    async with svc.db.transaction() as session:
        row = await session.get_one(ResearchSource, source_id)
        await session.execute(
            sa.delete(ResearchFact).where(ResearchFact.org_id == org_id, ResearchFact.source_id == source_id)
        )
        for fact in facts:
            session.add(
                ResearchFact(
                    id=fact.id,
                    org_id=org_id,
                    source_id=source_id,
                    text=fact.text,
                    quote_span={"start": fact.start, "end": fact.end},
                    chunk_index=fact.index,
                )
            )
        row.status = "ingested"
        row.error = None
        row.mime = mime
        row.bytes = len(raw)
        row.content_hash = digest
        row.title = row.title or extracted.title or (fetched.get("final_url") or "")[:300]
        row.fetched_at = svc.clock()
        row.fact_count = len(facts)
        row.embedding_model = None
        row.extract = {
            **fetched,
            "characters": len(extracted.text),
            "pages": extracted.pages,
            "warnings": warnings,
        }
    result = {"source_id": str(source_id), "facts": len(facts), "characters": len(extracted.text)}
    decision = embedding_route(svc)
    if decision is None or not facts:
        return StudioStep(done=True, result={**result, "embedded": 0})
    size = int(ingest.embed_batch)
    calls: list[ModelCall] = []
    batches: list[dict[str, Any]] = []
    for start in range(0, len(facts), size):
        chunk = facts[start : start + size]
        key = f"research.embed:{len(calls)}"
        calls.append(
            ModelCall(
                key=key,
                capability="embed.text",
                adapter_id=decision.adapter_id,
                request=embed_request(svc, [f.text for f in chunk], language=source.language),
            )
        )
        batches.append({"key": key, "fact_ids": [str(f.id) for f in chunk]})
    return StudioStep(
        calls=calls,
        next="embed_store",
        progress=0.6,
        data={"batches": batches, "model": model_string(decision), "result": result},
    )


@stage("research_ingest", "embed_store")
async def research_embed_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, source_id = UUID(ctx.org_id), UUID(ctx.target_id)
    outputs = {o["key"]: o for o in data.get("outputs", [])}
    dim = int(svc.settings.embedding_dim)
    stored = 0
    async with svc.db.transaction() as session:
        for batch in data.get("batches", []):
            vectors = list(dict((outputs.get(batch["key"]) or {}).get("result") or {}).get("vectors") or [])
            if len(vectors) != len(batch["fact_ids"]) or any(len(v) != dim for v in vectors):
                raise StudioError(f"embed.text returned unusable vectors for {batch['key']} (EMBEDDING_DIM {dim})")
            for fact_id, vector in zip(batch["fact_ids"], vectors, strict=True):
                await session.execute(
                    sa.update(ResearchFact)
                    .where(ResearchFact.org_id == org_id, ResearchFact.id == UUID(fact_id))
                    .values(embedding=vector, embedding_model=data["model"])
                )
                stored += 1
        await session.execute(
            sa.update(ResearchSource)
            .where(ResearchSource.org_id == org_id, ResearchSource.id == source_id)
            .values(embedding_model=data["model"])
        )
    return StudioStep(
        done=True, result={**dict(data.get("result") or {}), "embedded": stored, "embedding_model": data["model"]}
    )
