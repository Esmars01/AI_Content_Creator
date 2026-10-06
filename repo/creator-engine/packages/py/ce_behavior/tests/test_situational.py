"""Situational acting compilation suite [3] (§37): every acting-chain link compiles into CBS fields
and then into realization methods, for both mock behavior matrices (§15.1, §15.7)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from ce_behavior.compiler import CompileTarget, compile_behavior, coverage_downgrades
from ce_behavior.inputs import editorial_context
from ce_behavior.scene import SceneWords
from ce_core.behavior.cbs import CBSContent
from ce_core.spec.videospec import VideoSpec
from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, bundle, catalog_with, example_cbs, version_behavior
from ce_testing.fixtures import example_spec, example_spec_dict

pytestmark = pytest.mark.behavior

B = bundle()
H, A, U = "HONORED", "APPROXIMATED", "UNSUPPORTED"

# (item, dimension) → (global-prompt engine, segment-control engine), build time, the §11 example
# (with its cutaway overlay sht_2 over ev_1 and the punch-in mv_1 at seg_2.w0).
EXPECTED: dict[tuple[str, str], tuple[tuple[str, str], tuple[str, str]]] = {
    ("states[st_1]/emotion", "emotion_visual"): ((A, "keyframe_conditioning"), (H, "native_segment")),
    ("states[st_1]/emotion", "emotion_vocal"): ((A, "text_prompt_segment"), (A, "text_prompt_segment")),
    ("states[st_1]/strategies/prosody", "prosody_rate"): ((H, "native_parametric"), (H, "native_parametric")),
    ("states[st_1]/strategies/prosody", "prosody_energy"): ((H, "native_parametric"), (H, "native_parametric")),
    ("states[st_1]/strategies/prosody", "prosody_pitch"): ((H, "native_parametric"), (H, "native_parametric")),
    ("states[st_1]/strategies/gaze", "gaze"): ((U, "omit"), (H, "native_parametric")),
    ("states[st_1]/strategies/gesture", "gesture"): ((U, "omit"), (A, "text_prompt_segment")),
    ("states[st_1]/strategies/posture", "posture"): ((A, "keyframe_conditioning"), (H, "native_segment")),
    ("states[st_1]/strategies/reaction", "reaction"): ((U, "omit"), (H, "native_segment")),
    ("states[st_1]/strategies/camera_awareness", "camera_awareness"): (
        (A, "keyframe_conditioning"),
        (H, "native_segment"),
    ),
    ("states[st_2]/emotion", "emotion_visual"): ((A, "prosody_transfer"), (H, "native_segment")),
    ("states[st_2]/emotion", "emotion_vocal"): ((A, "text_prompt_segment"), (A, "text_prompt_segment")),
    ("states[st_2]/strategies/prosody", "prosody_rate"): ((H, "native_parametric"), (H, "native_parametric")),
    ("states[st_2]/strategies/prosody", "prosody_energy"): ((H, "native_parametric"), (H, "native_parametric")),
    ("states[st_2]/strategies/prosody", "prosody_pitch"): ((H, "native_parametric"), (H, "native_parametric")),
    ("states[st_2]/strategies/gaze", "gaze"): ((U, "omit"), (H, "native_parametric")),
    ("states[st_2]/strategies/gesture", "gesture"): ((U, "omit"), (A, "text_prompt_segment")),
    ("states[st_2]/strategies/posture", "posture"): ((U, "omit"), (H, "native_segment")),
    ("states[st_2]/strategies/reaction", "reaction"): ((U, "omit"), (H, "native_segment")),
    ("states[st_2]/strategies/camera_awareness", "camera_awareness"): ((U, "omit"), (H, "native_segment")),
    ("events[ev_1]", "gaze"): ((A, "editorial_cutaway"), (H, "native_parametric")),
    ("events[ev_2]", "facial_expression"): ((U, "omit"), (U, "omit")),
    ("annotations[an_1]", "prosody_emphasis"): ((H, "native_parametric"), (H, "native_parametric")),
    ("annotations[an_2]", "prosody_pause"): ((H, "native_parametric"), (H, "native_parametric")),
    ("annotations[an_3]", "prosody_emphasis"): ((H, "native_parametric"), (H, "native_parametric")),
}


def short(ref: str) -> str:
    return ref.split("/", 3)[-1] if ref.startswith("/scenes") else ref.split("/")[-1]


def realized(report: Any) -> dict[tuple[str, str], tuple[str, str]]:
    return {(short(e.item_ref), e.dimension): (str(e.compiled.level), str(e.compiled.method)) for e in report.entries}


def test_every_chain_link_lands_in_the_cbs() -> None:
    """SITUATION … REACTION (§15.1) → CBS fields."""
    cbs = example_cbs()["scn_hook"]
    st_1, st_2 = cbs.trajectory
    assert cbs.situation and cbs.situation.audience_stance == "agrees_with_misconception"  # SITUATION
    assert st_1.internal_state.label == "amused_certainty"  # INTERNAL STATE
    assert (st_2.social_goal, st_2.audience_goal, st_2.performance_intent) == (  # INTENT
        "establish_authority",
        "create_doubt",
        "undercut_belief",
    )
    assert cbs.intent.reveal_strategy == "tease_then_reveal"
    assert st_1.emotion.masking and st_1.emotion.felt.label == "amused"  # EMOTION
    assert st_2.confidence > st_1.confidence  # trajectory through confidence_delta
    assert cbs.prosody_directives[1].strategy == "slow_measured"  # PROSODY
    assert {e.dimension for e in cbs.events} == {"gaze", "facial_expression"}  # GAZE, FACIAL
    strategies = st_2.strategies
    assert (strategies.gesture, strategies.posture, strategies.camera_awareness, strategies.reaction) == (
        "still",
        "seated_lean_in",
        "direct_address",
        "none",
    )  # GESTURE, POSTURE, CAMERA AWARENESS, REACTION
    assert st_2.transition_in and st_2.transition_in.trigger and st_2.transition_in.trigger.kind == "realization"


@pytest.mark.parametrize(("disabled", "column"), [(GLOBAL_ONLY, 0), (SEGMENT_ONLY, 1)], ids=["global", "segment"])
def test_methods_per_item_for_both_mock_matrices(disabled: frozenset[str], column: int) -> None:
    vb = version_behavior(catalog_with(disabled))
    assert realized(vb.report) == {k: v[column] for k, v in EXPECTED.items()}


def test_the_segment_engine_needs_no_editorial_crutch_for_the_event() -> None:
    """EDITORIAL RESPONSE: the cutaway approximates ev_1 only where the face cannot look away."""
    global_ = realized(version_behavior(catalog_with(GLOBAL_ONLY)).report)
    segment = realized(version_behavior(catalog_with(SEGMENT_ONLY)).report)
    assert global_[("events[ev_1]", "gaze")] == (A, "editorial_cutaway")
    assert segment[("events[ev_1]", "gaze")] == (H, "native_parametric")


def _bare_spec() -> VideoSpec:
    data = example_spec_dict()
    scene = data["scenes"][0]
    scene["shots"] = [s for s in scene["shots"] if s["key"] != "sht_2"]
    scene["shots"][0]["camera"]["moves"] = []
    return VideoSpec.model_validate(data)


def test_the_plan_time_pass_proposes_splits_and_cutaways_for_the_global_engine() -> None:
    vb = version_behavior(catalog_with(GLOBAL_ONLY), _bare_spec(), stage="plan_time")
    actions = {(a.kind, short(a.item_ref)) for c in vb.compiled for a in c.editorial_actions}
    assert ("shot_split", "states[st_2]/emotion") in actions  # one prompt per side of the realization
    assert ("cutaway", "events[ev_1]") in actions  # the face cannot look away on cue
    assert not any(kind == "cutaway" and "/states[" in ref for kind, ref in actions)  # never over a whole state
    assert not any(ref.endswith("reaction") and "st_2" in ref for _, ref in actions)  # `none` is not editable
    split = next(a for c in vb.compiled for a in c.editorial_actions if a.kind == "shot_split")
    assert split.at is not None and (split.at.segment_key, split.at.word) == ("seg_2", 0)
    assert vb.report.stage == "predicted"


def test_the_plan_time_pass_proposes_nothing_the_segment_engine_does_natively() -> None:
    vb = version_behavior(catalog_with(SEGMENT_ONLY), _bare_spec(), stage="plan_time")
    actions = {(a.kind, short(a.item_ref)) for c in vb.compiled for a in c.editorial_actions}
    assert actions == {("punch_in", "events[ev_2]")}  # only the word-precise smile needs help


def test_the_build_time_pass_never_proposes() -> None:
    vb = version_behavior(catalog_with(GLOBAL_ONLY), _bare_spec())
    assert all(
        not c.editorial_actions or all(a.detail != "proposed at plan time" for a in c.editorial_actions)
        for c in vb.compiled
    )
    report = realized(vb.report)
    assert report[("events[ev_1]", "gaze")] == (U, "omit")  # no overlay in the spec, none invented
    assert report[("states[st_2]/emotion", "emotion_visual")] == (A, "prosody_transfer")


# ---------------------------------------------------------------------- compiler rules


def _visual_target(adapter: str, spec: VideoSpec, **changes: Any) -> tuple[CompileTarget, CBSContent, SceneWords]:
    catalog = catalog_with()
    manifest = catalog.manifests[adapter]
    scene = spec.scenes[0]
    words = SceneWords.of(spec, scene)
    shot = scene.shots[0]
    rng = words.range(shot.span)
    assert rng is not None
    target = CompileTarget(
        node_kind="behavior.compile_visual",
        stage="build_time",
        channel="visual",
        target_key=f"{shot.key}:c1",
        character_key="char_alex",
        scope=rng,
        shot=rng,
        shot_key=shot.key,
        chunk=1,
        matrix=manifest.behavior_matrix,
        knobs=dict(manifest.knobs),
        editorial_methods=frozenset(B.modes["talking_head_explainer"].editorial_methods),
        editorial=editorial_context(spec, scene, shot, words),
    )
    return replace(target, **changes), example_cbs(spec)[scene.key], words


def _method(compiled: Any, suffix: str, dimension: str) -> tuple[str, str]:
    r = next(r for r in compiled.realizations if r.item_ref.endswith(suffix) and r.dimension == dimension)
    return str(r.level), str(r.method)


def test_a_global_prompt_needs_the_state_to_cover_the_shot() -> None:
    data = example_spec_dict()
    states = data["scenes"][0]["acting"]["states"]
    states[0]["span"]["end"] = {"segment_key": "seg_2", "word": 5}
    del states[1]
    spec = VideoSpec.model_validate(data)
    target, cbs, words = _visual_target("mock_avatar_global", spec)
    compiled = compile_behavior(cbs, target, vocab=B.vocab, words=words)
    assert _method(compiled, "states[st_1]/emotion", "emotion_visual") == (A, "text_prompt_global")
    target, cbs, words = _visual_target("mock_avatar_global", example_spec())
    compiled = compile_behavior(cbs, target, vocab=B.vocab, words=words)
    assert _method(compiled, "states[st_1]/emotion", "emotion_visual") == (A, "keyframe_conditioning")


def test_measured_unreliable_controls_are_planned_approximated() -> None:
    target, cbs, words = _visual_target("mock_avatar_segment", example_spec(), unreliable=frozenset({"gaze"}))
    compiled = compile_behavior(cbs, target, vocab=B.vocab, words=words)
    level, method = _method(compiled, "events[ev_1]", "gaze")
    assert (level, method) == (A, "native_parametric")
    detail = next(r.detail for r in compiled.realizations if r.item_ref.endswith("events[ev_1]"))
    assert "unreliable" in detail


def test_editorial_methods_follow_the_mode_template() -> None:
    target, cbs, words = _visual_target("mock_avatar_global", example_spec(), editorial_methods=frozenset())
    compiled = compile_behavior(cbs, target, vocab=B.vocab, words=words)
    assert _method(compiled, "events[ev_1]", "gaze") == (U, "omit")


def test_compiling_never_changes_the_cbs() -> None:
    target, cbs, words = _visual_target("mock_avatar_global", example_spec())
    before = cbs.digest()
    compiled = compile_behavior(cbs, target, vocab=B.vocab, words=words)
    assert cbs.digest() == before == compiled.cbs_content_digest
    assert compiled.route_digest is None  # no route given: nothing pretends to be routed


def test_downgrades_compare_the_planned_and_the_actual_route() -> None:
    planned, cbs, words = _visual_target("mock_avatar_segment", example_spec())
    actual, _, _ = _visual_target("mock_avatar_global", example_spec())
    downgrades = coverage_downgrades(
        compile_behavior(cbs, planned, vocab=B.vocab, words=words),
        compile_behavior(cbs, actual, vocab=B.vocab, words=words),
    )
    keys = {(short(d["item_ref"]), d["dimension"]) for d in downgrades}
    assert ("states[st_2]/strategies/gaze", "gaze") in keys
    assert all(d["planned"].startswith(H) for d in downgrades if d["dimension"] == "gaze")
