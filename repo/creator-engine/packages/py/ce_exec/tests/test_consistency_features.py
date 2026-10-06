"""The consistency job reads its features from the outputs the build actually writes (Phase 14 fix):
the CBS's `emotion_visual`/`emotion_vocal` controls, `post.camera`'s `motion.peak_px`, and the
analyzers' track shapes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ce_exec.consistency_job import _features_for
from ce_testing.fixtures import example_spec


def _out(data: dict[str, Any]) -> Any:
    return SimpleNamespace(data=data)


def test_features_come_from_the_outputs_the_build_writes() -> None:
    spec = example_spec()
    control = {"character_key": "char_alex"}
    outputs: dict[str, list[tuple[str, Any]]] = {
        "behavior.resolve": [
            (
                "behavior.resolve:scn_hook",
                _out(
                    {
                        "content": {
                            "requested_controls": [
                                # one item requests both channels: counted once, the visual state wins
                                {**control, "item_ref": "/a", "dimension": "emotion_visual", "value": "curious"},
                                {**control, "item_ref": "/a", "dimension": "emotion_vocal", "value": "warm"},
                                {**control, "item_ref": "/b", "dimension": "emotion_vocal", "value": "confident"},
                                {**control, "item_ref": "/c", "dimension": "gaze", "value": "to_camera"},
                                {
                                    "character_key": "char_other",
                                    "item_ref": "/d",
                                    "dimension": "emotion_visual",
                                    "value": "sad",
                                },
                            ]
                        }
                    }
                ),
            )
        ],
        "post.camera": [
            ("post.camera:sht_1", _out({"motion": {"type": "handheld", "peak_px": 6.5}})),
            ("post.camera:sht_2", _out({"motion": {"type": "static", "peak_px": 0.0}})),  # not her shot
        ],
        "behavior.observe": [
            (
                "behavior.observe:sht_1",
                _out(
                    {
                        "observed": {
                            "analyzers": [{"adapter_id": "mediapipe_face"}],
                            "tracks": [
                                {
                                    "character_key": "char_alex",
                                    "sample_hz": 5.0,
                                    "series": {"gaze_deviation_deg": [1.0] * 10, "head_motion_energy": [0.2] * 10},
                                    "events": [{"type": "blink", "start_s": 0.5, "end_s": 0.6}],
                                }
                            ],
                        }
                    }
                ),
            )
        ],
    }
    features = _features_for({"key": "char_alex", "signature_phrases": []}, spec, outputs, {})
    assert features["emotion_range"]["value"] == 2.0 and features["emotion_range"]["n"] == 2
    assert features["emotion_transitions"]["value"] == 1.0
    assert features["camera_motion_energy"]["value"] == 6.5 and features["camera_motion_energy"]["n"] == 1
    assert features["blink_rate"]["value"] == 30.0  # one blink in two seconds
    assert features["head_motion_energy"]["value"] == 0.2
    assert features["head_motion_energy"]["mock"] is False
    assert "face_size_ratio" not in features  # no analyzer measures it: left unmeasured, not invented
