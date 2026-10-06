"""I4 — requested ≠ compiled ≠ observed (§4, §37).

Every requested behavior item in a CBS gets a coverage entry with a level and a method; nothing
reports HONORED without a declared method that supports it; after generation every entry also
gets an observation verdict (asserted end to end in `tests/e2e/test_behavior_mock.py`)."""

from __future__ import annotations

import pytest
from ce_behavior.compiler import level_rank
from ce_behavior.coverage import coverage_report
from ce_behavior.plan import compile_version, predicted_coverage
from ce_contracts.behavior import BehaviorMatrix, precision_rank
from ce_core.behavior.cbs import RequestedControl
from ce_core.spec.videospec import VideoSpec
from ce_router import RouterCatalog
from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, VersionBehavior, bundle, catalog_with, version_behavior
from ce_testing.fixtures import example_spec_dict

pytestmark = [pytest.mark.invariant, pytest.mark.behavior]

B = bundle()
HONORING = {"native_parametric": "parametric", "native_segment": "native_segment"}


def _check_honest(vb: VersionBehavior, catalog: RouterCatalog) -> int:
    """Every HONORED realization is a native method the routed engine declares for the dimension at
    the requested precision, and not measured unreliable. Returns the number checked."""
    controls: dict[tuple[str, str], RequestedControl] = {
        (c.item_ref, c.dimension): c for cbs in vb.cbs.values() for c in cbs.requested_controls
    }
    checked = 0
    for compiled in vb.compiled:
        route = next(r for r in vb.routes.values() if r.digest() == compiled.route_digest)
        matrix = catalog.manifests[route.adapter_id].behavior_matrix
        assert matrix is not None
        for r in compiled.realizations:
            if str(r.level) != "HONORED":
                continue
            declared = matrix.control(r.dimension)
            assert str(r.method) in HONORING, (r.item_ref, r.method)
            assert declared.control == HONORING[str(r.method)], (r.item_ref, r.dimension, declared.control)
            wanted = controls[(r.item_ref, r.dimension)].temporal_precision
            assert precision_rank(declared.temporal_precision) >= precision_rank(str(wanted)), r.item_ref
            checked += 1
    return checked


@pytest.mark.parametrize("stage", ["plan_time", "build_time"])
@pytest.mark.parametrize("disabled", [GLOBAL_ONLY, SEGMENT_ONLY], ids=["global", "segment"])
def test_every_requested_item_gets_a_level_and_a_method(disabled: frozenset[str], stage: str) -> None:
    vb = version_behavior(catalog_with(disabled), stage=stage)  # type: ignore[arg-type]
    requested = [(c.item_ref, c.dimension) for cbs in vb.cbs.values() for c in cbs.requested_controls]
    entries = [(e.item_ref, e.dimension) for e in vb.report.entries]
    assert sorted(entries) == sorted(requested)
    for entry in vb.report.entries:
        assert str(entry.compiled.level) in {"HONORED", "APPROXIMATED", "UNSUPPORTED"}
        assert str(entry.compiled.method)
        assert entry.observed is None and entry.outcome is None  # nothing observed before generation


@pytest.mark.parametrize("disabled", [GLOBAL_ONLY, SEGMENT_ONLY], ids=["global", "segment"])
def test_nothing_is_honored_without_a_declared_method(disabled: frozenset[str]) -> None:
    catalog = catalog_with(disabled)
    assert _check_honest(version_behavior(catalog), catalog) >= 9


@pytest.mark.parametrize("dimension", ["gaze", "emotion_visual", "posture", "reaction", "camera_awareness"])
def test_removing_a_declared_control_removes_the_honored_claim(dimension: str) -> None:
    catalog = catalog_with(SEGMENT_ONLY)
    manifest = catalog.manifests["mock_avatar_segment"]
    assert manifest.behavior_matrix is not None
    data = manifest.behavior_matrix.model_dump(mode="json")
    data["dimensions"][dimension] = {"control": "none"}
    crippled = manifest.model_copy(update={"behavior_matrix": BehaviorMatrix.model_validate(data)})
    altered = RouterCatalog(**{**catalog.__dict__, "manifests": {**catalog.manifests, crippled.id: crippled}})
    vb = version_behavior(altered)
    _check_honest(vb, altered)
    visual = [e for e in vb.report.entries if e.dimension == dimension and "/annotations[" not in e.item_ref]
    assert visual and all(str(e.compiled.level) != "HONORED" for e in visual)


def test_items_no_target_renders_are_still_reported() -> None:
    """A scene whose talking shot is gone still reports every visual item (UNSUPPORTED, omit)."""
    data = example_spec_dict()
    scene = data["scenes"][0]
    scene["shots"][0]["type"] = "broll"
    scene["shots"][0]["character_key"] = None
    scene["shots"][0]["broll"] = {"source": "generate", "prompt": "a desk"}
    spec = VideoSpec.model_validate(data)
    vb = version_behavior(catalog_with(GLOBAL_ONLY), spec)
    visual = [e for e in vb.report.entries if B.vocab.dimensions[e.dimension].channel == "visual"]
    assert visual and all((str(e.compiled.level), str(e.compiled.method)) == ("UNSUPPORTED", "omit") for e in visual)
    assert len(vb.report.entries) == len(vb.cbs["scn_hook"].requested_controls)


def test_the_weakest_target_decides_an_item_spanning_several() -> None:
    vb = version_behavior(catalog_with(GLOBAL_ONLY))
    compiled = compile_version(
        vb.spec, vb.cbs, vb.routes, catalog_with(GLOBAL_ONLY).manifests, B.vocab, stage="build_time"
    )
    report = predicted_coverage(vb.cbs, compiled, B.vocab, stage="compiled")
    for entry in report.entries:
        ranks = [
            level_rank(str(r.level))
            for c in compiled
            for r in c.realizations
            if (r.item_ref, r.dimension) == (entry.item_ref, entry.dimension)
        ]
        if ranks:
            assert level_rank(str(entry.compiled.level)) == min(ranks)
    empty = coverage_report(vb.cbs, {}, B.vocab, stage="compiled")
    assert len(empty.entries) == len(report.entries)
