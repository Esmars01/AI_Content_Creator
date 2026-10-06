"""BuildManifest records (§12.3) and parent builds for derived versions (§12.4).

`build_manifest_entries` holds one insert-only row per node key: its route, effective seed,
accepted artifact, config digests and impl_version. In-process QC metric routes are recorded as
extra route-only rows keyed `{node_key}/{capability}` (graph.metric_route_key), so a rebuild
from an empty cache reproduces them too.

A derived version needs its parent's graph to decide which nodes are clean. The graph is never
stored: `parent_build` re-derives it from the parent's spec on the parent's recorded routes
(`replay`), then restores the config digests and impl_versions the parent actually used, so a
config file or code change since then shows up as dirty.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ce_config.loader import ConfigBundle
from ce_core.build import BuildManifest, ExecutionGraph, ExecutionNode, RouteDecision
from ce_core.spec.videospec import VideoSpec
from ce_router import RouterCatalog

from ce_build.graph import BuildOptions, ParentBuild, build_graph, metric_route_key
from ce_build.refs import BuildRefs

__all__ = [
    "ManifestError",
    "NodeRecord",
    "assemble",
    "manifest_from_planned",
    "parent_build",
    "planned_routes",
    "records_for",
]


class ManifestError(ValueError):
    """Conflicting or duplicate manifest records (entries are insert-only)."""


@dataclass(frozen=True)
class NodeRecord:
    """One `build_manifest_entries` row."""

    node_key: str
    route: RouteDecision | None = None
    effective_seed: int | None = None
    artifact_id: str | None = None
    config_digests: Mapping[str, str] = field(default_factory=dict)
    impl_version: str | None = None

    def row(self) -> dict[str, Any]:
        return {
            "node_key": self.node_key,
            "route": self.route.model_dump(mode="json") if self.route else None,
            "effective_seed": self.effective_seed,
            "artifact_id": self.artifact_id,
            "config_digests": dict(self.config_digests),
            "impl_version": self.impl_version,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> NodeRecord:
        route = row.get("route")
        artifact = row.get("artifact_id")
        return cls(
            node_key=str(row["node_key"]),
            route=RouteDecision.model_validate(route) if route else None,
            effective_seed=row.get("effective_seed"),
            artifact_id=str(artifact) if artifact else None,
            config_digests=dict(row.get("config_digests") or {}),
            impl_version=row.get("impl_version"),
        )


def records_for(node: ExecutionNode, *, artifact_id: str | None, effective_seed: int | None) -> list[NodeRecord]:
    """The rows a completed node adds: its own, plus one per in-process metric route."""
    out = [
        NodeRecord(
            node_key=node.key,
            route=node.route,
            effective_seed=effective_seed,
            artifact_id=artifact_id,
            config_digests=dict(node.config_digests),
            impl_version=node.impl_version,
        )
    ]
    for capability, identity in sorted(dict(node.params.get("metrics", {})).items()):
        out.append(
            NodeRecord(node_key=metric_route_key(node.key, capability), route=RouteDecision.model_validate(identity))
        )
    return out


def assemble(records: Iterable[NodeRecord]) -> BuildManifest:
    """The manifest of a version from its rows. A config file read by several nodes must have one
    digest within a version; two rows for one node key are an error (insert-only)."""
    routes: dict[str, RouteDecision] = {}
    seeds: dict[str, int] = {}
    artifacts: dict[str, str] = {}
    configs: dict[str, str] = {}
    impls: dict[str, str] = {}
    seen: set[str] = set()
    for record in records:
        if record.node_key in seen:
            raise ManifestError(f"manifest entries are insert-only: duplicate row for {record.node_key}")
        seen.add(record.node_key)
        if record.route is not None:
            routes[record.node_key] = record.route
        if record.effective_seed is not None:
            seeds[record.node_key] = record.effective_seed
        if record.artifact_id is not None:
            artifacts[record.node_key] = record.artifact_id
        if record.impl_version is not None:
            impls[record.node_key] = record.impl_version
        for path, digest in record.config_digests.items():
            if configs.setdefault(path, digest) != digest:
                raise ManifestError(f"config {path} has two digests in one version")
    return BuildManifest(
        routes=routes, effective_seeds=seeds, artifact_map=artifacts, config_digests=configs, impl_versions=impls
    )


def planned_routes(graph: ExecutionGraph) -> dict[str, dict[str, Any]]:
    """`video_versions.planned_routes`: every routed node's decision, plus metric routes."""
    out: dict[str, dict[str, Any]] = {}
    for node in graph.nodes:
        if node.route is not None:
            out[node.key] = node.route.model_dump(mode="json")
        for capability, identity in dict(node.params.get("metrics", {})).items():
            out[metric_route_key(node.key, capability)] = dict(identity)
    return out


def manifest_from_planned(planned: Mapping[str, Mapping[str, Any]]) -> BuildManifest:
    """Plan-time routes as the initial pins of the first build (§12.3)."""
    return BuildManifest(routes={k: RouteDecision.model_validate(v) for k, v in planned.items()})


def parent_build(
    spec: VideoSpec,
    refs: BuildRefs,
    bundle: ConfigBundle,
    catalog: RouterCatalog,
    manifest: BuildManifest,
    *,
    options: BuildOptions | None = None,
) -> ParentBuild:
    """Re-derives the parent's graph on its recorded routes (see the module docstring)."""
    replay_options = BuildOptions(
        routing_profile=(options.routing_profile if options else None),
        provenance_mode=(options.provenance_mode if options else "mock_dev"),
        # the parent's own lip-sync patches (from its manifest), never the child's options
        lipsync_patch_shots=frozenset(k.split(":", 1)[1] for k in manifest.routes if k.startswith("lipsync.patch:")),
        sandbox=(options.sandbox if options else False),
        replay=True,
    )
    empty = ExecutionGraph(spec_content_digest=spec.content_digest(), routing_profile="draft", nodes=[])
    graph = build_graph(spec, refs, bundle, catalog, parent=ParentBuild(empty, manifest), options=replay_options)
    nodes = [
        node.model_copy(
            update={
                "config_digests": {p: manifest.config_digests.get(p, d) for p, d in node.config_digests.items()},
                "impl_version": manifest.impl_versions.get(node.key, node.impl_version),
                "route": manifest.routes.get(node.key, node.route),
            }
        )
        for node in graph.nodes
    ]
    return ParentBuild(graph.model_copy(update={"nodes": nodes}), manifest)
