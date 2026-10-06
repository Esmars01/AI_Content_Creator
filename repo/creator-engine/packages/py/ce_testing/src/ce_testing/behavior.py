"""Behavior fixtures (§15–§16): the example version's CBS, routes from `build_graph()` for a given
router catalog, whole-version compilation and coverage — all in memory, nothing persisted."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from ce_behavior.plan import compile_version, predicted_coverage, version_cbs
from ce_build import BuildOptions, build_graph
from ce_build.refs import BuildRefs
from ce_config.loader import ConfigBundle
from ce_core.behavior.cbs import CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_core.build import ExecutionGraph, RouteDecision
from ce_core.spec.videospec import VideoSpec
from ce_router import RouterCatalog

from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.fixtures import example_spec

__all__ = [
    "GLOBAL_ONLY",
    "SEGMENT_ONLY",
    "VersionBehavior",
    "bundle",
    "catalog_with",
    "example_cbs",
    "real_engine_catalog",
    "version_behavior",
]

GLOBAL_ONLY = frozenset({"mock_avatar_segment"})  # disabled adapters: only the global-prompt mock routes
SEGMENT_ONLY = frozenset({"mock_avatar_global"})  # only the segment-control mock routes


@lru_cache(maxsize=1)
def bundle() -> ConfigBundle:
    return config_bundle()


def catalog_with(disabled: frozenset[str] = frozenset(), **changes: object) -> RouterCatalog:
    return mock_catalog(bundle(), disabled=disabled, **changes)


def real_engine_catalog(adapter_ids: frozenset[str] | set[str], **changes: object) -> RouterCatalog:
    """A test catalog in which the given *real* engines route in place of the mocks of their
    capabilities, so the real compiler compiles for their declared behavior matrices (translator
    golden tests, Phase 8). Test-only: their status and validation are lifted to
    production/smoke_passed *in this catalog copy only* and an enabled `local` pool serves their
    families — nothing here claims a GPU validation."""
    from dataclasses import replace

    from ce_config.schemas import GpuPool

    base = mock_catalog(bundle())
    manifests = dict(base.manifests)
    capabilities: set[str] = set()
    families: set[str] = set()
    for adapter_id in adapter_ids:
        manifest = manifests[adapter_id]
        manifests[adapter_id] = manifest.model_copy(update={"status": "production", "validation": "smoke_passed"})
        capabilities |= {c.id for c in manifest.capabilities}
        families.add(manifest.runtime.family)
    displaced = {
        m.id
        for m in manifests.values()
        if m.mock and m.id not in adapter_ids and any(m.capability(c) is not None for c in capabilities)
    }
    pool = GpuPool(
        id="test_real_engines",
        gpu_classes=["rtx_pro_6000_96gb"],
        providers=["local"],
        families=sorted(families),  # type: ignore[arg-type]
        min=0,
        max=1,
        idle_timeout_s=60,
        target_latency_s=60,
        spot_ok=False,
        regions=["local"],
        enabled=True,
        autoscale=False,
    )
    catalog = replace(
        base,
        manifests=manifests,
        pools=[*base.pools, pool],
        gpu_providers=base.gpu_providers | {"local"},
        disabled=base.disabled | frozenset(displaced),
    )
    return replace(catalog, **changes) if changes else catalog  # type: ignore[arg-type]


def example_cbs(spec: VideoSpec | None = None, refs: BuildRefs | None = None) -> dict[str, CBSContent]:
    b = bundle()
    return version_cbs(spec or example_spec(), refs or example_build_refs(), b.vocab, b.app.behavior)


@dataclass(frozen=True)
class VersionBehavior:
    spec: VideoSpec
    graph: ExecutionGraph
    routes: Mapping[str, RouteDecision]
    cbs: Mapping[str, CBSContent]
    compiled: list[CompiledBehavior]
    report: BehaviorCoverageReport


def version_behavior(
    catalog: RouterCatalog,
    spec: VideoSpec | None = None,
    *,
    stage: Literal["plan_time", "build_time"] = "build_time",
    options: BuildOptions | None = None,
) -> VersionBehavior:
    """`build_graph()` and `compile()` for one version on one router configuration."""
    b = bundle()
    spec = spec or example_spec()
    refs = example_build_refs()
    graph = build_graph(spec, refs, b, catalog, options=options or BuildOptions())
    routes = {n.key: n.route for n in graph.nodes if n.route is not None}
    cbs = version_cbs(spec, refs, b.vocab, b.app.behavior)
    mode = b.modes.get(str(spec.meta.mode))
    compiled = compile_version(
        spec,
        cbs,
        routes,
        catalog.manifests,
        b.vocab,
        editorial_methods=frozenset(mode.editorial_methods) if mode else frozenset(),
        stage=stage,
    )
    report = predicted_coverage(cbs, compiled, b.vocab, stage="predicted" if stage == "plan_time" else "compiled")
    return VersionBehavior(spec=spec, graph=graph, routes=routes, cbs=cbs, compiled=compiled, report=report)
