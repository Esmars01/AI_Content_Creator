"""Acceptance fixtures (plans) — every creation example of §2 produces a valid plan in fixture mode
(§37). Assertions cover the detected input mode, exact-script byte equality, scene, intent, acting
and world structure, and coverage report entries (for both mock matrices)."""

from __future__ import annotations

from typing import Any

import pytest
from ce_core.spec.validate import ValidationContext, validate_spec
from ce_core.spec.videospec import VideoSpec
from ce_testing.build import config_bundle
from ce_testing.director import MAYA, coverage_by_matrix, fixture_inputs, plan_fixture
from ce_testing.fixtures import ALEX
from ce_voice import parse_tags

pytestmark = pytest.mark.acceptance

CREATION = {
    "explain_ai_agents": "Create a 30-second TikTok explaining why most people misunderstand AI agents.",
    "exact_desk_45s": "Create a natural-looking 45-second video of a 25-year-old tech creator sitting at a desk",
    "exact_az_script": "Here is my exact A-Z script. Turn it into a realistic creator video without changing my",
    "ugc_product_review": "Make a UGC-style product review with a woman in her late 20s filming herself with an iPhone",
    "trajectory_excited_skeptical": "Create a video where the creator starts excited, becomes skeptical, pauses",
    "cold_email_contrarian": "I want a guy explaining why cold email is dead but actually say it's not dead",
    "realizes_viewer_disagrees": "He realizes halfway through that the viewer probably disagrees with him",
    "calm_but_surprised_she": "She is pretending to stay calm, but she's clearly surprised by the result.",
}


def _states(spec: VideoSpec) -> list[Any]:
    return [s for scene in sorted(spec.scenes, key=lambda x: x.order) if scene.acting for s in scene.acting.states]


def _events(spec: VideoSpec) -> list[Any]:
    return [e for scene in spec.scenes if scene.acting for e in scene.acting.events]


def _annotations(spec: VideoSpec) -> list[Any]:
    return [a for seg in spec.script.segments for a in seg.annotations]


@pytest.mark.parametrize("name", sorted(CREATION))
def test_every_creation_example_plans_validly_with_full_coverage(name: str) -> None:
    assert fixture_inputs(name)[0].startswith(CREATION[name])  # the §2 wording, verbatim
    outcome = plan_fixture(name)
    spec = outcome.spec
    bundle = config_bundle()
    issues = validate_spec(spec, ValidationContext(vocab=bundle.vocab, require_memory_snapshots=True))
    assert [i for i in issues if i.severity == "error"] == []
    assert outcome.planner == "llm" and outcome.report.planner == "llm"
    assert {r.stage for r in outcome.runs} >= {
        "interpret",
        "context",
        "research",
        "strategy",
        "script",
        "fact_check",
        "scenes",
        "acting",
        "intent_policy",
        "finalize",
    }
    assert all(r.status != "failed" for r in outcome.runs)
    assert len(spec.memory.snapshots) == 1 and outcome.snapshots  # one new snapshot per cast member (I7)
    assert all(scene.world is not None and scene.acting is not None for scene in spec.scenes)
    # Coverage: every requested control has an entry, on both mock matrices; segment ≥ global.
    by_matrix = coverage_by_matrix(outcome)
    levels: dict[str, dict[tuple[str, str], str]] = {}
    for matrix, result in by_matrix.items():
        requested = {(c.item_ref, c.dimension) for cbs in result["cbs"].values() for c in cbs.requested_controls}
        entries = {(e.item_ref, e.dimension): str(e.compiled.level) for e in result["report"].entries}
        assert requested and set(entries) == requested, matrix
        levels[matrix] = entries
    rank = {"UNSUPPORTED": 0, "APPROXIMATED": 1, "HONORED": 2}
    assert all(rank[levels["segment"][k]] >= rank[levels["global"][k]] for k in levels["global"])
    predicted = outcome.report.predicted_coverage
    assert predicted is not None and len(predicted.entries) == len(levels["global"])


def test_idea_explainer_has_the_reveal_structure_and_policy_trace() -> None:
    outcome = plan_fixture("explain_ai_agents")
    spec = outcome.spec
    assert spec.brief.input_mode == "idea" and spec.meta.mode == "talking_head_explainer"
    assert spec.meta.target_duration_s == 30 and spec.meta.strategy_pack == "myth_vs_reality"
    assert [s.purpose for s in spec.scenes] == ["hook", "setup", "turn", "explanation", "cta"]
    assert len(spec.brief.hook_candidates) == 4 and spec.brief.selected_hook_key == "hk_1"
    assert {str(s.world.world_version_id) for s in spec.scenes if s.world} == {str(ALEX.WORLD_VERSION_ID)}
    turn = spec.scene("scn_3")
    assert turn.intent.reveal_strategy == "tease_then_reveal"
    reveal = ("seg_3", 4)  # "Agents"
    moves = [m for shot in turn.shots for m in shot.camera.moves]
    punch = next(m for m in moves if (m.at.segment_key, m.at.word) == reveal)
    assert any(d.kind == "intent" and d.ref == "/scenes[scn_3]/intent/reveal_strategy" for d in punch.derived_from)
    pause = next(a for a in spec.script.segment("seg_3").annotations if a.type == "pause")
    assert pause.source == "intent_policy" and pause.span.start.word == 3
    trigger_state = next(s for s in turn.acting.states if s.transition_in and s.transition_in.trigger)  # type: ignore[union-attr]
    assert trigger_state.transition_in.trigger.kind == "realization"  # type: ignore[union-attr]
    assert (trigger_state.span.start.segment_key, trigger_state.span.start.word) == reveal  # type: ignore[union-attr]
    card = next(sh for sh in spec.scene("scn_5").shots if sh.type == "title_card")
    assert card.title.text == "Follow for part 2"  # type: ignore[union-attr]
    assert card.derived_from[0].ref == "/intent/video/cta_goal"
    script_run = next(r for r in outcome.runs if r.stage == "script")
    assert script_run.status == "repaired" and script_run.attempts[0]["raw"].startswith(
        "Sure!"
    )  # invalid JSON repaired
    assert spec.brief.assumptions and "No sources supplied" in spec.brief.assumptions[0]


def _assert_byte_equal(raw: str, span: tuple[int, int], spec: VideoSpec) -> None:
    """The segments (with the user's tags put back) reproduce the script span byte for byte; only
    whitespace separates them."""
    cursor = span[0]
    for segment in spec.script.segments:
        while raw[cursor].isspace():
            cursor += 1
        tagged = parse_tags(raw[cursor:])
        assert tagged.text.startswith(segment.text), segment.key
        # Find the raw slice whose cleaned text is exactly the segment.
        end = cursor + 1
        while parse_tags(raw[cursor:end]).text.rstrip() != segment.text:
            end += 1
            assert end <= span[1], f"{segment.key} not found verbatim"
        cursor = end
    assert raw[cursor : span[1]].strip() == ""


def test_exact_script_45s_keeps_every_byte_and_locks_the_wording() -> None:
    outcome = plan_fixture("exact_desk_45s")
    spec = outcome.spec
    raw = fixture_inputs("exact_desk_45s")[0]
    assert spec.brief.input_mode == "exact_script" and spec.brief.raw_input == raw
    start = raw.index("Most people think")
    _assert_byte_equal(raw, (start, len(raw)), spec)
    assert " ".join(s.text for s in spec.script.segments) == " ".join(raw[start:].split())
    assert spec.wording_locked
    assert spec.meta.target_duration_s == 45
    assert abs(outcome.report.estimated_duration_s - 45) / 45 <= 0.25
    # A 25-year-old presenter was requested; the closest approved creator is cast and the choice recorded.
    assert spec.cast[0].creator_version_id == MAYA.CREATOR_VERSION_ID
    assert any("closest approved creator" in a for a in outcome.report.assumptions)
    assert all(c.default_posture == "seated_upright" for s in spec.scenes for c in s.cast)


def test_exact_az_script_converts_the_users_tags() -> None:
    outcome = plan_fixture("exact_az_script")
    spec = outcome.spec
    raw = fixture_inputs("exact_az_script")[0]
    start = raw.index("[excited]")
    _assert_byte_equal(raw, (start, len(raw)), spec)
    assert all("[" not in s.text for s in spec.script.segments)
    tagged = {(a.type, a.tag, a.span.start.segment_key, a.span.start.word, a.source) for a in _annotations(spec)}
    assert ("pause", "short_pause", "seg_1", 9, "user_tag") in tagged  # after "itself."
    assert ("nonverbal_audio", "laugh", "seg_2", 4, "user_tag") in tagged  # after "blocking."
    sources = {(s.emotion.displayed.label, s.source) for s in _states(spec)}  # type: ignore[union-attr]
    assert ("excited", "user_tag") in sources and ("serious", "user_tag") in sources
    look = next(e for e in _events(spec) if e.type == "look_away")
    assert look.source == "user_tag" and (look.at.segment_key, look.at.word) == ("seg_4", 0)  # type: ignore[union-attr]
    assert spec.wording_locked and spec.meta.mode == "educational"


def test_ugc_review_casts_the_matching_creator_in_her_bedroom_and_passes_the_testimonial_guard() -> None:
    outcome = plan_fixture("ugc_product_review")
    spec = outcome.spec
    assert spec.meta.mode == "ugc_selfie"
    assert spec.cast[0].creator_version_id == MAYA.CREATOR_VERSION_ID
    assert {str(s.world.world_version_id) for s in spec.scenes if s.world} == {str(MAYA.WORLD_VERSION_ID)}
    assert {sh.camera.profile_id for _, sh in spec.shots() if sh.type == "talking_head"} == {"phone_front_selfie"}
    assert {c.placement for s in spec.scenes for c in s.cast} == {"zone_bed_edge"}
    guard = outcome.testimonial
    assert guard is not None and guard.applies and guard.classifier_status == "succeeded"
    assert guard.blocking == [] and not any(a.flagged for a in guard.assessments)
    uncertain = [f for f in outcome.report.findings if f.kind == "fact_check" and f.severity == "warning"]
    assert len(uncertain) == 2 and spec.research is not None and len(spec.research.claims) == 2


def test_excited_to_skeptical_to_serious_before_the_cta() -> None:
    spec = plan_fixture("trajectory_excited_skeptical").spec
    assert [s.emotion.displayed.label for s in _states(spec)][:3] == ["excited", "skeptical", "serious"]  # type: ignore[union-attr]
    skeptical = _states(spec)[1]
    assert skeptical.transition_in.trigger.kind == "realization"  # type: ignore[union-attr]
    assert any(a.type == "pause" for a in _annotations(spec))
    assert any(e.type == "look_away" for e in _events(spec))
    laugh = next(e for e in _events(spec) if e.type == "small_laugh")
    assert any(a.type == "nonverbal_audio" and a.span.start.word == laugh.at.word for a in _annotations(spec))  # type: ignore[union-attr]
    cta = spec.scenes[-1]
    assert cta.purpose == "cta" and cta.intent.narrative_goal == "call_to_action"
    serious_scene = next(
        s for s in spec.scenes if any(st.emotion.displayed.label == "serious" for st in s.acting.states)
    )  # type: ignore[union-attr]
    assert serious_scene.order == cta.order - 1


def test_brain_dump_becomes_explicit_constraints_with_examples_and_an_aggressive_open() -> None:
    outcome = plan_fixture("cold_email_contrarian")
    spec = outcome.spec
    assert spec.brief.input_mode == "brain_dump" and len(spec.brief.constraints) == 4
    assert spec.cast[0].creator_version_id == ALEX.CREATOR_VERSION_ID  # "a guy"
    hook = spec.scene("scn_1")
    assert hook.intent.attention_goal == "pattern_interrupt" and hook.intent.emotional_goal == "provoke"
    first_punch = hook.shots[0].camera.moves[0]
    assert (first_punch.at.segment_key, first_punch.at.word) == ("seg_1", 0)  # pattern interrupt
    assert hook.acting.states[0].strategies.prosody == "urgent_fast"  # type: ignore[union-attr]
    examples = next(s for s in spec.scenes if s.purpose == "example")
    authored = [sh for sh in examples.shots if sh.type == "broll" and not sh.derived_from]
    assert len(authored) == 2
    assert spec.scenes[-1].intent.emotional_goal == "provoke"


def test_realization_halfway_and_win_them_over() -> None:
    spec = plan_fixture("realizes_viewer_disagrees").spec
    states = _states(spec)
    assert [s.internal_state.label for s in states] == ["quiet_confidence", "doubt_about_reception", "determined"]  # type: ignore[union-attr]
    assert states[2].social_goal == "win_trust"


def test_calm_masking_surprise_casts_her() -> None:
    outcome = plan_fixture("calm_but_surprised_she")
    assert outcome.spec.cast[0].creator_version_id == MAYA.CREATOR_VERSION_ID
    masked = next(s for s in _states(outcome.spec) if s.emotion.masking)  # type: ignore[union-attr]
    assert masked.emotion.felt.label == "surprised" and masked.emotion.displayed.label == "calm"  # type: ignore[union-attr]
