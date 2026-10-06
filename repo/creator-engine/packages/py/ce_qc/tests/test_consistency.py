"""Creator consistency (§20): features, rolling baselines, bands and verdicts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_config.loader import load_config
from ce_config.schemas import ConsistencyQC
from ce_core.behavior.observed import CharacterTracks, TrackEvent
from ce_qc.consistency import RELATIVE_SPREAD, compare, rolling_baseline, speech_features, video_features

_LOADED = load_config(Path(__file__).resolve().parents[4] / "config", "test").qc_consistency
assert _LOADED is not None
CONFIG: ConsistencyQC = _LOADED


def test_speech_features_from_spoken_segments() -> None:
    features = speech_features(
        [
            {"text": "Okay so, um, here is the thing. You know what I mean?", "duration_s": 4.0, "pauses": 1},
            {"text": "Real talk: it works.", "duration_s": 2.0, "pauses": 0},
        ],
        signature_phrases=["real talk"],
    )
    assert round(features["wpm"]["value"]) == 160  # 16 words in 6 s
    assert features["pause_rate"]["value"] == 10.0
    assert features["filler_rate"]["value"] > 0
    assert features["signature_phrase_rate"]["value"] > 0


def test_behavior_features_from_tracks() -> None:
    # the shapes the analyzers emit (`CharacterTracks`, events with `type`; the MediaPipe body
    # analyzer's `head_motion_energy` series), as stored in observation outputs
    tracks = [
        CharacterTracks(
            character_key="char_host",
            sample_hz=5.0,
            series={"gaze_deviation_deg": [2.0, 3.0, 25.0, 4.0, 1.0], "head_motion_energy": [0.1] * 5},
            events=[TrackEvent(type="blink", start_s=0.2, end_s=0.3), TrackEvent(type="smile", start_s=0.4, end_s=0.9)],
        ).model_dump(mode="json")
    ]
    features = video_features(tracks=tracks, tracks_mock=True)
    assert features["gaze_to_camera_ratio"]["value"] == 0.8
    assert features["blink_rate"]["value"] == 60.0  # one blink in one second
    assert features["smile_frequency"]["value"] == 60.0
    assert features["head_motion_energy"]["value"] == 0.1 and features["head_motion_energy"]["mock"] is True
    # tracks stored with the older `kind` key still count
    legacy = [{"sample_hz": 5.0, "series": {"gaze_deviation_deg": [0.0] * 5}, "events": [{"kind": "blink"}]}]
    assert video_features(tracks=legacy)["blink_rate"]["value"] == 60.0


def test_rolling_baseline_merges_creator_test_and_videos() -> None:
    baseline = rolling_baseline(
        [{"wpm": {"value": 150.0}}, {"wpm": {"value": 170.0}}, {"wpm": 160.0, "identity_similarity": {"p50": 0.8}}],
        window=20,
    )
    assert baseline["wpm"] == {"mean": 160.0, "std": 10.0, "n": 3}
    assert baseline["face_similarity"]["mean"] == 0.8 and baseline["face_similarity"]["std"] is None


def test_bands_warn_by_default_and_gate_only_when_promoted() -> None:
    baseline: dict[str, Any] = {
        "wpm": {"mean": 150.0, "std": 5.0, "n": 5},
        "head_motion_energy": {"mean": 0.2, "std": None, "n": 1},
    }
    features = {
        "face_similarity": {"value": 0.9, "n": 4, "method": "m", "mock": False},
        "voice_similarity": {"value": 0.5, "n": 4, "method": "m", "mock": False},
        "wpm": {"value": 175.0, "n": 2, "method": "m", "mock": False},
        "head_motion_energy": {"value": 0.21, "n": 2, "method": "m", "mock": True},
    }
    metrics, verdict = compare(features, baseline, CONFIG)
    assert metrics["face_identity"]["status"] == "in_band"
    assert metrics["voice_identity"]["status"] == "out_of_band"
    assert metrics["speech_style"]["status"] == "out_of_band" and metrics["speech_style"]["features"]["wpm"]["z"] == 5.0
    spread = metrics["behavior_patterns"]["features"]["head_motion_energy"]
    assert spread["in_band"] and spread["spread"] == f"{RELATIVE_SPREAD:.0%} of the mean"
    assert metrics["wardrobe"]["status"] == "not_measured"
    assert verdict == "warn"
    gated = CONFIG.model_copy(
        update={
            "metrics": {
                **CONFIG.metrics,
                "voice_identity": CONFIG.metrics["voice_identity"].model_copy(update={"gate": True}),
            }
        }
    )
    assert compare(features, baseline, gated)[1] == "out_of_band"


def test_spec_overrides_are_deviations_not_failures() -> None:
    features = {"voice_similarity": {"value": 0.2, "n": 4, "method": "m", "mock": False}}
    metrics, verdict = compare(features, {}, CONFIG, deviations=[{"dimension": "voice_identity", "reason": "override"}])
    assert metrics["voice_identity"]["status"] == "deviation" and verdict == "in_band"
