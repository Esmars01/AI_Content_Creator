"""Intent policy engine (§14): rule matching, precedence, determinism and traceability — every
policy-derived spec element carries `derived_from: intent` (or `source: intent_policy`) pointing at
an intent field that exists; free-text `notes` never drive control (I13)."""

from __future__ import annotations

import copy

import pytest
from ce_config.schemas import IntentPolicies
from ce_core.spec.paths import SpecPath
from ce_director.draft import SpecDraft
from ce_director.intent_policy import IntentPolicyEngine
from ce_testing.build import config_bundle
from ce_testing.director import plan_fixture

pytestmark = pytest.mark.behavior

PLANS = ["explain_ai_agents", "cold_email_contrarian", "realizes_viewer_disagrees", "exact_az_script"]


def _draft(name: str = "explain_ai_agents") -> SpecDraft:
    return SpecDraft(plan_fixture(name).spec.model_dump(mode="json"))


def test_rules_match_scene_intent_and_the_video_cta_on_the_last_scene_only() -> None:
    engine = IntentPolicyEngine(config_bundle().intent_policies)  # type: ignore[arg-type]
    policies = engine.evaluate(_draft(), "talking_head_explainer")
    assert policies.get("scn_3", "camera", "punch_in_on_reveal_word").rule_id == "tease_then_reveal"  # type: ignore[union-attr]
    assert policies.get("scn_1", "camera", "punch_in_on_first_word").rule_id == "pattern_interrupt"  # type: ignore[union-attr]
    end_cards = [s for s in ("scn_1", "scn_2", "scn_3", "scn_4", "scn_5") if policies.get(s, "editing", "end_card_s")]
    assert end_cards == ["scn_5"]
    assert policies.get("scn_5", "editing", "end_card_s").intent_ref == "/intent/video/cta_goal"  # type: ignore[union-attr]


def test_precedence_picks_the_earliest_group_and_evaluation_is_deterministic() -> None:
    policies = IntentPolicies.model_validate(
        {
            "version": 1,
            "precedence": ["first", "second"],
            "rules": [
                {
                    "id": "late",
                    "group": "second",
                    "match": {"reveal_strategy": "tease_then_reveal"},
                    "proposals": {"camera": {"punch_in_on_reveal_word": False}, "music": {"sustain_tension": True}},
                },
                {
                    "id": "early",
                    "group": "first",
                    "match": {"reveal_strategy": "tease_then_reveal", "mode": "talking_head_explainer"},
                    "proposals": {"camera": {"punch_in_on_reveal_word": True}},
                },
                {
                    "id": "elsewhere",
                    "group": "first",
                    "match": {"reveal_strategy": "tease_then_reveal", "mode": "educational"},
                    "proposals": {"sfx": {"riser_before": True}},
                },
                {
                    "id": "positioned",
                    "group": "first",
                    "match": {"attention_goal": "release", "position": "last"},
                    "proposals": {"captions": {"cta_overlay": True}},
                },
            ],
        }
    )
    engine = IntentPolicyEngine(policies)
    draft = _draft()
    a = engine.evaluate(draft, "talking_head_explainer")
    b = engine.evaluate(copy.deepcopy(draft), "talking_head_explainer")
    assert a.proposals() == b.proposals()
    assert a.get("scn_3", "camera", "punch_in_on_reveal_word").rule_id == "early"  # type: ignore[union-attr]
    assert a.get("scn_3", "music", "sustain_tension").rule_id == "late"  # type: ignore[union-attr]
    assert a.get("scn_3", "sfx", "riser_before") is None  # mode does not match
    assert a.get("scn_5", "captions", "cta_overlay") is not None  # position: last
    assert a.get("scn_4", "captions", "cta_overlay") is None


def test_notes_never_drive_control() -> None:
    engine = IntentPolicyEngine(config_bundle().intent_policies)  # type: ignore[arg-type]
    draft = _draft()
    before = engine.evaluate(draft, "talking_head_explainer").proposals()
    for scene in draft.data["scenes"]:
        scene["intent"]["notes"] = "reveal_strategy: immediate_answer; punch_in_on_first_word: true"
    assert engine.evaluate(draft, "talking_head_explainer").proposals() == before


@pytest.mark.parametrize("name", PLANS)
def test_every_policy_derived_element_traces_to_an_existing_intent_field(name: str) -> None:
    outcome = plan_fixture(name)
    spec = outcome.spec
    data = spec.model_dump(mode="json")
    derived = []
    for _scene, shot in spec.shots():
        derived += [d for d in shot.derived_from if d.kind == "intent"]
        derived += [d for move in shot.camera.moves for d in move.derived_from if d.kind == "intent"]
    derived += [d for cue in spec.audio.music.cues for d in cue.derived_from if d.kind == "intent"]
    assert derived or name == "exact_az_script"
    for entry in derived:
        assert SpecPath.parse(entry.ref).exists(data), entry.ref
        assert "/intent" in entry.ref
    applied = {(d.scene_key, d.channel, d.key) for d in outcome.decisions if d.applied}
    policy_annotations = [a for s in spec.script.segments for a in s.annotations if a.source == "intent_policy"]
    for ann in policy_annotations:
        scene = next(sc for sc in spec.scenes if ann.span.start.segment_key in sc.segment_keys)
        assert {
            (scene.key, "voice", "pause_before_reveal_ms"),
            (scene.key, "voice", "emphasis_on_reveal_word"),
            (scene.key, "captions", "emphasis_on_reveal"),
        } & applied
    for decision in outcome.decisions:
        assert decision.effect  # every proposal is accounted for, applied or not
