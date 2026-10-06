"""Measurement half of observation (§16.3): analyzer tracks of one take → `ObservedBehavior`.

The document is keyed only by the take (its content hash) and is reused while the take is
unchanged; it holds no verdicts. Multi-chunk takes are measured per chunk and stitched at the
chunk offsets. The behavior signature (gesture-energy profile, head-motion pattern, expression
sequence, arc shape) feeds take ranking and the repetition guard (§15.8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from ce_contracts.models import BodyLandmarksResult, FaceLandmarksResult
from ce_core.behavior.observed import AnalyzerRef, BehaviorSignature, CharacterTracks, ObservedBehavior, TrackEvent

__all__ = ["ChunkMeasurement", "observed_behavior", "signature"]

SIGNATURE_BINS = 8


@dataclass(frozen=True)
class ChunkMeasurement:
    offset_s: float
    duration_s: float
    face: FaceLandmarksResult
    body: BodyLandmarksResult | None = None


def _bins(values: Sequence[float], n: int = SIGNATURE_BINS) -> list[float]:
    if not values:
        return []
    parts = np.array_split(np.asarray(values, dtype=float), min(n, len(values)))
    return [round(float(np.mean(p)), 4) for p in parts if p.size]


def signature(tracks: CharacterTracks) -> BehaviorSignature:
    series = tracks.series
    yaw = np.asarray(series.get("head_yaw_deg", []), dtype=float)
    pitch = np.asarray(series.get("head_pitch_deg", []), dtype=float)
    motion: list[float] = []
    if yaw.size and pitch.size and yaw.size == pitch.size:
        motion = list(np.abs(np.diff(yaw, prepend=yaw[:1])) + np.abs(np.diff(pitch, prepend=pitch[:1])))
    sequence: list[str] = []
    for event in sorted(tracks.events, key=lambda e: e.start_s):
        if event.type != "blink" and (not sequence or sequence[-1] != event.type):
            sequence.append(event.type)
    smile = series.get("smile", [])
    brow = series.get("brow_raise", [])
    arc = [round(s - b, 4) for s, b in zip(_bins(smile), _bins(brow), strict=False)] if brow else _bins(smile)
    return BehaviorSignature(
        gesture_energy_profile=_bins(series.get("gesture_energy", [])),
        head_motion_pattern=_bins(motion),
        expression_sequence=sequence,
        arc_shape=arc,
    )


def observed_behavior(
    *,
    take_sha256: str,
    character_key: str,
    chunks: Sequence[ChunkMeasurement],
    analyzers: Sequence[AnalyzerRef],
) -> ObservedBehavior:
    """Stitches chunk measurements into one track set (times in the take's time base)."""
    if not chunks:
        return ObservedBehavior(take_sha256=take_sha256, analyzers=list(analyzers))
    hz = chunks[0].face.sample_hz
    series: dict[str, list[float]] = {}
    events: list[TrackEvent] = []
    detected: list[tuple[float, float]] = []
    for chunk in sorted(chunks, key=lambda c: c.offset_s):
        n = max(1, round(chunk.duration_s * hz))
        parts = [chunk.face.series] + ([chunk.body.series] if chunk.body is not None else [])
        for part in parts:
            for name, values in part.items():
                padded = list(values[:n]) + [values[-1] if values else 0.0] * max(0, n - len(values))
                series.setdefault(name, []).extend(round(float(v), 4) for v in padded)
        outs = list(chunk.face.events) + (list(chunk.body.events) if chunk.body is not None else [])
        for e in outs:
            events.append(
                TrackEvent(
                    type=e.type,
                    start_s=round(e.start_s + chunk.offset_s, 3),
                    end_s=round(e.end_s + chunk.offset_s, 3),
                    value=e.value,
                    confidence=e.confidence,
                )
            )
        detected.append((chunk.face.face_detected_ratio, chunk.duration_s))
    total = sum(d for _, d in detected) or 1.0
    tracks = CharacterTracks(
        character_key=character_key,
        sample_hz=hz,
        series=series,
        events=sorted(events, key=lambda e: (e.start_s, e.type)),
        face_detected_ratio=round(sum(r * d for r, d in detected) / total, 4),
    )
    return ObservedBehavior(
        take_sha256=take_sha256, analyzers=list(analyzers), tracks=[tracks], signature=signature(tracks)
    )
