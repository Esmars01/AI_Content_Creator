"""The emotional trajectory (§15.4).

The acting-model validators live with the VideoSpec validator (`ce_core.spec.validate`): states
tile the speech span; intensity jumps need a sudden transition or a trigger; a turn into an
emotion the previous state lists as incompatible needs a trigger; triggers sit inside the state
they start; masking needs felt ≠ displayed; labels exist; DNA ranges; zone postures; strategies
that express none of the displayed emotion's compatible strategies warn. This module holds the
trajectory helpers the Director and the Performance Timeline use:

- `trajectory`: the video's ordered states across scenes, with absolute confidence;
- `bands_from_seconds`: maps a seconds-based request ("0–3 s confident; 3–6 s realization…") to
  word-anchored states, choosing each start word closest to the requested second, and reports
  the drift — time is word-anchored, so seconds are honored approximately and never silently
  (ADR 0004).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ce_core.spec.videospec import VideoSpec

from ce_behavior.scene import SceneWords, resolved_states

__all__ = [
    "Band",
    "BandRequest",
    "TrajectoryPoint",
    "bands_from_seconds",
    "trajectory",
]


@dataclass(frozen=True)
class TrajectoryPoint:
    scene_key: str
    state_key: str
    character_key: str
    label: str
    intensity: float
    felt: str
    masking: bool
    confidence: float
    first_word: int | None
    last_word: int | None


def trajectory(spec: VideoSpec, baseline_confidence: Mapping[str, float] | None = None) -> list[TrajectoryPoint]:
    """Every character's states in scene order; confidence accumulates `confidence_delta` from the
    creator baseline (default 0.5), clamped to 0..1, across scenes."""
    confidence = dict(baseline_confidence or {})
    out: list[TrajectoryPoint] = []
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        if scene.acting is None:
            continue
        words = SceneWords.of(spec, scene)
        for resolved in resolved_states(scene, words):
            values = resolved.values
            if values.emotion is None:
                continue
            character = resolved.state.character_key
            level = confidence.get(character, 0.5) + resolved.state.confidence_delta
            confidence[character] = max(0.0, min(1.0, level))
            out.append(
                TrajectoryPoint(
                    scene_key=scene.key,
                    state_key=resolved.key,
                    character_key=character,
                    label=str(values.emotion.displayed.label),
                    intensity=float(values.emotion.displayed.intensity),
                    felt=str(values.emotion.felt.label),
                    masking=bool(values.emotion.masking),
                    confidence=round(confidence[character], 4),
                    first_word=resolved.first,
                    last_word=resolved.last,
                )
            )
    return out


@dataclass(frozen=True)
class BandRequest:
    start_s: float
    end_s: float
    label: str


@dataclass(frozen=True)
class Band:
    request: BandRequest
    first_word: int
    last_word: int
    start_s: float  # estimated start of the first word
    end_s: float  # estimated end of the last word
    drift_start_s: float
    drift_end_s: float


def bands_from_seconds(requests: Sequence[BandRequest], word_times: Sequence[tuple[float, float]]) -> list[Band]:
    """Word-anchored bands for a seconds-based trajectory request. `word_times` are the estimated
    (start, end) of the scene's words in speaking order. Bands tile the words: each band starts at
    the word whose start is closest to the requested second (strictly after the previous band's
    start, leaving every later band at least one word); the drift is reported per edge."""
    if not requests:
        return []
    ordered = sorted(requests, key=lambda r: r.start_s)
    n = len(word_times)
    if n < len(ordered):
        raise ValueError(f"{len(ordered)} bands cannot tile {n} words")
    starts: list[int] = []
    for i, request in enumerate(ordered):
        low = starts[-1] + 1 if starts else 0
        high = n - (len(ordered) - i)  # leave one word for each later band
        if i == 0:
            starts.append(0)  # the first band covers the speech from its first word
            continue
        best = min(range(low, high + 1), key=lambda w: (abs(word_times[w][0] - request.start_s), w))
        starts.append(best)
    bands: list[Band] = []
    for i, request in enumerate(ordered):
        first = starts[i]
        last = (starts[i + 1] - 1) if i + 1 < len(starts) else n - 1
        start_s, end_s = word_times[first][0], word_times[last][1]
        bands.append(
            Band(
                request=request,
                first_word=first,
                last_word=last,
                start_s=round(start_s, 3),
                end_s=round(end_s, 3),
                drift_start_s=round(start_s - request.start_s, 3),
                drift_end_s=round(end_s - request.end_s, 3),
            )
        )
    return bands
