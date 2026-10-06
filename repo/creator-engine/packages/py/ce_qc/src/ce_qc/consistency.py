"""Creator consistency across videos (§20): per-video features, baselines and threshold bands.

`video_features` turns one version's measurements into features (each with its method, sample
count and `mock` flag); `rolling_baseline` merges earlier features (the Creator Test baseline and
the creator's last N accepted videos) into mean / std / n per feature; `compare` scores the
dimensions of `config/qc/consistency.yaml` against the baseline:

- similarity dimensions (face, voice, wardrobe, worlds) are in band when the median similarity
  reaches `min_similarity` / `min_score`;
- distribution dimensions (speech style, behavior patterns, emotional tendencies, camera habits)
  are in band when every feature's |z| ≤ `max_z`. A baseline of fewer than two videos has no
  spread yet: the spread is then `RELATIVE_SPREAD` of the mean (stated in each metric).

Out-of-band metrics warn unless the user promoted them to gates (`gate: true`). Spec overrides
(another voice or appearance, out-of-character acting) are intentional deviations, never failures.
Nothing here measures personality or "same person"; those are human ratings (§20, §16.8).
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ce_config.schemas import ConsistencyQC

__all__ = [
    "DIMENSION_FEATURES",
    "RELATIVE_SPREAD",
    "compare",
    "rolling_baseline",
    "speech_features",
    "video_features",
]

RELATIVE_SPREAD = 0.15
FILLERS = frozenset({"um", "uh", "erm", "like", "basically", "actually", "literally", "you know", "i mean"})
DIMENSION_FEATURES: dict[str, tuple[str, ...]] = {
    "face_identity": ("face_similarity",),
    "voice_identity": ("voice_similarity",),
    "speech_style": ("wpm", "pause_rate", "filler_rate", "sentence_length", "signature_phrase_rate"),
    "behavior_patterns": (
        "gaze_to_camera_ratio",
        "blink_rate",
        "head_motion_energy",
        "gesture_energy",
        "smile_frequency",
    ),
    "emotional_tendencies": ("emotion_range", "emotion_transitions"),
    "worlds": ("world_score",),
    "wardrobe": ("wardrobe_similarity",),
    "camera_habits": ("face_size_ratio", "camera_motion_energy"),
}
SIMILARITY = {"face_identity", "voice_identity", "worlds", "wardrobe"}


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def _feature(value: float | None, n: int, method: str, *, mock: bool = False) -> dict[str, Any] | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return {"value": round(float(value), 4), "n": n, "method": method, "mock": mock}


def speech_features(
    segments: Sequence[Mapping[str, Any]], signature_phrases: Iterable[str] = ()
) -> dict[str, dict[str, Any]]:
    """Speech style from the script segments as spoken: `{text, duration_s, pauses}` each."""
    texts = [str(s.get("text", "")) for s in segments]
    words = [w for t in texts for w in _words(t)]
    duration = sum(float(s.get("duration_s", 0.0) or 0.0) for s in segments)
    out: dict[str, dict[str, Any]] = {}
    if not words:
        return out
    if duration > 0:
        out["wpm"] = _feature(len(words) / duration * 60.0, len(segments), "words / spoken seconds")  # type: ignore[assignment]
        pauses = sum(int(s.get("pauses", 0) or 0) for s in segments)
        out["pause_rate"] = _feature(pauses / duration * 60.0, len(segments), "pauses per minute")  # type: ignore[assignment]
    joined = " ".join(words)
    fillers = sum(joined.count(f) if " " in f else words.count(f) for f in FILLERS)
    out["filler_rate"] = _feature(fillers / len(words) * 100.0, len(words), "fillers per 100 words")  # type: ignore[assignment]
    sentences = [s for t in texts for s in re.split(r"[.!?]+", t) if _words(s)]
    if sentences:
        out["sentence_length"] = _feature(  # type: ignore[assignment]
            statistics.fmean(len(_words(s)) for s in sentences), len(sentences), "words per sentence"
        )
    phrases = [p.lower() for p in signature_phrases if p.strip()]
    if phrases:
        used = sum(" ".join(_words(t)).count(" ".join(_words(p))) for t in texts for p in phrases)
        out["signature_phrase_rate"] = _feature(  # type: ignore[assignment]
            used / max(1, len(sentences)), len(sentences), "signature phrases per sentence"
        )
    return {k: v for k, v in out.items() if v is not None}


def _series_mean(tracks: Sequence[Mapping[str, Any]], name: str) -> float | None:
    values = [float(v) for t in tracks for v in dict(t.get("series", {})).get(name, []) if v is not None]
    return statistics.fmean(values) if values else None


def _event_rate(tracks: Sequence[Mapping[str, Any]], kinds: set[str]) -> float | None:
    seconds = sum(
        len(next(iter(dict(t.get("series", {})).values()), [])) / float(t.get("sample_hz", 5.0) or 5.0) for t in tracks
    )
    if seconds <= 0:
        return None
    # analyzers emit `TrackEvent.type` (`kind` is accepted for older stored tracks)
    count = sum(1 for t in tracks for e in t.get("events", []) if str(e.get("type", e.get("kind", ""))) in kinds)
    return count / seconds * 60.0


def video_features(
    *,
    face_similarity: Sequence[float] = (),
    face_mock: bool = False,
    voice_similarity: Sequence[float] = (),
    voice_mock: bool = False,
    speech: Mapping[str, dict[str, Any]] | None = None,
    tracks: Sequence[Mapping[str, Any]] = (),
    tracks_mock: bool = False,
    emotion_states: Sequence[str] = (),
    world_scores: Sequence[float] = (),
    wardrobe_similarity: Sequence[float] = (),
    wardrobe_mock: bool = False,
    face_sizes: Sequence[float] = (),
    camera_motion: Sequence[float] = (),
) -> dict[str, dict[str, Any]]:
    """One version's features for one creator (§20 table). Missing measurements are left out."""
    out: dict[str, dict[str, Any] | None] = {}
    if face_similarity:
        out["face_similarity"] = _feature(
            statistics.median(face_similarity), len(face_similarity), "face.embed cosine", mock=face_mock
        )
    if voice_similarity:
        out["voice_similarity"] = _feature(
            statistics.median(voice_similarity), len(voice_similarity), "voice.embed cosine", mock=voice_mock
        )
    for key, value in (speech or {}).items():
        out[key] = value
    if tracks:
        gaze = [float(v) for t in tracks for v in dict(t.get("series", {})).get("gaze_deviation_deg", [])]
        if gaze:
            out["gaze_to_camera_ratio"] = _feature(
                sum(1 for g in gaze if abs(g) <= 10.0) / len(gaze),
                len(gaze),
                "share of samples within 10° of camera",
                mock=tracks_mock,
            )
        for key, series, method in (
            ("head_motion_energy", "head_motion_energy", "mean head-motion energy"),  # the body analyzer's series
            ("gesture_energy", "gesture_energy", "mean gesture energy"),
        ):
            out[key] = _feature(_series_mean(tracks, series), len(tracks), method, mock=tracks_mock)
        out["blink_rate"] = _feature(_event_rate(tracks, {"blink"}), len(tracks), "blinks per minute", mock=tracks_mock)
        out["smile_frequency"] = _feature(
            _event_rate(tracks, {"smile"}), len(tracks), "smiles per minute", mock=tracks_mock
        )
    if emotion_states:
        out["emotion_range"] = _feature(
            float(len(set(emotion_states))), len(emotion_states), "distinct requested states"
        )
        transitions = sum(1 for a, b in itertools.pairwise(emotion_states) if a != b)
        out["emotion_transitions"] = _feature(float(transitions), len(emotion_states), "state changes along the video")
    if world_scores:
        out["world_score"] = _feature(statistics.median(world_scores), len(world_scores), "cross-video world score")
    if wardrobe_similarity:
        out["wardrobe_similarity"] = _feature(
            statistics.median(wardrobe_similarity),
            len(wardrobe_similarity),
            "image.embed cosine vs references",
            mock=wardrobe_mock,
        )
    if face_sizes:
        out["face_size_ratio"] = _feature(
            statistics.median(face_sizes), len(face_sizes), "face box height / frame height"
        )
    if camera_motion:
        out["camera_motion_energy"] = _feature(
            statistics.fmean(camera_motion), len(camera_motion), "camera post amplitude"
        )
    return {k: v for k, v in out.items() if v is not None}


def _value(entry: Any) -> float | None:
    if isinstance(entry, (int, float)):
        return float(entry)
    if isinstance(entry, Mapping):
        for key in ("value", "p50", "mean"):
            if isinstance(entry.get(key), (int, float)):
                return float(entry[key])
    return None


# Creator Test scorecard names → feature names (D114 baselines keep the scorecard's names)
_CREATOR_TEST = {
    "identity_similarity": "face_similarity",
    "voice_similarity": "voice_similarity",
    "wpm": "wpm",
    "head_motion_energy": "head_motion_energy",
}


def rolling_baseline(snapshots: Sequence[Mapping[str, Any]], window: int) -> dict[str, dict[str, Any]]:
    """Mean, sample standard deviation and n per feature over the newest `window` snapshots (newest
    first). Snapshots are feature maps of earlier videos or Creator Test baseline stats."""
    values: dict[str, list[float]] = {}
    for snap in list(snapshots)[:window]:
        for key, entry in snap.items():
            name = _CREATOR_TEST.get(key, key)
            v = _value(entry)
            if v is not None:
                values.setdefault(name, []).append(v)
    return {
        k: {
            "mean": round(statistics.fmean(v), 4),
            "std": round(statistics.stdev(v), 4) if len(v) > 1 else None,
            "n": len(v),
        }
        for k, v in values.items()
    }


def compare(
    features: Mapping[str, Mapping[str, Any]],
    baseline: Mapping[str, Mapping[str, Any]],
    config: ConsistencyQC,
    *,
    deviations: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, dict[str, Any]], str]:
    """Per dimension: value(s), band, status (`in_band | out_of_band | not_measured | deviation`),
    confidence and gate; and the report verdict (`in_band | warn | out_of_band`)."""
    deviated = {str(d.get("dimension")) for d in deviations}
    metrics: dict[str, dict[str, Any]] = {}
    for dimension, rule in config.metrics.items():
        names = DIMENSION_FEATURES.get(dimension, ())
        present = {n: features[n] for n in names if n in features}
        entry: dict[str, Any] = {"method": rule.method, "gate": rule.gate, "features": {}}
        if not present:
            metrics[dimension] = {**entry, "status": "not_measured", "reason": "no measurement in this video"}
            continue
        mock = any(bool(f.get("mock")) for f in present.values())
        ok = True
        if dimension in SIMILARITY:
            floor = rule.min_similarity if rule.min_similarity is not None else rule.min_score
            for name, f in present.items():
                value = float(f["value"])
                base = baseline.get(name, {})
                passed = floor is None or value >= floor
                ok = ok and passed
                entry["features"][name] = {**f, "min": floor, "baseline_mean": base.get("mean"), "in_band": passed}
        else:
            limit = rule.max_z if rule.max_z is not None else 2.0
            for name, f in present.items():
                value = float(f["value"])
                spread_base = baseline.get(name)
                if not spread_base or spread_base.get("mean") is None:
                    entry["features"][name] = {**f, "z": None, "in_band": None, "reason": "no baseline yet"}
                    continue
                mean = float(spread_base["mean"])
                spread = spread_base.get("std")
                spread_basis = "baseline std"
                if not spread:
                    spread, spread_basis = max(RELATIVE_SPREAD * abs(mean), 1e-6), f"{RELATIVE_SPREAD:.0%} of the mean"
                z = (value - mean) / float(spread)
                passed = abs(z) <= limit
                ok = ok and passed
                entry["features"][name] = {
                    **f,
                    "baseline_mean": mean,
                    "z": round(z, 3),
                    "max_z": limit,
                    "spread": spread_basis,
                    "in_band": passed,
                }
        judged = [x for x in entry["features"].values() if x.get("in_band") is not None]
        n = sum(int(f.get("n", 1)) for f in present.values())
        confidence = round(min(1.0, n / 20.0) * (0.5 if mock else 1.0), 3)
        if not judged:
            status = "not_measured"
        elif dimension in deviated:
            status = "deviation"
        else:
            status = "in_band" if ok else "out_of_band"
        metrics[dimension] = {**entry, "status": status, "confidence": confidence, "mock": mock}
    out = [d for d, mtr in metrics.items() if mtr["status"] == "out_of_band"]
    if any(metrics[d]["gate"] for d in out):
        verdict = "out_of_band"
    elif out:
        verdict = "warn"
    else:
        verdict = "in_band"
    return metrics, verdict
