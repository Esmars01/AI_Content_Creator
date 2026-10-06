"""BuildManifest, RouteDecision and the execution graph models (§12.1–§12.4).

The manifest is stored as insert-only `build_manifest_entries` rows and assembled on read.
`ExecutionGraph`/`ExecutionNode` are the derived "how to build it" view of a spec (§11 data
flow); `ce_build` constructs them, and cache keys never contain a version id (§12.2).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, NonNegativeInt

from ce_core.canonical import content_digest
from ce_core.scalars import Digest, NonEmptyStr
from ce_core.spec.base import SpecModel

__all__ = ["BuildManifest", "ExecutionGraph", "ExecutionNode", "ManifestEntry", "RouteDecision"]

_NON_IDENTITY = {"reason", "routing_profile", "params", "score", "reasons", "fallbacks", "pinned"}


class RouteDecision(SpecModel):
    """`route(node, context)` (§23). Identity = adapter, model, revision and translator version;
    the rest records why it was chosen."""

    adapter_id: NonEmptyStr
    model_id: NonEmptyStr
    revision: NonEmptyStr
    translator_version: str | None = None
    routing_profile: NonEmptyStr = "draft"
    reason: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    reasons: list[str] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list, description="adapter ids of the precomputed fallback chain")
    pinned: bool = False

    def identity(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude=_NON_IDENTITY)

    def digest(self) -> str:
        """The `route_digest` used in `derived_from` and cache keys (only the identity fields)."""
        return content_digest(self.identity())

    def same_route(self, other: RouteDecision | None) -> bool:
        return other is not None and self.identity() == other.identity()


class ManifestEntry(SpecModel):
    """One insert-only manifest row: a node's route, effective seed, artifact and config digests."""

    node_key: NonEmptyStr
    kind: Literal["route", "seed", "artifact", "config_digest", "impl_version"]
    route: RouteDecision | None = None
    seed: NonNegativeInt | None = None
    artifact_id: str | None = None
    config_name: str | None = None
    digest: Digest | None = None
    impl_version: str | None = None


class BuildManifest(SpecModel):
    routes: dict[str, RouteDecision] = Field(default_factory=dict)
    effective_seeds: dict[str, NonNegativeInt] = Field(default_factory=dict)
    artifact_map: dict[str, str] = Field(default_factory=dict)
    config_digests: dict[str, Digest] = Field(default_factory=dict)
    impl_versions: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def assemble(cls, entries: list[ManifestEntry]) -> BuildManifest:
        """Assembles the manifest from rows. Entries are insert-only: a second entry for the same key is an error."""
        routes: dict[str, RouteDecision] = {}
        seeds: dict[str, int] = {}
        artifacts: dict[str, str] = {}
        configs: dict[str, str] = {}
        impls: dict[str, str] = {}
        for entry in entries:
            if entry.kind == "route":
                _put(routes, entry.node_key, entry.route, entry)
            elif entry.kind == "seed":
                _put(seeds, entry.node_key, entry.seed, entry)
            elif entry.kind == "artifact":
                _put(artifacts, entry.node_key, entry.artifact_id, entry)
            elif entry.kind == "config_digest":
                _put(configs, entry.config_name or entry.node_key, entry.digest, entry)
            else:
                _put(impls, entry.node_key, entry.impl_version, entry)
        return cls(
            routes=routes, effective_seeds=seeds, artifact_map=artifacts, config_digests=configs, impl_versions=impls
        )


def _put[V](target: dict[str, V], key: str, value: V | None, entry: ManifestEntry) -> None:
    if value is None:
        raise ValueError(f"manifest entry {entry.node_key}/{entry.kind} has no value")
    if key in target:
        raise ValueError(f"manifest entries are insert-only: duplicate {entry.kind} for {key}")
    target[key] = value


class ExecutionNode(SpecModel):
    """One build-graph node (§12.1). `static_digest` covers every cache-key input known before
    execution (kind, impl_version, spec fragment, referenced DNA/world/memory fields, config,
    params, seed basis, take); upstream artifact hashes and the CBS content digest join at run time."""

    key: NonEmptyStr
    kind: NonEmptyStr
    executor: Literal["cpu", "model", "render"]
    capability: str | None = None
    group: Literal["scene", "video"] = "scene"
    scene_key: str | None = None
    shot_key: str | None = None
    segment_key: str | None = None
    character_key: str | None = None
    chunk: NonNegativeInt | None = None
    take: NonNegativeInt | None = None
    deps: list[str] = Field(default_factory=list)
    impl_version: NonEmptyStr
    spec_digest: Digest
    refs_digest: Digest
    config_digests: dict[str, Digest] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    seed_base: NonNegativeInt | None = None
    reads_requests: bool = False
    route: RouteDecision | None = None
    route_source: str | None = Field(default=None, description="the route group this node shares its route with")
    asset_inputs: dict[str, str] = Field(
        default_factory=dict,
        description="asset ids the node reads (name → asset id); their content sha256 is in refs_digest",
    )
    estimate: dict[str, float] = Field(
        default_factory=dict, description="planning estimate (seconds, usd); never part of the cache key"
    )

    def static_digest(self) -> str:
        return content_digest(
            {
                "kind": self.kind,
                "impl_version": self.impl_version,
                "spec": self.spec_digest,
                "refs": self.refs_digest,
                "config": self.config_digests,
                "params": self.params,
                "seed": self.seed_base,
                "take": self.take,
            }
        )


class ExecutionGraph(SpecModel):
    """Nodes in a topological order; derived from a spec, never stored as the source of truth."""

    spec_content_digest: Digest
    routing_profile: NonEmptyStr
    nodes: list[ExecutionNode]

    def by_key(self) -> dict[str, ExecutionNode]:
        return {n.key: n for n in self.nodes}

    def node(self, key: str) -> ExecutionNode:
        for n in self.nodes:
            if n.key == key:
                return n
        raise KeyError(key)

    def dependents(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {n.key: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.deps:
                out[dep].append(n.key)
        return out
