"""`embed.text` for the orchestrator (Phase 12, §7 "Embeddings", ADR 0057).

Embeddings index Creator Memory items, the usage log's hooks and research facts; they never are
the record. Every vector is stored with the **model string** of the route that made it
(`adapter:model@revision`), and vectors of different models are never compared: a fact embedded
by the mock and a query embedded by a real engine simply fall back to keyword matching.

Planning needs embeddings synchronously (the brief for memory ranking, candidate hooks for the
repetition guard, fact-check queries), so the text-embedding adapter runs **in process**
(`cpu_inproc`, like the image embedder). A routed adapter of another family is not called here:
the caller gets `None` and keeps its keyword path (ADR 0057). Jobs that embed in bulk
(`MemoryUpdateWorkflow`, `ResearchIngestWorkflow`) use the same request through this module.

`EMBEDDING_MODEL` (a model-registry key) pins the adapter whose manifest lists that model; else
the router chooses. `EMBEDDING_DIM` is fixed per database (pgvector column size): an adapter that
answers another dimension is an error, never silently padded.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ce_contracts.models import EmbeddingResult, TextEmbedRequest
from ce_obs import get_logger
from ce_storage.content import StorageRunContext

from ce_exec.context import ExecServices

__all__ = ["Embedded", "EmbeddingError", "embed_request", "embed_texts", "embedding_route", "model_string"]

_log = get_logger("ce.exec.embeddings")


class EmbeddingError(Exception):
    """The embedding adapter answered something unusable (wrong dimension, wrong count)."""


@dataclass(frozen=True)
class Embedded:
    vectors: list[list[float]]
    model: str  # adapter:model@revision — vectors are comparable only within one model string
    adapter_id: str
    mock: bool


def model_string(decision: Any) -> str:
    return f"{decision.adapter_id}:{decision.model_id}@{decision.revision}"


def embed_request(svc: ExecServices, texts: Sequence[str], *, language: str | None = None) -> dict[str, Any]:
    """The `embed.text` request (JSON) for these texts: cut to `embeddings.max_chars`, with the
    configured dimension as a label (adapters with a fixed dimension ignore it and are checked)."""
    limit = int(svc.bundle.app.embeddings.max_chars)
    return TextEmbedRequest(
        texts=[t[:limit] for t in texts],
        language=language,
        labels={"dim": str(int(svc.settings.embedding_dim))},
    ).model_dump(mode="json")


def embedding_route(svc: ExecServices, *, language: str | None = None, catalog: Any = None) -> Any | None:
    """The route of `embed.text` (pinned by `EMBEDDING_MODEL` when set), or None without one."""
    from ce_core.build import RouteDecision
    from ce_router import RouteRequest, RoutingError, route

    catalog = catalog or svc.catalog
    pinned_key = getattr(svc.settings, "embedding_model", None)
    if pinned_key:
        for manifest in catalog.manifests.values():
            if not any(c.id == "embed.text" for c in manifest.capabilities):
                continue
            model = next((m for m in manifest.models if m.key == pinned_key), None)
            if model is not None:
                return RouteDecision(
                    adapter_id=manifest.id,
                    model_id=model.key,
                    revision=model.source.revision,
                    reason="EMBEDDING_MODEL",
                )
        _log.warning("EMBEDDING_MODEL names no installed embed.text model", model=pinned_key)
        return None
    try:
        return route(RouteRequest(capability="embed.text", language=language), catalog)
    except RoutingError as exc:
        _log.info("no embed.text route", error=str(exc)[:200])
        return None


async def embed_texts(
    svc: ExecServices, texts: Sequence[str], *, language: str | None = None, catalog: Any = None
) -> Embedded | None:
    """Embeds `texts` in process; None when no in-process `embed.text` adapter is routable (the
    caller keeps its keyword path). Raises `EmbeddingError` on an unusable answer."""
    if not texts:
        return None
    decision = embedding_route(svc, language=language, catalog=catalog)
    if decision is None:
        return None
    manifest = (catalog or svc.catalog).manifests.get(decision.adapter_id)
    if manifest is None or manifest.runtime.family != "cpu_inproc":
        _log.info("embed.text adapter is not in-process; keyword path kept", adapter=decision.adapter_id)
        return None
    adapter = await svc.adapter(decision.adapter_id)
    dim = int(svc.settings.embedding_dim)
    batch = int(svc.bundle.app.embeddings.batch_size)
    vectors: list[list[float]] = []
    run = StorageRunContext(svc.content, svc.scratch("embed"), seed=0)
    try:
        for start in range(0, len(texts), batch):
            request = TextEmbedRequest.model_validate(
                embed_request(svc, texts[start : start + batch], language=language)
            )
            result: EmbeddingResult = await adapter.run("embed.text", request, run)
            if len(result.vectors) != len(request.texts):
                raise EmbeddingError(
                    f"{decision.adapter_id} returned {len(result.vectors)} vectors for {len(request.texts)} texts"
                )
            for vector in result.vectors:
                if len(vector) != dim:
                    raise EmbeddingError(
                        f"{decision.adapter_id} returned {len(vector)}-dimensional vectors; EMBEDDING_DIM is {dim}"
                    )
            vectors.extend([float(x) for x in v] for v in result.vectors)
    finally:
        shutil.rmtree(run.scratch_dir, ignore_errors=True)
    return Embedded(
        vectors=vectors, model=model_string(decision), adapter_id=decision.adapter_id, mock=bool(manifest.mock)
    )
