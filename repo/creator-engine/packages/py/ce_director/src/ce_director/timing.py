"""Duration estimates before alignment (§11 anchors, §13 stage 5, §15.4).

Estimates use the speaker's voice WPM (`VoiceDNA.wpm_for`: calibrated on the voice version; a
seeded creator ships default values) times the scene's pacing delta, plus explicit pauses, laid
out like the render timeline (`render.timeline`: a lead-in, a gap between segments and a tail).
Previz replaces them with measured times after alignment and reports the drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ce_config.schemas import TimelineConfig
from ce_core.enums import AnnotationType, ShotLayer
from ce_core.spec.anchors import DurationSpan
from ce_core.spec.videospec import Segment, VideoSpec
from ce_core.text import tokenize

__all__ = ["WordTime", "estimate_timeline", "nearest_word", "seconds_for_words", "target_word_count"]


@dataclass(frozen=True)
class WordTime:
    scene_key: str
    segment_key: str
    word: int
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Timeline:
    words: list[WordTime]
    total_s: float
    speech_s: float

    def word(self, segment_key: str, word: int) -> WordTime | None:
        return next((w for w in self.words if w.segment_key == segment_key and w.word == word), None)


def seconds_for_words(n_words: int, wpm: float) -> float:
    return n_words * 60.0 / max(wpm, 1.0)


def target_word_count(duration_s: float, wpm: float, timeline: TimelineConfig, segments: int) -> int:
    """Words that fill `duration_s` at `wpm` once the timeline's lead, gaps and tail are taken out."""
    overhead = timeline.lead_s + timeline.tail_s + timeline.segment_gap_s * max(0, segments - 1)
    return max(1, round(max(0.0, duration_s - overhead) * wpm / 60.0))


def _pause_s(segment: Segment, pause_ms: Mapping[str, int]) -> dict[int, float]:
    """Seconds of explicit pause after each word (pause annotations sit after their word)."""
    out: dict[int, float] = {}
    for ann in segment.annotations:
        if ann.type == AnnotationType.PAUSE:
            ms = ann.pause_ms or pause_ms.get(ann.tag, 0)
            out[ann.span.end.word] = out.get(ann.span.end.word, 0.0) + ms / 1000.0
    return out


def estimate_timeline(
    spec: VideoSpec,
    wpm_by_speaker: Mapping[str, float],
    *,
    default_wpm: float,
    pause_ms: Mapping[str, int],
    timeline: TimelineConfig,
) -> Timeline:
    """Estimated start and end of every word, in timeline seconds."""
    t = timeline.lead_s
    speech = 0.0
    out: list[WordTime] = []
    first = True
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        delta = scene.pacing.target_wpm_delta if scene.pacing else 0.0
        for seg_key in scene.segment_keys:
            segment = spec.script.segment(seg_key)
            if not first:
                t += timeline.segment_gap_s
            first = False
            wpm = wpm_by_speaker.get(segment.speaker_key, default_wpm) * (1.0 + delta)
            per_word = 60.0 / max(wpm, 1.0)
            pauses = _pause_s(segment, pause_ms)
            for token in tokenize(segment.text):
                out.append(WordTime(scene.key, seg_key, token.index, t, t + per_word))
                t += per_word + pauses.get(token.index, 0.0)
                speech += per_word + pauses.get(token.index, 0.0)
        for shot in scene.shots:
            if shot.layer == ShotLayer.BASE and isinstance(shot.span, DurationSpan):
                t += shot.span.duration_ms / 1000.0
    return Timeline(words=out, total_s=t + timeline.tail_s, speech_s=speech)


def nearest_word(timeline: Timeline, seconds: float, *, after: tuple[str, int] | None = None) -> WordTime | None:
    """The word whose estimated start is closest to `seconds` (strictly after `after` when given)."""
    candidates = timeline.words
    if after is not None:
        index = next((i for i, w in enumerate(candidates) if (w.segment_key, w.word) == after), None)
        candidates = candidates[index + 1 :] if index is not None else candidates
    if not candidates:
        return None
    return min(candidates, key=lambda w: (abs(w.start_s - seconds), w.start_s))
