"""I5 — Deterministic builds: the same spec, BuildManifest, config and code give the same cache
keys; routes and seeds are pinned per version; re-routing happens only for dirty nodes or on an
explicit request. (The end-to-end second run with 100% cache hits is `tests/e2e`.)"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from ce_build import BuildOptions, ParentBuild, assemble, build_graph, cache_key, diff_graph, records_for
from ce_core.spec.videospec import VideoSpec
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.fixtures import example_spec_dict

pytestmark = [pytest.mark.invariant]

BUNDLE = config_bundle()
PROFILE = BUNDLE.routing["draft"].model_copy(
    update={
        "id": "quality_only",
        "weights": BUNDLE.routing["final"].weights.model_copy(update={"quality": 1.0, "cost": 0.0, "latency": 0.0}),
    }
)
QBUNDLE = replace(BUNDLE, routing={**BUNDLE.routing, "quality_only": PROFILE})
OPTIONS = BuildOptions(routing_profile="quality_only")


def spec(**changes: object) -> VideoSpec:
    data = example_spec_dict()
    data.update(changes)
    return VideoSpec.model_validate(data)


def keys(graph: object) -> dict[str, str]:
    out = {}
    for node in graph.nodes:  # type: ignore[attr-defined]
        upstream = {d: f"{abs(hash(d)) % 10**12:064d}" for d in node.deps}
        out[node.key] = cache_key(node, upstream, "sha256:" + "c" * 64 if node.reads_requests else None)
    return out


def test_same_inputs_give_the_same_cache_keys() -> None:
    a = build_graph(spec(), example_build_refs(), BUNDLE, mock_catalog())
    b = build_graph(spec(), example_build_refs(), BUNDLE, mock_catalog())
    assert keys(a) == keys(b)


def test_version_ids_never_enter_cache_keys() -> None:
    base = build_graph(spec(), example_build_refs(), BUNDLE, mock_catalog())
    other = build_graph(
        spec(version_id=str(UUID(int=42)), parent_version_id=str(UUID(int=41))),
        example_build_refs(),
        BUNDLE,
        mock_catalog(),
    )
    assert keys(other) == keys(base)


def test_seeds_are_per_node_and_take_and_ignore_versions() -> None:
    graph = build_graph(spec(), example_build_refs(), BUNDLE, mock_catalog()).by_key()
    t1, t2 = graph["avatar.render:sht_1:c1:t1"].seed_base, graph["avatar.render:sht_1:c1:t2"].seed_base
    assert t1 is not None and t2 is not None and t1 != t2
    again = build_graph(spec(version_id=str(UUID(int=9))), example_build_refs(), BUNDLE, mock_catalog()).by_key()
    assert again["avatar.render:sht_1:c1:t1"].seed_base == t1


def test_routes_are_pinned_and_rerouted_only_when_dirty_or_requested() -> None:
    favour_global = mock_catalog(
        QBUNDLE, quality={("mock_avatar_global", "avatar.a2v"): 1.0, ("mock_avatar_segment", "avatar.a2v"): 0.0}
    )
    favour_segment = mock_catalog(
        QBUNDLE, quality={("mock_avatar_global", "avatar.a2v"): 0.0, ("mock_avatar_segment", "avatar.a2v"): 1.0}
    )
    parent_graph = build_graph(spec(), example_build_refs(), QBUNDLE, favour_global, options=OPTIONS)
    manifest = assemble(
        [r for n in parent_graph.nodes for r in records_for(n, artifact_id=None, effective_seed=n.seed_base)]
    )
    parent = ParentBuild(parent_graph, manifest)
    clean = build_graph(spec(), example_build_refs(), QBUNDLE, favour_segment, parent=parent, options=OPTIONS)
    assert diff_graph(parent_graph, clean).route_changes == []  # weights changed, nothing re-routed
    assert keys(clean) == keys(parent_graph)
    forced = build_graph(
        spec(),
        example_build_refs(),
        QBUNDLE,
        favour_segment,
        parent=parent,
        options=replace(OPTIONS, force_reroute=frozenset({"avatar.render:sht_1:c1:t1"})),
    )
    assert {c.node_key for c in diff_graph(parent_graph, forced).route_changes} >= {"avatar.render:sht_1:c1:t1"}
