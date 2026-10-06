"""Route pinning (§12.4), locks pinning routes (§12.7), dirty analysis and impact (§12.9), and
the manifest round trip (§12.3)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from ce_build import (
    BuildOptions,
    ManifestError,
    NodeRecord,
    ParentBuild,
    assemble,
    build_graph,
    diff_graph,
    parent_build,
    planned_routes,
    records_for,
)
from ce_core.build import ExecutionGraph
from ce_core.spec.videospec import VideoSpec
from ce_router import RouterCatalog
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.fixtures import example_spec_dict, words

# A quality-only profile (720p, so both mock avatar engines qualify): weights decide the engine.
_BASE = config_bundle()
_PROFILE = _BASE.routing["draft"].model_copy(
    update={
        "id": "quality_only",
        "weights": _BASE.routing["final"].weights.model_copy(update={"quality": 1.0, "cost": 0.0, "latency": 0.0}),
    }
)
BUNDLE = replace(_BASE, routing={**_BASE.routing, "quality_only": _PROFILE})
FINAL = BuildOptions(routing_profile="quality_only")


def spec_of(data: dict[str, Any]) -> VideoSpec:
    return VideoSpec.model_validate(data)


def manifest_of(graph: ExecutionGraph) -> Any:
    rows = [
        r for i, n in enumerate(graph.nodes) for r in records_for(n, artifact_id=f"art-{i}", effective_seed=n.seed_base)
    ]
    return assemble(rows)


def favour(adapter: str, capability: str, other: str) -> RouterCatalog:
    return mock_catalog(BUNDLE, quality={(adapter, capability): 1.0, (other, capability): 0.0})


def with_second_voice(catalog: RouterCatalog, *, favoured: bool = True) -> RouterCatalog:
    second = catalog.manifests["mock_voice"].model_copy(update={"id": "mock_voice_b"})
    quality = {
        ("mock_voice_b", "voice.tts"): 1.0 if favoured else 0.0,
        ("mock_voice", "voice.tts"): 0.0 if favoured else 1.0,
    }
    return replace(
        catalog, manifests={**catalog.manifests, "mock_voice_b": second}, quality={**catalog.quality, **quality}
    )


def avatar_adapters(graph: ExecutionGraph) -> set[str]:
    return {n.route.adapter_id for n in graph.nodes if n.kind == "avatar.render" and n.route}


@pytest.fixture(scope="module")
def parent() -> tuple[VideoSpec, ExecutionGraph, ParentBuild]:
    spec = spec_of(example_spec_dict())
    graph = build_graph(
        spec,
        example_build_refs(),
        BUNDLE,
        favour("mock_avatar_global", "avatar.a2v", "mock_avatar_segment"),
        options=FINAL,
    )
    assert avatar_adapters(graph) == {"mock_avatar_global"}
    return spec, graph, ParentBuild(graph, manifest_of(graph))


FLIPPED = favour("mock_avatar_segment", "avatar.a2v", "mock_avatar_global")


def test_flipped_weights_would_choose_the_other_engine(parent: Any) -> None:
    spec, _, _ = parent
    fresh = build_graph(spec, example_build_refs(), BUNDLE, FLIPPED, options=FINAL)
    assert avatar_adapters(fresh) == {"mock_avatar_segment"}


def test_route_pinning_holds_when_router_weights_change(parent: Any) -> None:
    spec, graph, pb = parent
    child = build_graph(spec, example_build_refs(), BUNDLE, FLIPPED, parent=pb, options=FINAL)
    assert avatar_adapters(child) == {"mock_avatar_global"}
    for old, new in zip(graph.nodes, child.nodes, strict=True):
        assert old.key == new.key
        assert new.static_digest() == old.static_digest(), new.key
        if new.route is not None:
            assert new.route.same_route(old.route) and new.route.pinned, new.key
    impact = diff_graph(graph, child)
    assert impact.regenerate == [] and impact.cascade == [] and impact.route_changes == []
    assert impact.estimate["usd"] == 0 and impact.estimate["nodes"] == 0


def test_dirty_groups_are_rerouted_and_the_impact_shows_it(parent: Any) -> None:
    _, graph, pb = parent
    data = example_spec_dict()
    data["script"]["segments"][1]["text"] = "But here's the catch... they're not."
    child = build_graph(spec_of(data), example_build_refs(), BUNDLE, FLIPPED, parent=pb, options=FINAL)
    assert avatar_adapters(child) == {"mock_avatar_segment"}  # the one-chunk shot covers seg_2: no clean member
    by_key = child.by_key()
    assert by_key["tts.segment:seg_1"].route.same_route(graph.by_key()["tts.segment:seg_1"].route)  # type: ignore[union-attr]
    impact = diff_graph(graph, child)
    assert "tts.segment:seg_2" in impact.regenerate and "tts.segment:seg_1" in impact.keep
    assert "avatar.render:sht_1:c1:t1" in impact.regenerate
    assert {c.node_key for c in impact.route_changes} >= {"avatar.render:sht_1:c1:t1", "avatar.render:sht_1:c1:t2"}
    assert "render.final:tiktok_1080x1920_30" in impact.cascade
    assert "audio.sfx:sfx_1" in impact.keep
    assert impact.estimate["model_nodes"] > 0


def test_a_disabled_pinned_adapter_forces_a_reroute(parent: Any) -> None:
    spec, graph, pb = parent
    catalog = replace(FLIPPED, disabled=frozenset({"mock_avatar_global"}))
    child = build_graph(spec, example_build_refs(), BUNDLE, catalog, parent=pb, options=FINAL)
    assert avatar_adapters(child) == {"mock_avatar_segment"}
    impact = diff_graph(graph, child)
    change = next(c for c in impact.route_changes if c.node_key == "avatar.render:sht_1:c1:t1")
    assert change.old == "mock_avatar_global" and "re-routed" in change.reason


def test_explicit_reroute_replaces_a_clean_pin(parent: Any) -> None:
    spec, _, pb = parent
    options = BuildOptions(routing_profile="quality_only", force_reroute=frozenset({"avatar.render:sht_1:c1:t2"}))
    child = build_graph(spec, example_build_refs(), BUNDLE, FLIPPED, parent=pb, options=options)
    assert avatar_adapters(child) == {"mock_avatar_segment"}


def test_the_voice_lock_pins_the_tts_route_even_when_dirty() -> None:
    data = example_spec_dict()
    spec = spec_of(data)
    base = mock_catalog()
    graph = build_graph(spec, example_build_refs(), BUNDLE, base)
    pb = ParentBuild(graph, manifest_of(graph))
    data["script"]["segments"][0]["text"] = "Everyone thinks AI agents are only smarter chatbots."
    data["script"]["segments"][1]["text"] = "But here's the catch... they're not."
    catalog = with_second_voice(base)
    locked = build_graph(spec_of(data), example_build_refs(), BUNDLE, catalog, parent=pb).by_key()
    assert locked["tts.segment:seg_1"].route.adapter_id == "mock_voice"  # type: ignore[union-attr]
    assert "voice lock" in locked["tts.segment:seg_1"].route.reason  # type: ignore[union-attr]
    data["locks"] = []
    unlocked = build_graph(spec_of(data), example_build_refs(), BUNDLE, catalog, parent=pb).by_key()
    assert unlocked["tts.segment:seg_1"].route.adapter_id == "mock_voice_b"  # type: ignore[union-attr]
    assert unlocked["tts.segment:seg_2"].route == unlocked["tts.segment:seg_1"].route  # no splicing within a voice


def test_a_new_segment_joins_the_pinned_voice_route() -> None:
    data = example_spec_dict()
    data["locks"] = []
    base = mock_catalog()
    graph = build_graph(spec_of(data), example_build_refs(), BUNDLE, base)
    pb = ParentBuild(graph, manifest_of(graph))
    data["script"]["segments"][1]["text"] = "But here's the catch... they're not."
    child = build_graph(spec_of(data), example_build_refs(), BUNDLE, with_second_voice(base), parent=pb).by_key()
    assert child["tts.segment:seg_2"].route.adapter_id == "mock_voice"  # type: ignore[union-attr]  # seg_1 is clean


def test_seed_override_on_a_chunk_cascades_to_later_chunks_of_that_take() -> None:
    data = example_spec_dict()
    data["script"]["segments"][1]["text"] = " ".join(["word"] * 140)
    data["script"]["segments"][1]["annotations"] = []
    data["scenes"][0]["shots"][0]["span"]["end"]["word"] = 139
    data["scenes"][0]["acting"]["states"][1]["span"]["end"]["word"] = 139
    data["scenes"][0]["acting"]["events"] = []
    data["scenes"][0]["shots"][1]["span"] = words("seg_2", 2, 3)
    data["meta"]["target_duration_s"] = 60
    catalog = mock_catalog()
    graph = build_graph(spec_of(data), example_build_refs(), BUNDLE, catalog)
    pb = ParentBuild(graph, manifest_of(graph))
    data["generation"]["seed_overrides"] = {"avatar.render:sht_1:c1:t1": 7}
    child = build_graph(spec_of(data), example_build_refs(), BUNDLE, catalog, parent=pb)
    impact = diff_graph(graph, child)
    assert impact.regenerate == ["avatar.render:sht_1:c1:t1"]
    later = sorted(
        k for k in child.by_key() if k.startswith("avatar.render:sht_1:") and k.endswith(":t1") and ":c1:" not in k
    )
    assert later and set(later) <= set(impact.cascade)
    assert not any(k.startswith("avatar.render:sht_1:") and k.endswith(":t2") for k in impact.executes)


def test_evaluate_hook_keeps_downstream_when_compiled_output_is_unchanged(parent: Any) -> None:
    _, graph, pb = parent
    data = example_spec_dict()
    data["scenes"][0]["acting"]["events"][1]["intensity"] = 0.35  # a tweak the engine cannot show
    child = build_graph(spec_of(data), example_build_refs(), BUNDLE, FLIPPED, parent=pb, options=FINAL)
    conservative = diff_graph(graph, child)
    assert "behavior.resolve:scn_hook" in conservative.regenerate
    assert avatar_adapters(child) == {"mock_avatar_segment"}  # without evaluation the shot looks dirty

    def same(old: Any, new: Any) -> tuple[str, str]:
        return ("out", "out")

    child = build_graph(spec_of(data), example_build_refs(), BUNDLE, FLIPPED, parent=pb, options=FINAL, evaluate=same)
    assert avatar_adapters(child) == {"mock_avatar_global"}  # clean: keeps its pinned route
    impact = diff_graph(graph, child, evaluate=same)
    # resolve re-runs (cheap) and is found to leave its output unchanged: nothing downstream runs
    assert impact.regenerate == ["behavior.resolve:scn_hook"] and impact.cascade == []
    assert impact.no_visible_effect[0]["node_key"] == "behavior.resolve:scn_hook"
    assert impact.estimate["model_nodes"] == 0


def test_parent_build_replays_recorded_routes(parent: Any) -> None:
    spec, graph, pb = parent
    replayed = parent_build(spec, example_build_refs(), BUNDLE, FLIPPED, pb.manifest, options=FINAL)
    assert [(n.key, n.static_digest(), n.route.identity() if n.route else None) for n in replayed.graph.nodes] == [
        (n.key, n.static_digest(), n.route.identity() if n.route else None) for n in graph.nodes
    ]


def test_manifest_is_insert_only_and_records_metric_routes(parent: Any) -> None:
    _, graph, pb = parent
    assert "qc.shot:sht_1:t1/qc.vqa" in pb.manifest.routes
    assert set(planned_routes(graph)) == set(pb.manifest.routes)
    record = NodeRecord("x", effective_seed=1)
    with pytest.raises(ManifestError, match="insert-only"):
        assemble([record, record])
    with pytest.raises(ManifestError, match="two digests"):
        assemble([NodeRecord("a", config_digests={"c.yaml": "1"}), NodeRecord("b", config_digests={"c.yaml": "2"})])
    row = records_for(graph.nodes[3], artifact_id="a1", effective_seed=3)[0].row()
    assert NodeRecord.from_row(row).row() == row
