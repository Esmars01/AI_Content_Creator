"""Calibration of the observation proxies (§16.2, Phase 7).

The reliability classes shipped in `observation_proxies.yaml` are initial hypotheses. Calibration
measures each proxy of a given analyzer revision against labelled material and stores the result
per revision (`model_behavior_profiles`, `translator_version = proxy_calibration`, ADR 0045):

- visual proxies on fixture clips with labelled events (consented or permissively licensed
  recordings, §16.7 and §41 — without them calibration skips with the reason);
- speech-rate proxies on synthetic speech with known rates (the CPU TTS at controlled speeds).

`proxy_scores(...)` counts detections in labelled positive windows (true positives) and in
negative windows between events (false positives) through the judge's own measure, so calibration
evaluates exactly what QC runs. F1 maps to a reliability class (`config/qc/behavior.yaml`
`calibration`); precision becomes the proxy's calibrated confidence. `calibrated_vocabulary(...)`
returns the vocabulary the judge uses for tracks of that analyzer revision.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from ce_config.schemas import JudgeConfig
from ce_core.enums import ObservationVerdict
from ce_core.vocab import ProxyDef, Vocabulary

from ce_behavior.judge import VISUAL_SERIES, _visual_measure

__all__ = [
    "PROFILE_TRANSLATOR",
    "LabelledEvent",
    "ProxyCalibration",
    "calibrated_vocabulary",
    "proxy_scores",
    "reliability_class",
    "speech_rate_scores",
]

PROFILE_TRANSLATOR = "proxy_calibration"  # model_behavior_profiles.translator_version of these rows


@dataclass(frozen=True)
class LabelledEvent:
    proxy: str  # the proxy key the event should trigger (look_away, smile_score, blink_event, …)
    start_s: float
    end_s: float


@dataclass(frozen=True)
class ProxyCalibration:
    proxy: str
    analyzer: str
    adapter_id: str
    revision: str
    tp: int
    fp: int
    fn: int
    tn: int
    reliability: str
    method: str
    fixture_set: str

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def measured(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "n": self.n,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "reliability": self.reliability,
            "confidence": round(self.precision, 4),
            "analyzer": self.analyzer,
            "method": self.method,
            "fixture_set": self.fixture_set,
        }


def reliability_class(f1: float, n: int, *, high: float = 0.85, medium: float = 0.65, min_n: int = 20) -> str:
    """Too few labelled windows prove nothing: they keep the lowest class."""
    if n < min_n:
        return "low"
    return "high" if f1 >= high else ("medium" if f1 >= medium else "low")


def _detected(verdict: ObservationVerdict) -> bool:
    return verdict == ObservationVerdict.CONFIRMED


def proxy_scores(
    proxy_key: str,
    proxy: ProxyDef,
    clips: Sequence[tuple[Mapping[str, Sequence[float]], float, float, Sequence[LabelledEvent]]],
    config: JudgeConfig,
) -> tuple[int, int, int, int]:
    """(tp, fp, fn, tn) of a visual proxy over clips `(series, sample_hz, duration_s, events)`:
    each labelled event of this proxy is a positive window; equally long gaps between events
    (outside the timing tolerance) are negative windows."""
    series_name = VISUAL_SERIES.get(proxy_key)
    tp = fp = fn = tn = 0
    if series_name is None:
        return tp, fp, fn, tn
    tol = proxy.tolerance_ms / 1000.0
    label = proxy.labels[0] if proxy.labels else proxy_key
    for series, hz, duration, events in clips:
        if series_name not in series:
            continue
        values = np.asarray(series[series_name], dtype=float)
        mine = sorted((e for e in events if e.proxy == proxy_key), key=lambda e: e.start_s)
        for event in mine:
            verdict, _ = _visual_measure(proxy_key, proxy, label, values, hz, (event.start_s, event.end_s), tol, config)
            tp, fn = (tp + 1, fn) if _detected(verdict) else (tp, fn + 1)
        busy = sorted((e.start_s - tol, e.end_s + tol) for e in events)
        width = max((e.end_s - e.start_s for e in mine), default=1.0)
        t = 0.0
        while t + width <= duration:
            window = (t, t + width)
            if all(window[1] <= a or window[0] >= b for a, b in busy):
                verdict, _ = _visual_measure(proxy_key, proxy, label, values, hz, window, tol, config)
                fp, tn = (fp + 1, tn) if _detected(verdict) else (fp, tn + 1)
            t += width
    return tp, fp, fn, tn


def speech_rate_scores(
    trials: Iterable[tuple[float, float, float]], *, slow_threshold: float, fast_threshold: float
) -> dict[str, tuple[int, int, int, int]]:
    """Speech-rate proxies over `(true_relative_rate, measured_wpm, baseline_wpm)` trials: a trial is
    positive for `speech_rate_slow` when its true rate is below the threshold, for
    `speech_rate_fast` when at or above it."""
    out = {"speech_rate_slow": [0, 0, 0, 0], "speech_rate_fast": [0, 0, 0, 0]}
    for truth, measured, baseline in trials:
        relative = measured / baseline if baseline else 0.0
        for key, positive, detected in (
            ("speech_rate_slow", truth < slow_threshold, relative < slow_threshold),
            ("speech_rate_fast", truth >= fast_threshold, relative >= fast_threshold),
        ):
            cell = out[key]
            if positive and detected:
                cell[0] += 1
            elif not positive and detected:
                cell[1] += 1
            elif positive:
                cell[2] += 1
            else:
                cell[3] += 1
    return {k: (v[0], v[1], v[2], v[3]) for k, v in out.items()}


def calibrated_vocabulary(
    vocab: Vocabulary, rows: Iterable[Mapping[str, Any]], analyzers: Mapping[str, tuple[str, str]]
) -> Vocabulary:
    """`vocab` with the measured reliability and confidence of every proxy whose analyzer
    capability (`face.landmarks`, …) was served by the calibrated adapter revision in `analyzers`
    (capability → (adapter_id, revision)). Proxies without a matching row keep their hypotheses."""
    measured: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        data = row.get("measured", {})
        capability = str(data.get("analyzer", ""))
        if analyzers.get(capability) == (str(row.get("adapter_id")), str(row.get("revision"))):
            measured[str(row.get("dimension"))] = data
    if not measured:
        return vocab
    proxies = {
        key: proxy.model_copy(
            update={
                "reliability": str(measured[key]["reliability"]),
                "calibrated_confidence": float(measured[key]["confidence"]),
            }
        )
        if key in measured
        else proxy
        for key, proxy in vocab.proxies.items()
    }
    return dataclasses.replace(vocab, proxies=proxies)
