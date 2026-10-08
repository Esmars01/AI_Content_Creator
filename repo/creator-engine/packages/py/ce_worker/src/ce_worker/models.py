"""Plugin manifests → model-cache entries (§24, §25).

A plugin's models and its fetchable dependencies (`huggingface`, `s3`, `url` sources) are ensured
in the worker's cache before `load()`; their local paths reach the adapter as
`LoadContext.config["model_paths"]` (model key → path, dependency role → path; read by
`ce_plugin_kit.engine.ModelPaths`). `git` and `builtin` sources are part of the worker image and
are not fetched. The cache key of a model is its manifest key (what the scheduler sees in
`cached_models`); a dependency's is `<model key>:<role>`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ce_worker.model_cache import FetchProgress, ModelCache

__all__ = ["ModelFetch", "ensure_plugin_models", "fetch_plan", "source_uri"]

FETCHABLE = frozenset({"huggingface", "s3", "url"})


@dataclass(frozen=True)
class ModelFetch:
    cache_key: str
    path_key: str  # the key in model_paths: the model key, or the dependency role
    uri: str
    files: tuple[str, ...]
    expected: dict[str, str]


def source_uri(source: Any) -> str | None:
    """The cache URI of a manifest source; None for sources the image provides."""
    if source.type == "huggingface" and source.repo:
        return f"hf://{source.repo}@{source.revision}"
    if source.type == "s3" and source.uri:
        return source.uri
    if source.type == "url" and source.uri:
        return "url://" + source.uri.split("://", 1)[-1].rstrip("/") + f"@{source.revision}"
    return None


def fetch_plan(manifest: Any) -> list[ModelFetch]:
    out: list[ModelFetch] = []
    for decl in manifest.models:
        uri = source_uri(decl.source)
        if uri is not None:
            out.append(ModelFetch(decl.key, decl.key, uri, tuple(decl.files), dict(decl.sha256)))
        for dep in decl.dependencies:
            if dep.source is None or dep.source.type not in FETCHABLE or not dep.role:
                continue
            dep_uri = source_uri(dep.source)
            if dep_uri is not None:
                out.append(ModelFetch(f"{decl.key}:{dep.role}", dep.role, dep_uri, tuple(dep.files), dict(dep.sha256)))
    return out


async def ensure_plugin_models(
    cache: ModelCache, manifest: Any, *, progress: Callable[[ModelFetch], FetchProgress] | None = None
) -> dict[str, str]:
    """Fetches (once per host) and verifies every model the plugin needs; returns `model_paths`.
    `progress` gives the progress object each entry's fetch reports into (status reports)."""
    paths: dict[str, str] = {}
    for item in fetch_plan(manifest):
        local = await cache.ensure(
            item.cache_key,
            item.uri,
            expected=item.expected or None,
            files=item.files,
            progress=progress(item) if progress is not None else None,
        )
        paths[item.path_key] = str(local)
    return paths
