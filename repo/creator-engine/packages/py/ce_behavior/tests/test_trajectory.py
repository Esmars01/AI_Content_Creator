"""Emotional trajectory suite [3, 4] (§37): states tile; transitions need triggers; masking; a
seconds-based request maps to states with reported drift (§15.3, §15.4)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest
from ce_behavior.acting import BandRequest, bands_from_seconds, trajectory
from ce_core.errors import Issue
from ce_core.spec.validate import ValidationContext, validate_spec
from ce_core.spec.videospec import VideoSpec
from ce_testing.behavior import bundle
from ce_testing.fixtures import example_references, example_spec, example_spec_dict
from pydantic import ValidationError

pytestmark = pytest.mark.behavior

VOCAB = bundle().vocab


def issues(change: Callable[[dict[str, Any]], None] | None = None) -> list[Issue]:
    data = example_spec_dict()
    if change:
        change(data)
    return validate_spec(VideoSpec.model_validate(data), ValidationContext(vocab=VOCAB, refs=example_references()))


def codes(found: list[Issue], severity: str = "error") -> set[str]:
    return {i.code for i in found if i.severity == severity}


def states(d: dict[str, Any]) -> list[dict[str, Any]]:
    return d["scenes"][0]["acting"]["states"]  # type: ignore[no-any-return]


def test_the_example_trajectory_is_valid() -> None:
    assert [i for i in issues() if i.severity == "error"] == []
    assert "strategies_do_not_express_emotion" not in codes(issues(), "warning")


def test_states_must_tile_the_speech() -> None:
    def gap(d: dict[str, Any]) -> None:
        states(d)[1]["span"]["start"] = {"segment_key": "seg_2", "word": 2}
        states(d)[1]["transition_in"]["trigger"]["at"] = {"segment_key": "seg_2", "word": 2}

    assert "tiling_gap" in codes(issues(gap))


def test_intensity_jumps_need_a_sudden_transition_or_a_trigger() -> None:
    def jump(d: dict[str, Any]) -> None:
        states(d)[1]["emotion"]["displayed"]["intensity"] = 0.2
        states(d)[1]["emotion"]["felt"]["intensity"] = 0.2
        states(d)[0]["emotion"]["displayed"]["intensity"] = 0.8
        states(d)[1]["transition_in"] = {"style": "gradual", "duration_words": 2}

    assert "intensity_jump" in codes(issues(jump))


def test_a_turn_between_incompatible_emotions_needs_a_trigger() -> None:
    def turn(d: dict[str, Any]) -> None:
        states(d)[1]["emotion"] = {
            "felt": {"label": "uncertain", "intensity": 0.5},
            "displayed": {"label": "uncertain", "intensity": 0.5},
            "masking": False,
        }
        states(d)[1]["strategies"]["prosody"] = "hesitant_with_pauses"
        states(d)[1]["transition_in"] = {"style": "gradual", "duration_words": 1}

    assert "turn_without_trigger" in codes(issues(turn))

    def with_trigger(d: dict[str, Any]) -> None:
        turn(d)
        states(d)[1]["transition_in"]["trigger"] = {"kind": "realization", "at": {"segment_key": "seg_2", "word": 0}}

    assert "turn_without_trigger" not in codes(issues(with_trigger))


def test_a_trigger_sits_inside_the_state_it_starts() -> None:
    def outside(d: dict[str, Any]) -> None:
        states(d)[1]["transition_in"]["trigger"]["at"] = {"segment_key": "seg_1", "word": 3}

    assert "trigger_outside_state" in codes(issues(outside))


def test_masking_requires_felt_different_from_displayed() -> None:
    def same(d: dict[str, Any]) -> None:
        states(d)[0]["emotion"]["felt"] = dict(states(d)[0]["emotion"]["displayed"])

    with pytest.raises(ValidationError, match="masking requires"):
        issues(same)


def test_strategies_that_express_nothing_of_the_emotion_warn() -> None:
    def odd(d: dict[str, Any]) -> None:
        states(d)[1]["strategies"].update(
            {
                "prosody": "playful_rising",
                "gaze": "side_glance",
                "gesture": "emphatic_beats",
                "posture": "seated_relaxed",
            }
        )

    found = issues(odd)
    assert "strategies_do_not_express_emotion" in codes(found, "warning")
    assert "strategies_do_not_express_emotion" not in codes(found)


def test_the_trajectory_accumulates_confidence_across_states() -> None:
    points = trajectory(example_spec(), {"char_alex": 0.7})
    assert [(p.state_key, p.label, p.confidence, p.masking) for p in points] == [
        ("st_1", "confident", 0.7, True),
        ("st_2", "serious", 0.85, False),
    ]
    assert (points[0].first_word, points[0].last_word, points[1].first_word) == (0, 7, 8)


# ---------------------------------------------------------------------- seconds → states (§15.4)

EXAMPLE = [
    BandRequest(0, 3, "confident"),
    BandRequest(3, 6, "realization"),
    BandRequest(6, 8, "uncertain"),
    BandRequest(8, 12, "curious"),
    BandRequest(12, 17, "confident"),
    BandRequest(17, 22, "humorous_reaction"),
    BandRequest(22, 30, "serious"),
]


def _words(n: int, wpm: float = 150.0, start: float = 0.1) -> list[tuple[float, float]]:
    per = 60.0 / wpm
    return [(round(start + i * per, 3), round(start + i * per + per * 0.8, 3)) for i in range(n)]


def test_a_seconds_request_maps_to_word_anchored_states_with_reported_drift() -> None:
    times = _words(74)  # ≈ 30 s at 150 wpm
    bands = bands_from_seconds(EXAMPLE, times)
    assert [b.request.label for b in bands] == [r.label for r in EXAMPLE]
    assert bands[0].first_word == 0 and bands[-1].last_word == 73
    for previous, current in itertools.pairwise(bands):
        assert current.first_word == previous.last_word + 1  # the states tile the words
    for band in bands[1:]:
        assert abs(band.drift_start_s) <= 0.2  # the closest word start, at most half a word away
        assert band.drift_start_s == pytest.approx(band.start_s - band.request.start_s)
    assert bands[-1].drift_end_s == pytest.approx(times[-1][1] - 30)


def test_drift_is_reported_when_speech_cannot_follow_the_seconds() -> None:
    times = _words(20, wpm=150)  # only ≈ 8 s of speech for a 30 s request
    bands = bands_from_seconds(EXAMPLE, times)
    assert len(bands) == 7 and all(b.last_word >= b.first_word for b in bands)
    assert bands[-1].drift_start_s < -10  # reported, never hidden
    with pytest.raises(ValueError, match="cannot tile"):
        bands_from_seconds(EXAMPLE, _words(5))
