"""Proxy calibration (§16.2): scores through the judge's own measures, F1 → reliability class, and
the calibrated vocabulary the judge uses for tracks of the calibrated analyzer revision."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from ce_behavior.calibration import (
    LabelledEvent,
    ProxyCalibration,
    calibrated_vocabulary,
    proxy_scores,
    reliability_class,
    speech_rate_scores,
)
from ce_config.loader import load_config

ROOT = Path(__file__).resolve().parents[4]
BUNDLE = load_config(ROOT / "config", "test")
VOCAB = BUNDLE.vocab
JUDGE = BUNDLE.app.behavior.judge


def _clip(
    events: list[tuple[float, float]], hz: float = 10.0, duration: float = 30.0, spurious: Sequence[float] = ()
) -> tuple[dict, float, float, list[LabelledEvent]]:  # type: ignore[type-arg]
    t = np.arange(int(duration * hz)) / hz
    gaze = np.full_like(t, 3.0)
    for a, b in events:
        gaze[(t >= a) & (t <= b)] = 20.0
    for s in spurious:
        gaze[(t >= s) & (t <= s + 0.6)] = 20.0
    return {"gaze_deviation_deg": gaze.tolist()}, hz, duration, [LabelledEvent("look_away", a, b) for a, b in events]


def test_scores_count_detections_in_positive_and_negative_windows() -> None:
    proxy = VOCAB.proxies["look_away"]
    clip = _clip([(2.0, 2.8), (8.0, 8.9), (15.0, 15.6)], spurious=[22.0])
    tp, fp, fn, tn = proxy_scores("look_away", proxy, [clip], JUDGE)
    assert (tp, fn) == (3, 0) and fp >= 1 and tn > 5
    missed = ({"gaze_deviation_deg": [3.0] * 300}, 10.0, 30.0, [LabelledEvent("look_away", 5.0, 5.8)])
    assert proxy_scores("look_away", proxy, [missed], JUDGE)[:3] == (0, 0, 1)


def test_reliability_classes_and_speech_rate() -> None:
    assert reliability_class(0.9, 40) == "high" and reliability_class(0.7, 40) == "medium"
    assert reliability_class(0.5, 40) == "low" and reliability_class(0.99, 5) == "low"
    scores = speech_rate_scores(
        [(0.75, 110, 150), (0.85, 130, 150), (1.1, 168, 150), (1.25, 190, 150), (0.95, 150, 150)],
        slow_threshold=0.9,
        fast_threshold=1.0,
    )
    assert scores["speech_rate_slow"] == (2, 0, 0, 3)
    tp, _fp, fn, _tn = scores["speech_rate_fast"]
    assert tp == 2 and fn == 0


def test_calibrated_vocabulary_applies_only_to_the_calibrated_revision() -> None:
    calibration = ProxyCalibration(
        "look_away", "face.landmarks", "mediapipe_face", "float16/1", 6, 4, 2, 30, "low", "fixture_clips", "t"
    )
    row = {
        "adapter_id": "mediapipe_face",
        "revision": "float16/1",
        "dimension": "look_away",
        "measured": calibration.measured(),
    }
    vocab = calibrated_vocabulary(VOCAB, [row], {"face.landmarks": ("mediapipe_face", "float16/1")})
    assert vocab.proxies["look_away"].reliability == "low" and vocab.proxies["look_away"].calibrated_confidence == 0.6
    assert VOCAB.proxies["look_away"].reliability == "high"  # the hypothesis is untouched
    other = calibrated_vocabulary(VOCAB, [row], {"face.landmarks": ("mock_observer", "1")})
    assert other is VOCAB
