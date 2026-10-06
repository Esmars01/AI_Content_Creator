"""The Creator Test script and scorecard (§17.4): a valid, comparable ~20-second spec, and a
scorecard that measures what ran, flags mocks and never invents the unmeasurable."""

from __future__ import annotations

import uuid

from ce_core.spec.videospec import VideoSpec
from ce_core.text.tokenize import tokenize
from ce_creator.scorecard import baseline_stats, cosine, distribution, scorecard
from ce_creator.test_script import SEGMENTS, TEST_SCRIPT_VERSION, creator_test_spec
from ce_testing.fixtures import ALEX, example_spec_dict


def _spec() -> dict:
    world = example_spec_dict()["scenes"][0]["world"]
    return creator_test_spec(
        video_id=str(uuid.uuid4()), version_id=str(uuid.uuid4()), creator_version_id=str(ALEX.CREATOR_VERSION_ID),
        world={**world, "overrides": {"element_states": {}, "hide_elements": [], "add_elements": [],
                                       "move_elements": [], "lighting": None, "acoustics": None}},
        wardrobe_version_id=None, vocab_version=example_spec_dict()["vocab_version"],
        seed_namespace=str(uuid.uuid4()),
    )  # fmt: skip


def test_the_test_script_is_a_valid_twenty_second_trajectory() -> None:
    spec = VideoSpec.model_validate(_spec())
    words = sum(len(tokenize(text)) for _, text in SEGMENTS)
    assert 15 <= words * 60 / 150 <= 30  # about 20 s at the default 150 WPM
    acting = spec.scenes[0].acting
    assert acting is not None
    labels = [s.emotion.displayed.label for s in acting.states if s.emotion is not None]
    assert labels == ["neutral", "excited", "hesitant", "serious"]
    assert {e.type for e in acting.events} == {"look_away", "small_laugh"}
    assert any(a.type == "emphasis" for seg in spec.script.segments for a in seg.annotations)
    assert spec.brief.input_mode == "exact_script" and TEST_SCRIPT_VERSION in spec.brief.angle


def test_scorecard_measures_flags_mocks_and_keeps_the_unmeasurable_unmeasured() -> None:
    card = scorecard(
        build_state="ready",
        verify=[{"wer": 0.0, "passed": True}, {"wer": 0.1, "passed": True}],
        tts=[{"words": 10, "duration_s": 4.0}, {"words": 5, "duration_s": 2.0}],
        qc_shots=[{"metrics": {"qc.lipsync": {"metric": "lipsync_score", "score": 6.5, "metrics": {"mock": 1}}}}],
        observations=[{"observed": {"signature": {"head_motion_pattern": [1.0, 2.0]},
                                    "tracks": [{"face_detected_ratio": 0.9}]}}],
        qc_world=[{"environment_identity": 0.8}],
        coverage={"report": {"entries": [{"outcome": "CONFIRMED"}, {"outcome": "NOT_MEASURABLE"}]}},
        face_similarity=[0.9, 0.8, None],
        face_mock=True,
        voice_similarity=[],
        voice_mock=True,
        voice_basis="the voice version has no reference audio to compare with",
        speech_quality=[{"metric": "dnsmos_ovrl", "score": 3.1, "mock": False}],
        critique={"eyes": 0.8, "mock": True},
    )  # fmt: skip
    assert card["wpm"]["value"] == 150.0 and card["wer"]["max"] == 0.1 and card["wer"]["passed"]
    assert card["identity_similarity"]["n"] == 2 and card["identity_similarity"]["mock"] is True
    assert card["voice_similarity"]["status"] == "not_measured"
    assert card["accent"]["status"] == "not_measured" and card["human_rating"]["status"] == "not_measured"
    assert card["lipsync"]["advisory"] is True and card["lipsync"]["mock"] is True
    assert card["requested_vs_observed"] == {
        "items": 2,
        "outcomes": {"CONFIRMED": 1, "NOT_MEASURABLE": 1},
        "confirmed": 1,
    }
    assert card["motion"]["flags"] == []
    stats = baseline_stats(card)
    assert stats["wpm"] == 150.0 and stats["identity_similarity"]["mock"] is True and "voice_similarity" not in stats


def test_helpers() -> None:
    assert cosine([1, 0], [1, 0]) == 1.0 and cosine([1, 0], [0, 1]) == 0.0 and cosine([], [1]) is None
    assert distribution([None]) == {"status": "not_measured", "n": 0}
    assert distribution([1.0, 3.0])["p50"] == 2.0
