"""CanonicalBehaviorSpec suite [3] (§37): schema, deterministic resolution, resolution order
(§10.6), digest stability and no engine fields."""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from typing import Any

import pytest
from ce_behavior.inputs import cast_inputs, world_input
from ce_behavior.lint import ENGINE_FIELD_NAMES, engine_terms, lint_document, lint_schema
from ce_behavior.resolve import CastInput, build_envelope, resolve
from ce_core.behavior.cbs import CanonicalBehaviorSpec, CBSContent
from ce_core.identity.creator import CreatorDNA
from ce_core.identity.memory import MemoryItem
from ce_core.identity.world import WorldDNA
from ce_core.spec.acting import ActingPlan
from ce_core.spec.intent import SceneIntent, VideoIntent
from ce_core.spec.videospec import VideoSpec
from ce_testing.behavior import bundle, catalog_with, example_cbs
from ce_testing.build import example_build_refs
from ce_testing.fixtures import ALEX, example_spec, example_spec_dict

pytestmark = pytest.mark.behavior

B = bundle()
REFS = example_build_refs()


def resolve_scene(spec: VideoSpec, **cast_changes: Any) -> CBSContent:
    scene = spec.scenes[0]
    cast = cast_inputs(spec, REFS, scene)
    if cast_changes:
        cast = {k: replace(v, **cast_changes) for k, v in cast.items()}
    return resolve(spec, scene, cast=cast, world=world_input(REFS, scene), vocab=B.vocab, config=B.app.behavior)


def spec_with(mutate: Any) -> VideoSpec:
    data = example_spec_dict()
    mutate(data)
    return VideoSpec.model_validate(data)


def control(cbs: CBSContent, ref_suffix: str, dimension: str) -> Any:
    return next(c for c in cbs.requested_controls if c.item_ref.endswith(ref_suffix) and c.dimension == dimension)


# ---------------------------------------------------------------------- schema


def test_the_example_resolves_to_the_documented_cbs() -> None:
    """§15.6 example: profile, affordances, trajectory confidences and prosody directives."""
    cbs = example_cbs()["scn_hook"]
    (member,) = cbs.cast
    profile = member.behavior_profile
    assert profile.baseline.model_dump() == {"energy": 0.65, "confidence": 0.7, "warmth": 0.6, "expressivity": 0.55}
    assert profile.emotion_ranges == {"amused": (0.1, 0.6), "confident": (0.3, 0.8), "serious": (0.2, 0.8)}
    assert [h.kind for h in profile.habits] == ["gaze_habit.thinking_glance", "reaction_habit.laughter"]
    assert cbs.world is not None
    affordances = cbs.world.affordances
    assert affordances.attention_targets == ["camera", "el_monitor", "el_window"]
    assert affordances.seated and affordances.hand_space == "desk_surface"
    assert [(s.key, s.confidence) for s in cbs.trajectory] == [("st_1", 0.7), ("st_2", 0.85)]
    directives = [(d.segment_key, d.strategy, d.rate, d.energy, d.pitch_variation) for d in cbs.prosody_directives]
    assert directives == [("seg_1", "assertive_light", 1.0, 0.65, 0.5), ("seg_2", "slow_measured", 0.9, 0.55, 0.35)]
    assert cbs.prosody_directives[1].pauses[0].model_dump() == {"after_word": 3, "ms": 350}
    assert cbs.constraints.avoidances == ["emotion_visual:angry>0.5", "gesture:finger_point_at_camera"]
    assert cbs.situation is not None and cbs.situation.kind == "contradicting_the_audience"
    assert {e.key: e.dimension for e in cbs.events} == {"ev_1": "gaze", "ev_2": "facial_expression"}


def test_requested_controls_cover_every_item_and_channel() -> None:
    """One entry per state emotion (visual + vocal), strategy channel, event and annotation."""
    cbs = example_cbs()["scn_hook"]
    keys = [(c.item_ref, c.dimension) for c in cbs.requested_controls]
    assert len(keys) == len(set(keys)) == 25
    dims = {c.dimension for c in cbs.requested_controls if "/states[st_1]" in c.item_ref}
    assert dims == {
        "emotion_visual",
        "emotion_vocal",
        "prosody_rate",
        "prosody_energy",
        "prosody_pitch",
        "gaze",
        "gesture",
        "posture",
        "reaction",
        "camera_awareness",
    }
    assert control(cbs, "events[ev_1]", "gaze").value == "look_away:down_left@seg_2.w2+700ms"
    assert control(cbs, "annotations[an_2]", "prosody_pause").value == "350ms after w3"
    # Priority and confidence rules: strategies cap at `should`; low-salience tokens are `nice`;
    # policy annotations are `must`; masking emotions are low-reliability.
    assert control(cbs, "states[st_2]/strategies/gaze", "gaze").priority == "should"
    assert control(cbs, "states[st_2]/strategies/reaction", "reaction").priority == "nice"
    assert control(cbs, "annotations[an_2]", "prosody_pause").priority == "must"
    assert control(cbs, "states[st_1]/emotion", "emotion_visual").observation_reliability == "low"
    assert control(cbs, "events[ev_1]", "gaze").planner_confidence == 0.8  # memory-supported (+0.1)


def test_cbs_round_trips_and_the_envelope_is_not_digested() -> None:
    spec = example_spec()
    cbs = example_cbs()["scn_hook"]
    assert CBSContent.model_validate(cbs.model_dump(mode="json")) == cbs
    envelope = build_envelope(
        cbs, spec=spec, scene=spec.scenes[0], cast=cast_inputs(spec, REFS, spec.scenes[0]), vocab_version="2026.10.1"
    )
    assert envelope.envelope.content_digest == cbs.digest()
    assert envelope.envelope.refs.cast[0].memory_snapshot_id == ALEX.SNAPSHOT_ID
    assert CanonicalBehaviorSpec.model_validate(envelope.model_dump(mode="json")).content == cbs


# ---------------------------------------------------------------------- determinism and digests


def test_resolution_is_deterministic_and_order_independent() -> None:
    spec = example_spec()
    first = resolve_scene(spec)
    items = list(cast_inputs(spec, REFS, spec.scenes[0])["char_alex"].snapshot_items)
    random.Random(7).shuffle(items)
    shuffled = resolve_scene(spec, snapshot_items=tuple(reversed(items)))
    assert first.digest() == resolve_scene(spec).digest() == shuffled.digest()


def test_digest_ignores_identity_refs_and_intent_notes_but_tracks_behavior() -> None:
    spec = example_spec()
    base = resolve_scene(spec).digest()
    other_version = spec.model_copy(update={"version_id": ALEX.CREATOR_VERSION_ID})
    assert resolve_scene(other_version).digest() == base
    noted = spec_with(lambda d: d["scenes"][0]["intent"].__setitem__("notes", "film near the window"))
    assert resolve_scene(noted).digest() == base

    def louder(d: dict[str, Any]) -> None:
        d["scenes"][0]["acting"]["states"][1]["emotion"]["displayed"]["intensity"] = 0.7
        d["scenes"][0]["acting"]["states"][1]["emotion"]["felt"]["intensity"] = 0.7

    assert resolve_scene(spec_with(louder)).digest() != base


def test_the_cbs_digest_is_identical_across_engine_routes() -> None:
    from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, version_behavior

    a = version_behavior(catalog_with(GLOBAL_ONLY))
    b = version_behavior(catalog_with(SEGMENT_ONLY))
    assert {k: v.digest() for k, v in a.cbs.items()} == {k: v.digest() for k, v in b.cbs.items()}


# ---------------------------------------------------------------------- resolution order (§10.6)


def _memory(kind: str, value: dict[str, Any], item: str = "c1") -> dict[str, Any]:
    return {"item_id": f"0192f0a0-0000-7000-8000-0000000000{item}", "kind": kind, "value": value, "confidence": 0.9}


def test_memory_shifts_dna_baselines_within_the_configured_limit() -> None:
    spec = example_spec()
    warm = resolve_scene(spec, snapshot_items=(_memory("social_behavior.warmth", {"level": 0.95}),))
    assert warm.cast[0].behavior_profile.baseline.warmth == pytest.approx(0.6 + B.app.behavior.memory_shift_max)
    cool = resolve_scene(spec, snapshot_items=(_memory("social_behavior.warmth", {"level": 0.55}),))
    assert cool.cast[0].behavior_profile.baseline.warmth == pytest.approx(0.55)


def test_memory_ranges_narrow_inside_dna_bounds_never_outside() -> None:
    spec = example_spec()
    narrow = (_memory("emotional_tendency.emotional_range", {"label": "serious", "min": 0.4, "max": 0.9}),)
    assert resolve_scene(spec, snapshot_items=narrow).cast[0].behavior_profile.emotion_ranges["serious"] == (0.4, 0.8)
    disjoint = (_memory("emotional_tendency.emotional_range", {"label": "serious", "min": 0.85, "max": 0.95}),)
    assert resolve_scene(spec, snapshot_items=disjoint).cast[0].behavior_profile.emotion_ranges["serious"] == (0.2, 0.8)


def test_states_are_clamped_to_dna_unless_out_of_character() -> None:
    def hot(d: dict[str, Any]) -> None:
        d["scenes"][0]["acting"]["states"][1]["emotion"]["displayed"]["intensity"] = 0.95

    clamped = resolve_scene(spec_with(hot))
    assert clamped.trajectory[1].emotion.displayed.intensity == 0.8
    assert control(clamped, "states[st_2]/emotion", "emotion_visual").value == "serious@0.8"

    def allowed(d: dict[str, Any]) -> None:
        hot(d)
        d["scenes"][0]["acting"]["states"][1]["out_of_character"] = {"allowed": True, "reason": "the big reveal"}

    free = resolve_scene(spec_with(allowed))
    assert free.trajectory[1].emotion.displayed.intensity == 0.95
    assert free.constraints.out_of_character == ["/scenes[scn_hook]/acting/states[st_2]"]


def test_avoidances_are_the_union_of_dna_and_memory_and_cap_intensity() -> None:
    spec = example_spec()
    avoid = (_memory("avoidance.emotional_state", {"label": "serious", "max_intensity": 0.5}),)
    cbs = resolve_scene(spec, snapshot_items=avoid)
    assert "emotion_visual:serious>0.5" in cbs.constraints.avoidances
    assert "gesture:finger_point_at_camera" in cbs.constraints.avoidances  # DNA stays
    assert cbs.trajectory[1].emotion.displayed.intensity == 0.5


def test_user_tags_win_priority_and_confidence() -> None:
    def tagged(d: dict[str, Any]) -> None:
        d["script"]["segments"][0]["annotations"][0]["source"] = "user_tag"

    cbs = resolve_scene(spec_with(tagged))
    director = control(example_cbs()["scn_hook"], "annotations[an_1]", "prosody_emphasis")
    user = control(cbs, "annotations[an_1]", "prosody_emphasis")
    assert (director.priority, user.priority) == ("should", "must")
    assert user.planner_confidence > director.planner_confidence


def test_a_missing_creator_dna_falls_back_to_neutral_defaults() -> None:
    spec = example_spec()
    scene = spec.scenes[0]
    cbs = resolve(
        spec,
        scene,
        cast={"char_alex": CastInput("char_alex", None, "sha256:" + "0" * 64)},
        world=None,
        vocab=B.vocab,
        config=B.app.behavior,
    )
    assert cbs.cast[0].behavior_profile.baseline.energy == 0.5 and cbs.world is None
    assert len(cbs.requested_controls) == 25  # every item is still requested


# ---------------------------------------------------------------------- no engine fields (I1 lint)


MODEL_INDEPENDENT = (CreatorDNA, MemoryItem, WorldDNA, SceneIntent, VideoIntent, ActingPlan, CBSContent)


@pytest.mark.parametrize("model", MODEL_INDEPENDENT, ids=lambda m: m.__name__)
def test_model_independent_schemas_have_no_engine_fields(model: type) -> None:
    knob_params, _names = engine_terms(catalog_with().manifests.values())
    assert lint_schema(model, knob_params) == []


def test_concrete_documents_have_no_engine_names_or_parameters() -> None:
    params, names = engine_terms(catalog_with().manifests.values())
    assert "mock_avatar_global" in names and "mock_motion_gain" in params
    cbs = example_cbs()["scn_hook"].model_dump(mode="json")
    spec = example_spec().model_dump(mode="json")
    for document in (cbs, spec["scenes"][0]["acting"], spec["scenes"][0]["intent"], spec["intent"]):
        assert lint_document(document, params, names) == []


def test_the_lint_catches_engine_fields() -> None:
    params, names = engine_terms(catalog_with().manifests.values())
    leaked = copy.deepcopy(example_cbs()["scn_hook"].model_dump(mode="json"))
    leaked["trajectory"][0]["strategies"]["guidance_scale"] = 7.5
    leaked["trajectory"][1]["attention_target"] = "mock_avatar_global"
    leaked["mock_motion_gain"] = 1.2
    problems = lint_document(leaked, params, names)
    assert "/trajectory/0/strategies/guidance_scale" in problems
    assert "/trajectory/1/attention_target=mock_avatar_global" in problems
    assert "/mock_motion_gain" in problems
    assert "prompt" in ENGINE_FIELD_NAMES
