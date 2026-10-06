"""Acceptance: human behavior examples [4] (§37) — every request of §15.5 and the trajectory example
of §15.4 produces the expected acting structure (situation, states, masking, triggers, events), CBS
items and predicted coverage for both mock matrices."""

from __future__ import annotations

from typing import Any

import pytest
from ce_core.spec.videospec import VideoSpec
from ce_testing.director import coverage_by_matrix, fixture_inputs, plan_fixture

pytestmark = [pytest.mark.acceptance, pytest.mark.behavior]

RANK = {"UNSUPPORTED": 0, "APPROXIMATED": 1, "HONORED": 2}


def states(spec: VideoSpec) -> list[Any]:
    return [s for scene in sorted(spec.scenes, key=lambda x: x.order) if scene.acting for s in scene.acting.states]


def events(spec: VideoSpec) -> list[Any]:
    return [e for scene in spec.scenes if scene.acting for e in scene.acting.events]


def annots(spec: VideoSpec) -> list[Any]:
    return [a for seg in spec.script.segments for a in seg.annotations]


def at(ref: Any) -> tuple[str, int]:
    return (ref.segment_key, ref.word)


def assert_cbs_and_coverage(outcome: Any, refs: list[tuple[str, str]]) -> dict[str, dict[tuple[str, str], str]]:
    """Each (item_ref prefix, dimension) is a CBS requested control with a predicted coverage entry on
    both mock matrices, and the segment engine covers it at least as well as the global one."""
    by_matrix = coverage_by_matrix(outcome)
    levels: dict[str, dict[tuple[str, str], str]] = {}
    for matrix, result in by_matrix.items():
        requested = {(c.item_ref, c.dimension) for cbs in result["cbs"].values() for c in cbs.requested_controls}
        entries = {(e.item_ref, e.dimension): str(e.compiled.level) for e in result["report"].entries}
        assert set(entries) == requested
        for prefix, dimension in refs:
            assert any(r.startswith(prefix) and d == dimension for r, d in requested), (matrix, prefix, dimension)
        levels[matrix] = entries
    for key in levels["global"]:
        assert RANK[levels["segment"][key]] >= RANK[levels["global"][key]], key
    return levels


def state_ref(spec: VideoSpec, state: Any) -> str:
    scene = next(s for s in spec.scenes if s.acting and state in s.acting.states)
    return f"/scenes[{scene.key}]/acting/states[{state.key}]"


def event_ref(spec: VideoSpec, event: Any) -> str:
    scene = next(s for s in spec.scenes if s.acting and event in s.acting.events)
    return f"/scenes[{scene.key}]/acting/events[{event.key}]"


def test_he_realizes_the_viewer_may_disagree() -> None:
    assert fixture_inputs("realizes_viewer_disagrees")[1] == "He realizes the viewer may disagree with him."
    outcome = plan_fixture("realizes_viewer_disagrees", 1)
    spec = outcome.spec
    scene = spec.scene("scn_1")
    assert scene.acting is not None and scene.acting.situation.kind == "contradicting_the_audience"
    turn = next(s for s in states(spec) if s.transition_in and s.transition_in.trigger)
    trigger = turn.transition_in.trigger
    assert trigger.kind == "realization" and at(trigger.at) == ("seg_2", 4)  # "see": where he notices
    assert turn.internal_state.label == "doubt_about_reception"
    assert turn.strategies.gaze == "glance_away_and_return"
    after = states(spec)[states(spec).index(turn) + 1]
    assert after.internal_state.label == "determined" and after.strategies.prosody == "slow_measured"
    punches = [m for _, sh in spec.shots() for m in sh.camera.moves if m.type == "punch_in"]
    assert any(at(m.at) == at(trigger.at) for m in punches)  # editorial: a punch-in at the trigger
    assert_cbs_and_coverage(
        outcome,
        [
            (state_ref(spec, turn) + "/strategies/gaze", "gaze"),
            (state_ref(spec, after) + "/strategies/prosody", "prosody_rate"),
        ],
    )


def test_she_remembers_something_embarrassing() -> None:
    outcome = plan_fixture("embarrassing_memory")
    spec = outcome.spec
    scene = spec.scene("scn_1")
    assert scene.acting is not None
    assert scene.acting.situation.stimulus is not None and scene.acting.situation.stimulus.kind == "memory"
    recall = states(spec)[0]
    assert recall.internal_state.label == "embarrassed_recall"
    assert (recall.emotion.felt.label, recall.emotion.displayed.label, recall.emotion.masking) == (
        "embarrassed",
        "amused",
        True,
    )
    assert recall.strategies.gaze == "look_down_thinking" and recall.strategies.reaction == "suppressed"
    assert recall.strategies.prosody == "hesitant_with_pauses" and recall.strategies.posture == "seated_withdrawn"
    smile = next(e for e in events(spec) if e.type == "small_smile")
    assert any(a.type == "pause" for a in annots(spec) if a.span.start.segment_key in scene.segment_keys)
    levels = assert_cbs_and_coverage(
        outcome,
        [(state_ref(spec, recall) + "/emotion", "emotion_visual"), (event_ref(spec, smile), "facial_expression")],
    )
    assert levels["segment"][(state_ref(spec, recall) + "/emotion", "emotion_visual")] == "HONORED"


def test_he_tries_to_convince_a_skeptical_audience() -> None:
    outcome = plan_fixture("convince_skeptic")
    spec = outcome.spec
    scene = spec.scene("scn_1")
    assert scene.acting is not None
    assert scene.acting.situation.kind == "persuading_skeptic" and scene.acting.situation.audience_stance == "skeptical"
    assert scene.intent.persuasion_goal == "reframe_mental_model"
    trajectory = scene.acting.states
    assert all(s.social_goal == "win_trust" and s.performance_intent == "build_case" for s in trajectory)
    assert all(
        s.strategies.gesture == "illustrative_strong" and s.strategies.camera_awareness == "direct_address"
        for s in trajectory
    )
    assert sum(s.confidence_delta for s in trajectory) > 0 and all(s.confidence_delta >= 0 for s in trajectory)
    assert {e.type for e in scene.acting.events} >= {"open_palms", "count_on_fingers"}
    by_matrix = coverage_by_matrix(outcome)
    confidence = [t.confidence for t in by_matrix["global"]["cbs"]["scn_1"].trajectory]
    assert confidence == sorted(confidence) and confidence[-1] > confidence[0]  # rising, absolute in the CBS
    fingers = next(e for e in scene.acting.events if e.type == "count_on_fingers")
    assert_cbs_and_coverage(
        outcome,
        [(event_ref(spec, fingers), "gesture"), (state_ref(spec, trajectory[0]) + "/strategies/gesture", "gesture")],
    )


def test_she_becomes_confident_after_realizing_she_is_correct() -> None:
    outcome = plan_fixture("confident_after_realizing")
    spec = outcome.spec
    first, second = spec.scene("scn_1").acting.states  # type: ignore[union-attr]
    assert (first.emotion.displayed.label, second.emotion.displayed.label) == ("uncertain", "confident")
    assert second.transition_in.trigger.kind == "realization" and second.transition_in.style == "sudden"
    assert second.confidence_delta == pytest.approx(0.4)
    assert (first.strategies.posture, second.strategies.posture) == ("seated_upright", "seated_lean_in")
    assert (first.strategies.prosody, second.strategies.prosody) == ("hesitant_with_pauses", "assertive")
    assert_cbs_and_coverage(
        outcome,
        [
            (state_ref(spec, second) + "/strategies/posture", "posture"),
            (state_ref(spec, second) + "/emotion", "emotion_visual"),
        ],
    )


def test_he_pretends_to_stay_calm_while_surprised() -> None:
    assert fixture_inputs("calm_but_surprised_he") == ["He is pretending to stay calm while being surprised."]
    outcome = plan_fixture("calm_but_surprised_he")
    spec = outcome.spec
    masked = next(s for s in states(spec) if s.emotion.masking)
    assert (masked.emotion.felt.label, masked.emotion.felt.intensity) == ("surprised", 0.7)
    assert (masked.emotion.displayed.label, masked.emotion.displayed.intensity) == ("calm", 0.5)
    assert masked.strategies.reaction == "suppressed" and masked.strategies.prosody == "controlled_even"
    brow = next(e for e in events(spec) if e.type == "eyebrow_raise")
    leak = next(e for e in events(spec) if e.type == "double_take")
    assert brow.intensity <= 0.3 and leak.duration_ms <= 500  # low-intensity brow, a brief double take
    assert leak.trigger_ref is not None and leak.trigger_ref.endswith("/transition_in/trigger")
    assert_cbs_and_coverage(
        outcome,
        [
            (state_ref(spec, masked) + "/emotion", "emotion_visual"),
            (event_ref(spec, brow), "facial_expression"),
            (event_ref(spec, leak), "reaction"),
        ],
    )


def test_starts_excited_becomes_skeptical_pauses_looks_away_laughs_then_serious_before_the_cta() -> None:
    inputs = fixture_inputs("trajectory_excited_skeptical")
    assert inputs[1] == (
        "starts excited, becomes skeptical, pauses, looks away, laughs slightly, then becomes serious before the CTA"
    )
    outcome = plan_fixture("trajectory_excited_skeptical", 1)
    spec = outcome.spec
    labels = [s.emotion.displayed.label for s in states(spec)]
    assert labels[:3] == ["excited", "skeptical", "serious"]
    pause = next(a for a in annots(spec) if a.type == "pause")
    look = next(e for e in events(spec) if e.type == "look_away")
    laugh = next(e for e in events(spec) if e.type == "small_laugh")
    voice_laugh = next(a for a in annots(spec) if a.type == "nonverbal_audio")
    assert at(voice_laugh.span.start) == at(laugh.at)  # the voice actually laughs where the face does
    assert at(pause.span.start) < at(look.at) or pause.span.start.segment_key != look.at.segment_key
    assert spec.scenes[-1].intent.narrative_goal == "call_to_action"
    marks = {t.event_key: t for t in outcome.report.event_timings}  # the Performance Timeline's markers
    assert {look.key, laugh.key} <= set(marks)
    assert marks[look.key].at_s <= marks[laugh.key].at_s <= outcome.report.estimated_duration_s
    assert [t.at_s for t in outcome.report.event_timings] == sorted(t.at_s for t in outcome.report.event_timings)
    assert_cbs_and_coverage(
        outcome,
        [
            (event_ref(spec, look), "gaze"),
            (event_ref(spec, laugh), "reaction"),
            (
                f"/script/segments[{voice_laugh.span.start.segment_key}]/annotations[{voice_laugh.key}]",
                "nonverbal_audio",
            ),
        ],
    )


def test_seconds_based_trajectory_maps_to_seven_states_with_reported_drift() -> None:
    outcome = plan_fixture("trajectory_seconds")
    spec = outcome.spec
    trajectory = states(spec)
    assert [s.emotion.displayed.label for s in trajectory] == [
        "confident",
        "surprised",
        "uncertain",
        "curious",
        "confident",
        "amused",
        "serious",
    ]
    timings = {t.state_key: t for t in outcome.report.state_timings}
    requested = [0, 3, 6, 8, 12, 17, 22]
    for state, second in zip(trajectory, requested, strict=True):
        timing = timings[state.key]
        assert timing.requested_start_s == second
        assert timing.drift_s is not None and abs(timing.drift_s) <= 0.5  # drift reported, never hidden
    realization, humor = trajectory[1], trajectory[5]
    assert realization.transition_in.trigger.kind == "realization"
    assert humor.transition_in.trigger is not None and humor.strategies.reaction == "open"
    laugh = next(e for e in events(spec) if e.type == "small_laugh")
    assert any(a.type == "nonverbal_audio" and at(a.span.start) == at(laugh.at) for a in annots(spec))
    assert outcome.report.timing_source == "estimated"
    assert_cbs_and_coverage(
        outcome, [(state_ref(spec, realization) + "/emotion", "emotion_visual"), (event_ref(spec, laugh), "reaction")]
    )
