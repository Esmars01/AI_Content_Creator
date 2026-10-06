"""I1 — model-independent creator behavior (§4, §37).

`build_graph()` and `compile()` run twice on one version with two router configurations (mock
engines with different behavior matrices), persisting nothing: the spec content digest, the CBS
content digests and the DNA digests are identical; only compiled outputs, coverage and routes
differ; the segment engine's coverage is at least the global engine's; the compiler
approximations planned for the other route are flagged for re-proposal. A schema lint rejects
engine parameters in the model-independent schemas. Enabling `mock_avatar_segment` upgrades
coverage without any Director change.
"""

from __future__ import annotations

import pytest
from ce_behavior.approximations import reproposal_ops, stale_approximations
from ce_behavior.compiler import level_rank
from ce_behavior.lint import engine_terms, lint_document, lint_schema
from ce_build import cache_key
from ce_core.behavior.cbs import CBSContent
from ce_core.identity.creator import CreatorDNA
from ce_core.identity.memory import MemoryItem, MemorySnapshot
from ce_core.identity.world import WorldDNA
from ce_core.spec.acting import ActingPlan
from ce_core.spec.intent import SceneIntent, VideoIntent
from ce_core.spec.videospec import VideoSpec
from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, VersionBehavior, bundle, catalog_with, version_behavior
from ce_testing.build import example_build_refs
from ce_testing.fixtures import ALEX, example_spec, example_spec_dict

pytestmark = [pytest.mark.invariant, pytest.mark.behavior]

AVATAR = "avatar.render:sht_1:c1:t1"


def _key(node: object) -> str:
    """The node's cache key with the same (placeholder) upstream hashes on both sides."""
    deps = node.deps  # type: ignore[attr-defined]
    return cache_key(node, dict.fromkeys(deps, "0" * 64), "sha256:" + "1" * 64)  # type: ignore[arg-type]


def _levels(vb: VersionBehavior) -> dict[tuple[str, str], int]:
    return {(e.item_ref, e.dimension): level_rank(str(e.compiled.level)) for e in vb.report.entries}


def test_a_model_swap_changes_only_compiled_outputs_coverage_and_routes() -> None:
    spec = example_spec()
    before = spec.model_dump(mode="json")
    a = version_behavior(catalog_with(GLOBAL_ONLY), spec)
    b = version_behavior(catalog_with(SEGMENT_ONLY), spec)
    assert spec.model_dump(mode="json") == before  # nothing written back into the spec

    # Identical: spec content, CBS content, DNA digests and the behavior nodes' inputs.
    assert a.graph.spec_content_digest == b.graph.spec_content_digest == spec.content_digest()
    assert {k: v.digest() for k, v in a.cbs.items()} == {k: v.digest() for k, v in b.cbs.items()}
    refs = example_build_refs()
    creator = refs.creator(ALEX.CREATOR_VERSION_ID)
    assert {m.dna_behavior_digest for cbs in a.cbs.values() for m in cbs.cast} == {creator.behavior_digest()}
    assert a.cbs["scn_hook"].world == b.cbs["scn_hook"].world
    a_nodes, b_nodes = a.graph.by_key(), b.graph.by_key()
    assert _key(a_nodes["behavior.resolve:scn_hook"]) == _key(b_nodes["behavior.resolve:scn_hook"])
    for key in a_nodes:
        if key.startswith(("tts.segment:", "behavior.compile_voice:")):
            assert _key(a_nodes[key]) == _key(b_nodes[key]), key  # the voice route did not change

    # Different: routes, compiled outputs, coverage.
    assert (a.routes[AVATAR].adapter_id, b.routes[AVATAR].adapter_id) == ("mock_avatar_global", "mock_avatar_segment")
    visual_a = next(c for c in a.compiled if c.target_key == "sht_1:c1")
    visual_b = next(c for c in b.compiled if c.target_key == "sht_1:c1")
    assert visual_a.cbs_content_digest == visual_b.cbs_content_digest  # same request…
    assert visual_a.route_digest != visual_b.route_digest  # …compiled for different engines
    assert visual_a.realizations != visual_b.realizations
    assert _key(a_nodes["behavior.compile_visual:sht_1:c1"]) != _key(b_nodes["behavior.compile_visual:sht_1:c1"])

    # Coverage of the segment engine ≥ the global engine, item by item.
    la, lb = _levels(a), _levels(b)
    assert la.keys() == lb.keys()
    assert all(lb[k] >= la[k] for k in la)
    assert sum(lb.values()) > sum(la.values())


def test_a_reroute_flags_compiler_approximations_for_re_proposal() -> None:
    spec = example_spec()
    a = version_behavior(catalog_with(GLOBAL_ONLY), spec)
    b = version_behavior(catalog_with(SEGMENT_ONLY), spec)
    assert stale_approximations(spec, a.routes) == []  # planned for this route: nothing stale
    (stale,) = stale_approximations(spec, b.routes)
    assert (stale.element_path, stale.approximates) == (
        "/scenes[scn_hook]/shots[sht_2]",
        "/scenes[scn_hook]/acting/events[ev_1]",
    )
    assert stale.route_key == AVATAR and stale.current_route_digest == b.routes[AVATAR].digest()
    (op,) = reproposal_ops([stale], b.routes)
    assert op.op == "remove_shot" and "mock_avatar_segment" in op.reason
    # Nothing is changed silently: the spec still contains the element.
    assert any(s.key == "sht_2" for s in spec.scenes[0].shots)


def test_enabling_the_segment_engine_upgrades_coverage_without_a_director_change() -> None:
    """The operator enables `mock_avatar_segment` (a product decision). With the final routing
    profile the router weighs behavior: the global mock declares gaze, gesture and reaction
    `emergent`, an inability that scores 0, so the newly enabled engine wins the avatar route."""
    data = example_spec_dict()
    data["meta"]["quality_tier"] = "final"
    spec = VideoSpec.model_validate(data)
    before = version_behavior(catalog_with(GLOBAL_ONLY), spec)
    after = version_behavior(catalog_with(), spec)
    assert before.routes[AVATAR].adapter_id == "mock_avatar_global"
    assert after.routes[AVATAR].adapter_id == "mock_avatar_segment"
    assert before.graph.spec_content_digest == after.graph.spec_content_digest
    assert {k: v.digest() for k, v in before.cbs.items()} == {k: v.digest() for k, v in after.cbs.items()}
    lb, la = _levels(before), _levels(after)
    upgraded = [k for k in lb if la[k] > lb[k]]
    assert len(upgraded) >= 10 and all(la[k] >= lb[k] for k in lb)


# ---------------------------------------------------------------------- schema lint


@pytest.mark.parametrize(
    "model",
    [CreatorDNA, MemoryItem, MemorySnapshot, WorldDNA, VideoIntent, SceneIntent, ActingPlan, CBSContent],
    ids=lambda m: m.__name__,
)
def test_model_independent_schemas_contain_no_engine_parameters(model: type) -> None:
    params, _ = engine_terms(catalog_with().manifests.values())
    assert lint_schema(model, params) == []


def test_model_independent_documents_contain_no_engine_names() -> None:
    params, names = engine_terms(catalog_with().manifests.values())
    b = bundle()
    vb = version_behavior(catalog_with(GLOBAL_ONLY))
    spec = vb.spec.model_dump(mode="json")
    for document in (
        *(c.model_dump(mode="json") for c in vb.cbs.values()),
        *(s["acting"] for s in spec["scenes"]),
        *(s["intent"] for s in spec["scenes"]),
        spec["intent"],
    ):
        assert lint_document(document, params, names) == []
    assert b.vocab.dimensions  # the canonical dimension list is the only vocabulary engines share
