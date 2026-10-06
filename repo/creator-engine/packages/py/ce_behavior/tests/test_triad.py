"""Requested vs compiled vs observed [3] (§37, §16): per-proxy verdicts on tracks with injected
failures, the 12 viewer-level outcomes, editorial items judged on `approximation_executed`, retry
targets by method, Performance QA decisions, take scores and measured profiles."""

from __future__ import annotations

from typing import Any

import pytest
from ce_behavior.coverage import NOT_RENDERED, coverage_report
from ce_behavior.judge import AudioEvidence, VisualEvidence, judge_audio, judge_visual
from ce_behavior.observe import ChunkMeasurement, observed_behavior, signature
from ce_behavior.profiles import ObservationRow, aggregate
from ce_behavior.qa import decide, retry_target, take_behavior_score
from ce_config.schemas import BehaviorQC
from ce_contracts.models import BodyLandmarksResult, FaceLandmarksResult, TrackEventOut
from ce_core.behavior.cbs import CBSContent, RequestedControl
from ce_core.behavior.compiled import Realization
from ce_core.behavior.coverage import is_delivered
from ce_core.behavior.observed import CharacterTracks, ItemObservation
from ce_core.enums import CoverageLevel, ObservationVerdict, Outcome, RealizationMethod
from ce_testing.behavior import bundle, example_cbs

pytestmark = pytest.mark.behavior

B = bundle()
JUDGE = B.app.behavior.judge
assert B.qc_behavior is not None
POLICY: BehaviorQC = B.qc_behavior
HZ = 10.0


def ctl(dimension: str, value: str, ref: str = "/scenes[scn_hook]/acting/events[ev_x]", **kw: Any) -> RequestedControl:
    fields: dict[str, Any] = {
        "item_ref": ref,
        "character_key": "char_alex",
        "dimension": dimension,
        "value": value,
        "temporal_precision": "word",
        "priority": "must",
        "planner_confidence": 0.8,
        "observation_reliability": "high",
    }
    fields.update(kw)
    return RequestedControl.model_validate(fields)


def tracks(n: int = 50, face: float = 1.0, **series: list[float]) -> CharacterTracks:
    base = {
        "gaze_deviation_deg": [3.0] * n,
        "smile": [0.1] * n,
        "brow_raise": [0.1] * n,
        "eye_closure": [0.05] * n,
        "gesture_energy": [0.1] * n,
        "shoulder_width_ratio": [1.0] * n,
    }
    base.update(series)
    return CharacterTracks(character_key="char_alex", sample_hz=HZ, series=base, face_detected_ratio=face)


def with_run(values: list[float], start_s: float, end_s: float, level: float) -> list[float]:
    out = list(values)
    for i in range(int(start_s * HZ), int(end_s * HZ)):
        out[i] = level
    return out


def verdict(control: RequestedControl, window: tuple[float, float], t: CharacterTracks | None) -> str:
    return str(judge_visual(control, window, VisualEvidence(tracks=t), B.vocab, JUDGE).verdict)


# ---------------------------------------------------------------------- per-take verdicts


LOOK = ctl("gaze", "look_away:down_left@seg_2.w2+700ms")


def test_look_away_confirmed_partial_and_not_observed() -> None:
    gaze = [3.0] * 50
    assert verdict(LOOK, (2.0, 2.7), tracks(gaze_deviation_deg=with_run(gaze, 2.0, 2.7, 20.0))) == "CONFIRMED"
    assert verdict(LOOK, (2.0, 2.7), tracks(gaze_deviation_deg=with_run(gaze, 2.0, 2.2, 20.0))) == "PARTIAL"
    assert verdict(LOOK, (2.0, 2.7), tracks()) == "NOT_OBSERVED"  # the injected failure: nothing happened
    late = with_run(gaze, 3.4, 4.0, 20.0)  # outside the 400 ms tolerance
    assert verdict(LOOK, (2.0, 2.7), tracks(gaze_deviation_deg=late)) == "NOT_OBSERVED"


def test_holding_the_camera_is_contradicted_by_looking_away() -> None:
    hold = ctl("gaze", "hold_camera", "/scenes[scn_hook]/acting/states[st_1]/strategies/gaze")
    assert verdict(hold, (0.0, 3.0), tracks()) == "CONFIRMED"
    away = with_run([3.0] * 50, 0.0, 2.5, 25.0)
    assert verdict(hold, (0.0, 3.0), tracks(gaze_deviation_deg=away)) == "CONTRADICTED"


def test_smile_events_by_level() -> None:
    smile = ctl("facial_expression", "small_smile@seg_2.w5+600ms")
    assert verdict(smile, (1.0, 1.6), tracks(smile=with_run([0.1] * 50, 1.0, 1.6, 0.7))) == "CONFIRMED"
    assert verdict(smile, (1.0, 1.6), tracks()) == "NOT_OBSERVED"


def test_unmeasurable_and_inapplicable_are_never_upgraded() -> None:
    emotion = ctl("emotion_visual", "serious@0.6", "/scenes[scn_hook]/acting/states[st_2]/emotion")
    assert verdict(emotion, (0.0, 3.0), tracks()) == "NOT_MEASURABLE"  # VLM proxies do not run
    assert verdict(LOOK, (2.0, 2.7), tracks(face=0.2)) == "NOT_MEASURABLE"  # face not detected
    assert verdict(LOOK, (2.0, 2.7), None) == "NOT_APPLICABLE"  # the character is not in the take
    missing = CharacterTracks(character_key="char_alex", sample_hz=HZ, series={}, face_detected_ratio=1.0)
    assert verdict(LOOK, (2.0, 2.7), missing) == "NOT_MEASURABLE"  # the analyzer series is missing


def test_gesture_and_posture_relative_to_the_rest_of_the_take() -> None:
    still = ctl("gesture", "still", "/scenes[scn_hook]/acting/states[st_2]/strategies/gesture")
    busy = with_run([0.1] * 50, 2.0, 5.0, 0.5)
    assert verdict(still, (2.0, 5.0), tracks(gesture_energy=busy)) == "CONTRADICTED"
    assert verdict(still, (0.0, 2.0), tracks(gesture_energy=busy)) == "CONFIRMED"
    lean = ctl("posture", "seated_lean_in", "/scenes[scn_hook]/acting/states[st_2]/strategies/posture")
    leaning = with_run([1.0] * 50, 2.0, 5.0, 1.1)
    assert verdict(lean, (2.0, 5.0), tracks(shoulder_width_ratio=leaning)) == "CONFIRMED"
    assert verdict(lean, (2.0, 5.0), tracks()) == "NOT_OBSERVED"


def _audio(times: dict[tuple[str, int], tuple[float, float]], **kw: Any) -> AudioEvidence:
    order = sorted(times, key=lambda w: times[w][0])
    return AudioEvidence(word_times=times, order=order, **kw)


def test_audio_pauses_rate_and_energy() -> None:
    words = [("seg_2", i) for i in range(6)]
    tight = {w: (0.3 * i, 0.3 * i + 0.25) for i, w in enumerate(words)}
    paused = {
        w: (t0 + (0.4 if i > 3 else 0.0), t1 + (0.4 if i > 3 else 0.0)) for i, (w, (t0, t1)) in enumerate(tight.items())
    }
    pause = ctl("prosody_pause", "350ms after w3", "/script/segments[seg_2]/annotations[an_2]")

    def audio_verdict(control: RequestedControl, item: list[tuple[str, int]], evidence: AudioEvidence) -> str:
        return str(judge_audio(control, item, evidence, B.vocab, JUDGE).verdict)

    assert audio_verdict(pause, [words[3]], _audio(paused)) == "CONFIRMED"
    assert audio_verdict(pause, [words[3]], _audio(tight)) == "NOT_OBSERVED"
    slow = ctl("prosody_rate", "slow_measured", "/scenes[scn_hook]/acting/states[st_2]/strategies/prosody")
    assert audio_verdict(slow, words, _audio(tight, baseline_wpm=150.0)) == "CONTRADICTED"  # 200 wpm
    assert audio_verdict(slow, words, _audio({w: (0.6 * i, 0.6 * i + 0.4) for i, w in enumerate(words)})) == "CONFIRMED"
    energy = ctl("prosody_energy", "slow_measured", "/scenes[scn_hook]/acting/states[st_2]/strategies/prosody")
    seg_1 = [("seg_1", i) for i in range(6)]
    times = {
        **{w: (0.5 * i, 0.5 * i + 0.4) for i, w in enumerate(seg_1)},
        **{w: (3 + 0.5 * i, 3.4 + 0.5 * i) for i, w in enumerate(words)},
    }
    requested = {**dict.fromkeys(seg_1, 0.65), **dict.fromkeys(words, 0.55)}
    quieter = [0.3] * 30 + [0.2] * 30
    louder = [0.2] * 30 + [0.3] * 30
    ev = {"energy_hz": 10.0, "requested_energy": requested}
    assert audio_verdict(energy, words, _audio(times, energy=quieter, **ev)) == "CONFIRMED"
    assert audio_verdict(energy, words, _audio(times, energy=louder, **ev)) == "CONTRADICTED"
    assert audio_verdict(energy, words, _audio(times, **{"requested_energy": requested})) == "NOT_MEASURABLE"


# ---------------------------------------------------------------------- viewer level: the 12 outcomes


def _realization(control: RequestedControl, level: str, method: str) -> Realization:
    return Realization(
        item_ref=control.item_ref,
        dimension=control.dimension,
        level=CoverageLevel(level),
        method=RealizationMethod(method),
    )


def _observation(control: RequestedControl, verdict_: str) -> ItemObservation:
    return ItemObservation(
        item_ref=control.item_ref,
        character_key="char_alex",
        dimension=control.dimension,
        verdict=ObservationVerdict(verdict_),
        method="x",
    )


def test_all_twelve_outcomes_and_only_confirmed_is_delivered() -> None:
    cases = (
        [("HONORED", "native_segment", v) for v in ("CONFIRMED", "PARTIAL", "NOT_OBSERVED", "CONTRADICTED")]
        + [("APPROXIMATED", "text_prompt_segment", v) for v in ("CONFIRMED", "PARTIAL", "NOT_OBSERVED", "CONTRADICTED")]
        + [
            ("UNSUPPORTED", "omit", "NOT_OBSERVED"),
            ("UNSUPPORTED", "omit", "CONFIRMED"),
            ("HONORED", "native_segment", "NOT_MEASURABLE"),
            ("APPROXIMATED", "keyframe_conditioning", "NOT_APPLICABLE"),
        ]
    )
    controls = [ctl("gaze", "look_away", f"/scenes[scn_hook]/acting/events[ev_{i}]") for i in range(len(cases))]
    cbs = example_cbs()["scn_hook"].model_copy(update={"requested_controls": controls})
    report = coverage_report(
        {"scn_hook": cbs},
        {(c.item_ref, c.dimension): _realization(c, lvl, m) for c, (lvl, m, _) in zip(controls, cases, strict=True)},
        B.vocab,
        stage="observed",
        observations={
            (c.item_ref, c.dimension): _observation(c, v) for c, (_, _, v) in zip(controls, cases, strict=True)
        },
    )
    outcomes = [str(e.outcome) for e in report.entries]
    assert set(outcomes) == {o.value for o in Outcome}  # every one of the 12
    delivered = {str(e.outcome) for e in report.entries if is_delivered(Outcome(str(e.outcome)))}
    assert delivered == {"HONORED_CONFIRMED", "APPROXIMATED_CONFIRMED"}
    emergent = next(e for e in report.entries if str(e.outcome) == "UNSUPPORTED_OBSERVED")
    assert "emergent" in emergent.summary


def test_items_nothing_renders_are_reported_not_dropped() -> None:
    cbs = example_cbs()["scn_hook"]
    report = coverage_report({"scn_hook": cbs}, {}, B.vocab, stage="compiled")
    assert len(report.entries) == len(cbs.requested_controls)
    assert all(str(e.compiled.level) == "UNSUPPORTED" and e.compiled.detail == NOT_RENDERED for e in report.entries)


def test_editorial_items_are_judged_on_approximation_executed() -> None:
    cut = ctl("gaze", "look_away:down_left@seg_2.w2+700ms", "/scenes[scn_hook]/acting/events[ev_1]")
    cbs = example_cbs()["scn_hook"].model_copy(update={"requested_controls": [cut]})
    key = (cut.item_ref, cut.dimension)
    realizations = {key: _realization(cut, "APPROXIMATED", "editorial_cutaway")}
    observations = {key: _observation(cut, "NOT_OBSERVED")}
    for executed, action in ((True, "pass"), (False, "render_defect")):
        report = coverage_report(
            {"scn_hook": cbs},
            realizations,
            B.vocab,
            stage="observed",
            observations=observations,
            executed={key: executed},
        )
        (entry,) = report.entries
        assert entry.expected_for_method and entry.approximation_executed is executed
        assert str(entry.outcome) == "APPROXIMATED_NOT_OBSERVED"  # hidden by the cutaway, as planned
        assert decide(report.entries, {key: cut}, POLICY, voice_locked=False).action == action
    executed_entry = coverage_report(
        {"scn_hook": cbs}, realizations, B.vocab, stage="observed", observations=observations, executed={key: True}
    ).entries[0]
    assert "expected" in executed_entry.summary


# ---------------------------------------------------------------------- retry targets and QA


@pytest.mark.parametrize(
    ("method", "locked", "rerun"),
    [
        ("native_parametric", False, ["avatar.render"]),
        ("native_segment", False, ["avatar.render"]),
        ("text_prompt_segment", False, ["avatar.render"]),
        ("text_prompt_global", False, ["avatar.render"]),
        ("shot_split", False, ["avatar.render"]),
        ("keyframe_conditioning", False, ["image.keyframe", "avatar.render"]),
        ("prosody_transfer", False, ["tts.segment", "avatar.render"]),
        ("prosody_transfer", True, ["needs_review"]),
        ("audio_nonverbal", True, ["needs_review"]),
        ("post_expression", False, ["post.expression"]),
        ("editorial_cutaway", False, ["render.final"]),
        ("caption_emphasis", False, ["render.final"]),
        ("omit", False, []),
    ],
)
def test_retry_targets_follow_the_method_table(method: str, locked: bool, rerun: list[str]) -> None:
    assert retry_target(method, voice_locked=locked) == rerun


def _entries(
    cases: list[tuple[RequestedControl, str, str, str]],
) -> tuple[Any, dict[tuple[str, str], RequestedControl]]:
    cbs = example_cbs()["scn_hook"].model_copy(update={"requested_controls": [c for c, *_ in cases]})
    report = coverage_report(
        {"scn_hook": cbs},
        {(c.item_ref, c.dimension): _realization(c, lvl, m) for c, lvl, m, _ in cases},
        B.vocab,
        stage="observed",
        observations={(c.item_ref, c.dimension): _observation(c, v) for c, _, _, v in cases},
    )
    return report.entries, {(c.item_ref, c.dimension): c for c, *_ in cases}


def test_must_items_not_observed_request_a_retry_of_the_right_node() -> None:
    must = ctl("gaze", "look_away", "/scenes[scn_hook]/acting/events[ev_1]")
    entries, controls = _entries([(must, "HONORED", "native_parametric", "NOT_OBSERVED")])
    decision = decide(entries, controls, POLICY, voice_locked=False)
    assert decision.action == "retry" and decision.executed
    (retry,) = decision.retries
    assert retry["rerun"] == ["avatar.render"] and retry["then"] == list(POLICY.must.after_retry)
    assert decision.as_dict()["ladder"] == "qc_gate"  # executed by the QC gate (ADR 0056)


def test_should_nice_low_reliability_and_unmeasurable_never_fail() -> None:
    should = ctl("gaze", "look_away", "/scenes[scn_hook]/acting/events[ev_1]", priority="should")
    low = ctl(
        "emotion_visual", "serious", "/scenes[scn_hook]/acting/states[st_2]/emotion", observation_reliability="low"
    )
    unmeasured = ctl("emotion_vocal", "serious", "/scenes[scn_hook]/acting/states[st_2]/emotion")
    partial = ctl("prosody_emphasis", "emphasize w5", "/script/segments[seg_2]/annotations[an_3]")
    entries, controls = _entries(
        [
            (should, "HONORED", "native_parametric", "NOT_OBSERVED"),
            (low, "HONORED", "native_segment", "CONTRADICTED"),
            (unmeasured, "APPROXIMATED", "text_prompt_segment", "NOT_MEASURABLE"),
            (partial, "HONORED", "native_parametric", "PARTIAL"),
        ]
    )
    decision = decide(entries, controls, POLICY, voice_locked=False)
    assert decision.action == "warn" and decision.retries == []
    reasons = {w["dimension"]: w["reason"] for w in decision.warnings}
    assert "low-reliability" in reasons["emotion_visual"] and reasons["prosody_emphasis"] == "HONORED_PARTIAL"


def test_take_scores_weigh_priority_and_rank_takes() -> None:
    must = ctl("gaze", "look_away", "/scenes[scn_hook]/acting/events[ev_1]")
    nice = ctl("facial_expression", "small_smile", "/scenes[scn_hook]/acting/events[ev_2]", priority="nice")
    controls = {(c.item_ref, c.dimension): c for c in (must, nice)}
    good = take_behavior_score([_observation(must, "CONFIRMED"), _observation(nice, "NOT_OBSERVED")], controls)
    bad = take_behavior_score([_observation(must, "NOT_OBSERVED"), _observation(nice, "CONFIRMED")], controls)
    assert good is not None and bad is not None and good > bad
    assert take_behavior_score([_observation(must, "NOT_MEASURABLE")], controls) is None


# ---------------------------------------------------------------------- measurement and profiles


def test_chunks_are_stitched_at_their_offsets() -> None:
    def face(events: list[TrackEventOut]) -> FaceLandmarksResult:
        return FaceLandmarksResult(
            sample_hz=HZ, series={"gaze_deviation_deg": [3.0] * 20, "smile": [0.2] * 20}, events=events,
            face_detected_ratio=1.0,
        )  # fmt: skip

    body = BodyLandmarksResult(sample_hz=HZ, series={"gesture_energy": [0.1] * 20}, body_detected_ratio=1.0)
    observed = observed_behavior(
        take_sha256="sha256:" + "a" * 64,
        character_key="char_alex",
        chunks=[
            ChunkMeasurement(2.0, 2.0, face([TrackEventOut(type="look_away", start_s=0.5, end_s=1.0)]), body),
            ChunkMeasurement(0.0, 2.0, face([]), body),
        ],
        analyzers=[],
    )
    (t,) = observed.tracks
    assert len(t.series["gaze_deviation_deg"]) == 40 and len(t.series["gesture_energy"]) == 40
    assert [(e.type, e.start_s, e.end_s) for e in t.events] == [("look_away", 2.5, 3.0)]
    assert observed.signature == signature(t) and observed.signature.expression_sequence == ["look_away"]


def test_profiles_aggregate_engine_executed_observations_only() -> None:
    def row(method: str, verdict_: str, dim: str = "gaze", scope: str = "take") -> ObservationRow:
        return ObservationRow("mock_avatar_segment", "0.1.0", "1", dim, "en-US", method, verdict_, scope)

    rows = [
        row("native_parametric", "CONFIRMED"),
        row("native_parametric", "PARTIAL"),
        row("native_parametric", "NOT_OBSERVED"),
        row("native_parametric", "NOT_MEASURABLE"),  # not measurable: not counted
        row("editorial_cutaway", "NOT_OBSERVED"),  # another node realized it: not counted
        row("native_parametric", "CONFIRMED", scope="viewer"),  # visual: take level only
        row("native_parametric", "CONFIRMED", dim="prosody_rate", scope="viewer"),  # audio: viewer level
    ]
    profiles = aggregate(rows, audio_dimensions=frozenset({"prosody_rate"}))
    by_dim = {k.dimension: v for k, v in profiles.items()}
    assert by_dim["gaze"]["n"] == 3 and by_dim["gaze"]["success_rate"] == 0.5
    assert by_dim["prosody_rate"]["n"] == 1
    assert {k.language for k in profiles} == {"en"}


def test_the_requested_controls_of_the_example_all_have_a_proxy_or_say_why_not() -> None:
    """Every requested label maps to a proxy, or the judgement reports NOT_MEASURABLE with a reason."""
    cbs: CBSContent = example_cbs()["scn_hook"]
    for control in cbs.requested_controls:
        if B.vocab.dimensions[control.dimension].channel != "visual":
            continue
        observation = judge_visual(control, (0.0, 4.0), VisualEvidence(tracks=tracks()), B.vocab, JUDGE)
        assert str(observation.verdict) in {v.value for v in ObservationVerdict}
        if str(observation.verdict) == "NOT_MEASURABLE":
            assert "(" in observation.method, control.item_ref  # the reason is recorded


# The Performance QA outcome matrix (§16.5, Phase 11 DoD "behavior QC actions follow the outcome
# matrix"): priority × level × verdict × reliability → the gate's action.
MATRIX = [
    # (priority, level, method, verdict, reliability, expected action)
    ("must", "HONORED", "native_parametric", "CONFIRMED", "high", "pass"),
    ("must", "HONORED", "native_parametric", "PARTIAL", "high", "warn"),
    ("must", "HONORED", "native_parametric", "NOT_OBSERVED", "high", "retry"),
    ("must", "HONORED", "native_parametric", "CONTRADICTED", "high", "retry"),
    ("must", "HONORED", "native_parametric", "NOT_OBSERVED", "low", "warn"),  # low reliability only warns
    ("must", "HONORED", "native_parametric", "NOT_MEASURABLE", "high", "pass"),  # never fails
    ("must", "APPROXIMATED", "text_prompt_global", "NOT_OBSERVED", "high", "retry"),  # not editorial
    ("must", "APPROXIMATED", "text_prompt_global", "PARTIAL", "high", "warn"),
    ("must", "APPROXIMATED", "editorial_cutaway", "NOT_OBSERVED", "high", "pass"),  # expected for the method
    ("should", "HONORED", "native_parametric", "NOT_OBSERVED", "high", "pass"),  # ranks takes only
    ("nice", "HONORED", "native_parametric", "CONTRADICTED", "high", "pass"),  # informational
]


@pytest.mark.parametrize(("priority", "level", "method", "verdict", "reliability", "action"), MATRIX)
def test_performance_qa_follows_the_outcome_matrix(
    priority: str, level: str, method: str, verdict: str, reliability: str, action: str
) -> None:
    control = ctl(
        "gaze",
        "look_away",
        "/scenes[scn_hook]/acting/events[ev_1]",
        priority=priority,
        observation_reliability=reliability,
    )
    entries, controls = _entries([(control, level, method, verdict)])
    if method == "editorial_cutaway":  # the cut happened where the compiler planned it
        entries = [e.model_copy(update={"approximation_executed": True}) for e in entries]
    decision = decide(entries, controls, POLICY, voice_locked=False)
    assert decision.action == action, (decision.as_dict(), entries[0].outcome)


def test_an_unexecuted_editorial_approximation_is_a_render_defect() -> None:
    control = ctl("gaze", "look_away", "/scenes[scn_hook]/acting/events[ev_1]")
    entries, controls = _entries([(control, "APPROXIMATED", "editorial_cutaway", "NOT_OBSERVED")])
    entries = [e.model_copy(update={"approximation_executed": False}) for e in entries]
    assert decide(entries, controls, POLICY, voice_locked=False).action == "render_defect"


def test_audio_items_retry_the_voice_or_go_to_review_when_it_is_locked() -> None:
    control = ctl("prosody_emphasis", "emphasize", "/script/segments[seg_1]/annotations[an_1]")
    entries, controls = _entries([(control, "HONORED", "native_parametric", "NOT_OBSERVED")])
    audio = frozenset({"prosody_emphasis"})
    free = decide(entries, controls, POLICY, voice_locked=False, audio_dimensions=audio)
    locked = decide(entries, controls, POLICY, voice_locked=True, audio_dimensions=audio)
    assert free.retries[0]["rerun"] == ["tts.segment"] and locked.retries[0]["rerun"] == ["needs_review"]
