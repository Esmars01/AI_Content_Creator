"""Judgement (§16.2–§16.3): verdicts of measured tracks against requested controls.

Measurement (the analyzers' tracks, `ObservedBehavior`) is expensive and keyed by the take;
judgement is cheap and recomputed whenever requests change. For each requested control the best
proxy of `config/vocab/observation_proxies.yaml` for its (dimension, label) is evaluated:

- CONFIRMED: the measure passes the proxy's threshold within its timing tolerance;
- PARTIAL: present but weaker, shorter or mistimed (≥ `behavior.judge.partial_fraction` of it);
- NOT_OBSERVED: absent;
- CONTRADICTED: the opposite measurable behavior over at least `contradiction_ratio` of the window;
- NOT_MEASURABLE: no implemented proxy, the analyzer did not run, its series is missing, or its
  confidence is below the proxy's minimum (never upgraded into a pass, §16.8);
- NOT_APPLICABLE: nothing observable in this artifact (the character is not in the take, a vocal
  item against video only).

VLM window questions (`vision.video`) and the audio-emotion classifier are not run before their
phases (11 and after the license review), so their proxies report NOT_MEASURABLE honestly.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from ce_config.schemas import JudgeConfig
from ce_core.behavior.cbs import RequestedControl
from ce_core.behavior.observed import CharacterTracks, ItemObservation
from ce_core.enums import ObservationVerdict
from ce_core.vocab import ProxyDef, Vocabulary

__all__ = [
    "AudioEvidence",
    "VisualEvidence",
    "control_label",
    "judge_audio",
    "judge_visual",
    "proxies_for",
]

V = ObservationVerdict
RELIABILITY_RANK = {"high": 2, "medium": 1, "low": 0}
Word = tuple[str, int]

# Proxies with an implemented measure, and the series each needs.
VISUAL_SERIES = {
    "look_away": "gaze_deviation_deg",
    "hold_camera": "gaze_deviation_deg",
    "smile_score": "smile",
    "brow_raise_score": "brow_raise",
    "frown_score": "brow_down",
    "jaw_open_score": "jaw_open",
    "blink_event": "eye_closure",
    "nod": "head_pitch_deg",
    "head_shake": "head_yaw_deg",
    "head_tilt": "head_roll_deg",
    "head_motion_low": "head_yaw_deg",
    "gesture_energy_high": "gesture_energy",
    "gesture_energy_low": "gesture_energy",
    "lean_in": "shoulder_width_ratio",
    "lean_back": "shoulder_width_ratio",
}
AUDIO_PROXIES = {
    "speech_rate_slow",
    "speech_rate_fast",
    "pause_at_anchor",
    "pause_ratio_high",
    "emphasis_word",
    "delivery_whisper",
    "energy_level",
}
# A requested energy difference below this counts as "the same level as the rest".
ENERGY_DELTA_EPS = 0.05
ENERGY_SAME_BAND = 0.1
CONFIDENCE = {"face.landmarks": 0.85, "body.landmarks": 0.7, "audio.prosody": 0.9, "asr.align": 0.9}
AWAY_LABELS = {"avoid_camera"}  # gaze strategies held away from the camera (ratio, not an event)


def control_label(control: RequestedControl) -> str:
    """The vocabulary token a control requests: `displayed:confident@0.6;…` → `confident`,
    `look_away:down_left@seg_2.w2+700ms` → `look_away`, `350ms after w3` → the annotation tag."""
    value = control.value
    if control.dimension in ("prosody_pause",):
        return "pause"
    value = value.split(";", 1)[0].removeprefix("displayed:")
    return value.split("@", 1)[0].split(":", 1)[0].split(" ", 1)[0]


def proxies_for(vocab: Vocabulary, dimension: str, label: str) -> list[tuple[str, ProxyDef]]:
    """Proxies for (dimension, label), most reliable first."""
    found = [(k, p) for k, p in vocab.proxies.items() if p.dimension == dimension and label in p.labels]
    if dimension == "prosody_pause" and label == "pause":
        found = [(k, p) for k, p in vocab.proxies.items() if k == "pause_at_anchor"]
    return sorted(found, key=lambda kp: (-RELIABILITY_RANK[str(kp[1].reliability)], kp[0]))


@dataclass(frozen=True)
class VisualEvidence:
    """One take's measured tracks for the character (None: the character is not in the take)."""

    tracks: CharacterTracks | None
    available: frozenset[str] = frozenset({"face.landmarks", "body.landmarks"})
    duration_s: float = 0.0


@dataclass(frozen=True)
class AudioEvidence:
    """Word timings and an energy track of the audio the viewer hears (the final mix)."""

    word_times: Mapping[Word, tuple[float, float]]
    order: Sequence[Word]
    energy: Sequence[float] = ()
    energy_hz: float = 10.0
    baseline_wpm: float = 150.0
    available: frozenset[str] = frozenset({"audio.prosody", "asr.align"})
    analyzers: Mapping[str, str] = field(default_factory=dict)
    # The abstract energy the CBS requests for each word (its prosody directive); energy is judged
    # relative to the rest of the speech, against the requested difference.
    requested_energy: Mapping[Word, float] = field(default_factory=dict)


def _obs(
    control: RequestedControl, verdict: ObservationVerdict, method: str, confidence: float = 0.0, **measures: float
) -> ItemObservation:
    return ItemObservation(
        item_ref=control.item_ref,
        character_key=control.character_key,
        dimension=control.dimension,
        verdict=verdict,
        measures={k: round(float(v), 4) for k, v in measures.items()},
        confidence=round(min(max(confidence, 0.0), 1.0), 4),
        method=method,
    )


def _not_measurable(control: RequestedControl, method: str, reason: str = "") -> ItemObservation:
    return _obs(control, V.NOT_MEASURABLE, method + (f" ({reason})" if reason else ""))


def _mask(n: int, hz: float, t0: float, t1: float) -> np.ndarray:
    t = np.arange(n) / hz
    mask = (t >= t0) & (t <= max(t1, t0 + 1.0 / hz))
    return mask


def _longest_run(flags: np.ndarray, hz: float) -> float:
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best / hz


def judge_visual(
    control: RequestedControl,
    window: tuple[float, float],
    evidence: VisualEvidence,
    vocab: Vocabulary,
    config: JudgeConfig,
) -> ItemObservation:
    """Judge a visual-channel item over `window` (seconds in the take's time base)."""
    label = control_label(control)
    candidates = proxies_for(vocab, control.dimension, label)
    if evidence.tracks is None:
        return _obs(control, V.NOT_APPLICABLE, candidates[0][0] if candidates else "none")
    if not candidates:
        return _not_measurable(control, "none", "no proxy for this label")
    tracks = evidence.tracks
    if tracks.face_detected_ratio < config.min_face_ratio:
        return _not_measurable(control, candidates[0][0], "face not detected")
    for key, proxy in candidates:
        series_name = VISUAL_SERIES.get(key)
        if series_name is None or proxy.analyzer not in evidence.available or series_name not in tracks.series:
            continue
        if not proxy.thresholds and key not in ("head_motion_low",):
            continue
        base = (
            proxy.calibrated_confidence
            if proxy.calibrated_confidence is not None
            else CONFIDENCE.get(proxy.analyzer, 0.6)
        )
        confidence = base * tracks.face_detected_ratio
        if confidence < proxy.min_confidence:
            return _not_measurable(control, key, "analyzer confidence too low")
        values = np.asarray(tracks.series[series_name], dtype=float)
        hz = tracks.sample_hz
        tol = proxy.tolerance_ms / 1000.0
        t0, t1 = window
        verdict, measures = _visual_measure(key, proxy, label, values, hz, (t0, t1), tol, config)
        return _obs(control, verdict, key, confidence, **measures)
    best = candidates[0]
    reason = "analyzer not run" if best[1].analyzer not in evidence.available else "proxy not measurable here"
    return _not_measurable(control, best[0], reason)


def _relative(values: np.ndarray, mask: np.ndarray) -> float | None:
    inside, outside = values[mask], values[~mask]
    if inside.size == 0 or outside.size == 0 or float(np.mean(outside)) == 0.0:
        return None
    return float(np.mean(inside) / np.mean(outside))


def _visual_measure(
    key: str,
    proxy: ProxyDef,
    label: str,
    values: np.ndarray,
    hz: float,
    window: tuple[float, float],
    tol: float,
    config: JudgeConfig,
) -> tuple[ObservationVerdict, dict[str, float]]:
    t0, t1 = window
    th = proxy.thresholds
    frac = config.partial_fraction
    n = values.size
    if key == "look_away" and label not in AWAY_LABELS:
        mask = _mask(n, hz, t0 - tol, t1 + tol)
        theta = th.get("gaze_deviation_deg", 12.0)
        need = th.get("min_duration_ms", 250.0) / 1000.0
        seg = values[mask]
        run = _longest_run(seg > theta, hz)
        peak = float(seg.max(initial=0.0))
        measures = {"gaze_deviation_deg_max": peak, "look_away_ms": run * 1000.0}
        if run >= need:
            return V.CONFIRMED, measures
        if run >= frac * need or (run > 0 and peak >= theta):
            return V.PARTIAL, measures
        return V.NOT_OBSERVED, measures
    if key in ("look_away", "hold_camera"):
        mask = _mask(n, hz, t0, t1)
        seg = values[mask]
        held = float(np.mean(seg < th.get("gaze_deviation_deg", 8.0))) if seg.size else 0.0
        away = float(np.mean(seg > 12.0)) if seg.size else 0.0
        measures = {"held_ratio": held, "away_ratio": away}
        if key == "look_away":  # avoid_camera: held away from the camera
            target = 0.5
            if away >= target:
                return V.CONFIRMED, measures
            if away >= frac * target:
                return V.PARTIAL, measures
            return (V.CONTRADICTED if held >= config.contradiction_ratio else V.NOT_OBSERVED), measures
        target = th.get("min_ratio", 0.7)
        if held >= target:
            return V.CONFIRMED, measures
        if away >= config.contradiction_ratio:
            return V.CONTRADICTED, measures
        if held >= frac * target:
            return V.PARTIAL, measures
        return V.NOT_OBSERVED, measures
    if key in ("smile_score", "brow_raise_score", "frown_score", "jaw_open_score"):
        mask = _mask(n, hz, t0 - tol, t1 + tol)
        theta = next(iter(th.values()))
        seg = values[mask]
        level = float(np.percentile(seg, 90)) if seg.size else 0.0
        measures = {"p90": level, "threshold": theta}
        if level >= theta:
            return V.CONFIRMED, measures
        if level >= frac * theta + (1 - frac) * 0.2:
            return V.PARTIAL, measures
        return V.NOT_OBSERVED, measures
    if key == "blink_event":
        mask = _mask(n, hz, t0 - tol, t1 + tol)
        run = _longest_run(values[mask] >= th.get("eye_closure", 0.6), hz)
        need = th.get("min_duration_ms", 250.0) / 1000.0
        measures = {"closure_ms": run * 1000.0}
        if run >= need:
            return V.CONFIRMED, measures
        return (V.PARTIAL if run >= frac * need or run > 0 else V.NOT_OBSERVED), measures
    if key in ("nod", "head_shake", "head_tilt"):
        mask = _mask(n, hz, t0 - tol, t1 + tol)
        seg = values[mask]
        amplitude = float((seg.max(initial=0.0) - seg.min(initial=0.0)) / 2.0) if seg.size else 0.0
        theta = next(iter(th.values()))
        measures = {"amplitude_deg": amplitude}
        if amplitude >= theta:
            return V.CONFIRMED, measures
        return (V.PARTIAL if amplitude >= frac * theta else V.NOT_OBSERVED), measures
    tolerance = config.relative_tolerance
    mask = _mask(n, hz, t0, t1)
    if key == "head_motion_low":
        motion = np.abs(np.diff(values, prepend=values[:1]))
        rel = _relative(motion, mask)
        if rel is None:
            return V.NOT_MEASURABLE, {}
        theta = th.get("relative_energy", 0.8)
        measures = {"relative_energy": rel}
        if rel <= theta + tolerance:
            return V.CONFIRMED, measures
        if rel >= 1.3:
            return V.CONTRADICTED, measures
        return (V.PARTIAL if rel <= 1.0 else V.NOT_OBSERVED), measures
    if key in ("gesture_energy_high", "gesture_energy_low"):
        rel = _relative(values, mask)
        if rel is None:
            return V.NOT_MEASURABLE, {}
        theta = th.get("relative_energy", 1.3 if key.endswith("high") else 1.0)
        measures = {"relative_energy": rel}
        if key.endswith("high"):
            if rel >= theta - tolerance:
                return V.CONFIRMED, measures
            if rel >= 1.0 + frac * (theta - 1.0):
                return V.PARTIAL, measures
            return (V.CONTRADICTED if rel <= 0.8 else V.NOT_OBSERVED), measures
        if rel <= theta + tolerance:
            return V.CONFIRMED, measures
        if rel >= 1.3:
            return V.CONTRADICTED, measures
        return V.PARTIAL, measures
    if key in ("lean_in", "lean_back"):
        seg = values[mask]
        level = float(np.mean(seg)) if seg.size else 1.0
        theta = th.get("shoulder_width_ratio", 1.06 if key == "lean_in" else 0.95)
        measures = {"shoulder_width_ratio": level}
        if key == "lean_in":
            if level >= theta - tolerance / 3:
                return V.CONFIRMED, measures
            if level >= 1.0 + frac * (theta - 1.0):
                return V.PARTIAL, measures
            return (V.CONTRADICTED if level <= 0.95 else V.NOT_OBSERVED), measures
        if level <= theta + tolerance / 3:
            return V.CONFIRMED, measures
        if level <= 1.0 - frac * (1.0 - theta):
            return V.PARTIAL, measures
        return (V.CONTRADICTED if level >= 1.06 else V.NOT_OBSERVED), measures
    return V.NOT_MEASURABLE, {}


# ---------------------------------------------------------------------- audio


def judge_audio(
    control: RequestedControl,
    words: Sequence[Word],
    evidence: AudioEvidence,
    vocab: Vocabulary,
    config: JudgeConfig,
) -> ItemObservation:
    """Judge an audio-channel item over its words, on the audio the viewer hears."""
    label = control_label(control)
    candidates = proxies_for(vocab, control.dimension, label)
    if not candidates:
        return _not_measurable(control, "none", "no proxy for this label")
    timed = [w for w in words if w in evidence.word_times]
    if not timed:
        return _obs(control, V.NOT_APPLICABLE, candidates[0][0])
    for key, proxy in candidates:
        if key not in AUDIO_PROXIES or proxy.analyzer not in evidence.available:
            continue
        if key in ("emphasis_word", "delivery_whisper", "energy_level") and not evidence.energy:
            continue
        confidence = (
            proxy.calibrated_confidence
            if proxy.calibrated_confidence is not None
            else CONFIDENCE.get(proxy.analyzer, 0.6)
        )
        verdict, measures = _audio_measure(key, proxy, control, timed, evidence, config)
        if verdict == V.NOT_MEASURABLE:
            continue
        return _obs(control, verdict, key, confidence, **measures)
    best = candidates[0]
    reason = "analyzer not run" if best[1].analyzer not in evidence.available else "proxy not measurable here"
    return _not_measurable(control, best[0], reason)


def _energy(evidence: AudioEvidence, t0: float, t1: float) -> float:
    hz = evidence.energy_hz
    a, b = int(t0 * hz), max(int(t1 * hz), int(t0 * hz) + 1)
    values = list(evidence.energy[a:b])
    return float(np.mean(values)) if values else 0.0


def _audio_measure(
    key: str,
    proxy: ProxyDef,
    control: RequestedControl,
    words: Sequence[Word],
    evidence: AudioEvidence,
    config: JudgeConfig,
) -> tuple[ObservationVerdict, dict[str, float]]:
    th = proxy.thresholds
    frac = config.partial_fraction
    tol = config.relative_tolerance
    times = evidence.word_times
    if key in ("speech_rate_slow", "speech_rate_fast"):
        if len(words) < 2:
            return V.NOT_MEASURABLE, {}
        span = times[words[-1]][1] - times[words[0]][0]
        if span <= 0:
            return V.NOT_MEASURABLE, {}
        wpm = len(words) / span * 60.0
        rel = wpm / max(evidence.baseline_wpm, 1.0)
        theta = th.get("relative_rate", 0.9 if key.endswith("slow") else 1.0)
        measures = {"wpm": wpm, "relative_rate": rel}
        if key.endswith("slow"):
            if rel <= theta + tol:
                return V.CONFIRMED, measures
            if rel >= 1.05 + tol:
                return V.CONTRADICTED, measures
            return (V.PARTIAL if rel <= theta + (1 - frac) * 0.1 + tol else V.NOT_OBSERVED), measures
        if rel >= theta - tol:
            return V.CONFIRMED, measures
        if rel <= 0.9 - tol:
            return V.CONTRADICTED, measures
        return V.PARTIAL, measures
    if key == "pause_at_anchor":
        anchor = words[-1]
        order = list(evidence.order)
        index = order.index(anchor) if anchor in order else -1
        if index < 0 or index + 1 >= len(order) or order[index + 1] not in times:
            return V.NOT_MEASURABLE, {}
        gap = (times[order[index + 1]][0] - times[anchor][1]) * 1000.0
        requested = float(control.value.split("ms", 1)[0]) if control.value[:1].isdigit() else 300.0
        need = th.get("min_ratio_of_requested", 0.7) * requested
        measures = {"gap_ms": gap, "requested_ms": requested}
        if gap >= need:
            return V.CONFIRMED, measures
        return (V.PARTIAL if gap >= frac * need else V.NOT_OBSERVED), measures
    if key == "pause_ratio_high":
        inside = _pause_share(words, times)
        rest = [w for w in evidence.order if w in times and w not in set(words)]
        outside = _pause_share(rest, times) if len(rest) >= 2 else None
        if outside is None or outside <= 0:
            return V.NOT_MEASURABLE, {}
        rel = inside / outside
        theta = th.get("relative_pause_share", 1.3)
        measures = {"pause_share": inside, "relative_pause_share": rel}
        if rel >= theta - tol:
            return V.CONFIRMED, measures
        return (V.PARTIAL if rel >= 1.0 + frac * (theta - 1.0) else V.NOT_OBSERVED), measures
    if key == "emphasis_word":
        target = list(words)
        order = list(evidence.order)
        first = order.index(target[0]) if target[0] in order else -1
        neighbors = [
            order[i]
            for i in (first - 2, first - 1, first + len(target), first + len(target) + 1)
            if 0 <= i < len(order) and order[i] in times
        ]
        if first < 0 or not neighbors:
            return V.NOT_MEASURABLE, {}
        level = float(np.mean([_energy(evidence, *times[w]) for w in target]))
        around = float(np.mean([_energy(evidence, *times[w]) for w in neighbors]))
        if around <= 0:
            return V.NOT_MEASURABLE, {}
        ratio = level / around
        measures = {"energy_ratio": ratio}
        if ratio >= 1.15:
            return V.CONFIRMED, measures
        return (V.PARTIAL if ratio >= 1.0 + frac * 0.15 else V.NOT_OBSERVED), measures
    if key == "delivery_whisper":
        order = list(evidence.order)
        first = order.index(words[0]) if words[0] in order else -1
        last = order.index(words[-1]) if words[-1] in order else -1
        neighbors = [order[i] for i in (first - 1, last + 1) if 0 <= i < len(order) and order[i] in times]
        if first < 0 or not neighbors:
            return V.NOT_MEASURABLE, {}
        level = float(np.mean([_energy(evidence, *times[w]) for w in words]))
        around = float(np.mean([_energy(evidence, *times[w]) for w in neighbors]))
        if level <= 0 or around <= 0:
            return V.NOT_MEASURABLE, {}
        db = 20.0 * float(np.log10(level / around))
        theta = th.get("relative_energy_db", -8.0)
        measures = {"relative_energy_db": db}
        if db <= theta:
            return V.CONFIRMED, measures
        return (V.PARTIAL if db <= frac * theta else V.NOT_OBSERVED), measures
    if key == "energy_level":
        return _energy_level(words, evidence, tol, frac)
    return V.NOT_MEASURABLE, {}


def _energy_level(
    words: Sequence[Word], evidence: AudioEvidence, tol: float, frac: float
) -> tuple[ObservationVerdict, dict[str, float]]:
    """Median word energy inside the item relative to the rest of the speech, judged against the
    requested difference (louder, quieter or the same level). Medians keep emphasized words from
    dominating."""
    times, requested = evidence.word_times, evidence.requested_energy
    inside = [w for w in words if w in times and w in requested]
    chosen = set(words)
    rest = [w for w in evidence.order if w in times and w in requested and w not in chosen]
    if not inside or len(rest) < 2:
        return V.NOT_MEASURABLE, {}
    level = float(np.median([_energy(evidence, *times[w]) for w in inside]))
    around = float(np.median([_energy(evidence, *times[w]) for w in rest]))
    if level <= 0 or around <= 0:
        return V.NOT_MEASURABLE, {}
    rel = level / around
    expected = float(np.mean([requested[w] for w in inside]) - np.mean([requested[w] for w in rest]))
    measures = {"relative_energy": rel, "requested_delta": round(expected, 4)}
    if expected > ENERGY_DELTA_EPS:
        if rel >= 1.0 + tol:
            return V.CONFIRMED, measures
        return (V.CONTRADICTED if rel <= 1.0 - tol else V.PARTIAL), measures
    if expected < -ENERGY_DELTA_EPS:
        if rel <= 1.0 - tol:
            return V.CONFIRMED, measures
        return (V.CONTRADICTED if rel >= 1.0 + tol else V.PARTIAL), measures
    if abs(rel - 1.0) <= ENERGY_SAME_BAND:
        return V.CONFIRMED, measures
    return (V.PARTIAL if abs(rel - 1.0) <= ENERGY_SAME_BAND / frac else V.NOT_OBSERVED), measures


def _pause_share(words: Sequence[Word], times: Mapping[Word, tuple[float, float]]) -> float:
    timed = [times[w] for w in words if w in times]
    if len(timed) < 2:
        return 0.0
    span = timed[-1][1] - timed[0][0]
    gaps = sum(max(0.0, b[0] - a[1]) for a, b in itertools.pairwise(timed) if b[0] - a[1] >= 0.2)
    return gaps / span if span > 0 else 0.0
