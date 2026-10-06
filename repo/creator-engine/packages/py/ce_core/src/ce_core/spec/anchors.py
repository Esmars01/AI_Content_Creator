"""Anchors: time is anchored to words and resolved to seconds after alignment (§11, ADR 0004).

Every timed field in a spec uses one of these types. A resolver maps them to seconds using
word timings: measured ones from the alignment artifact, or — in previz before alignment and
in the UI — estimated ones from a calibrated words-per-minute rate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from ce_core.keys import SceneKey, SegmentKey, ShotKey
from ce_core.spec.base import SpecModel
from ce_core.text import tokenize

__all__ = [
    "AnchorError",
    "AnchorResolver",
    "DurationSpan",
    "SceneSpan",
    "SegmentRef",
    "SegmentTimings",
    "ShotRef",
    "TimePoint",
    "TimeSpan",
    "WordRef",
    "WordSpan",
    "WordTiming",
    "estimate_segment_timings",
]


class WordRef(SpecModel):
    """One word of a segment (index into the canonical tokenizer's output)."""

    segment_key: SegmentKey
    word: NonNegativeInt


class WordSpan(SpecModel):
    """An inclusive word range. It may cross segments of the same scene, in order."""

    kind: Literal["words"] = "words"
    start: WordRef
    end: WordRef


class SegmentRef(SpecModel):
    segment_key: SegmentKey
    edge: Literal["start", "end"]


class ShotRef(SpecModel):
    kind: Literal["shot"] = "shot"
    shot_key: ShotKey
    offset_ms: NonNegativeInt = 0


class DurationSpan(SpecModel):
    """A span of fixed length after a shot point or a segment edge (title cards, silent holds)."""

    kind: Literal["duration"] = "duration"
    after: ShotRef | SegmentRef
    duration_ms: PositiveInt


class SceneSpan(SpecModel):
    kind: Literal["scene"] = "scene"
    scene_key: SceneKey


TimeSpan = Annotated[WordSpan | DurationSpan | SceneSpan, Field(discriminator="kind")]
"""Any span-valued field."""

TimePoint = WordRef | ShotRef | SegmentRef
"""Any point-valued field (`at`)."""


class AnchorError(ValueError):
    """An anchor does not resolve (unknown key, word out of range, order violated)."""


@dataclass(frozen=True, slots=True)
class WordTiming:
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class SegmentTimings:
    """Word timings of one segment, in the order of the canonical tokenizer."""

    words: tuple[WordTiming, ...]

    @property
    def start_s(self) -> float:
        return self.words[0].start_s

    @property
    def end_s(self) -> float:
        return self.words[-1].end_s


def estimate_segment_timings(
    segments: Sequence[tuple[str, str]],
    wpm: float,
    *,
    start_s: float = 0.0,
    gap_s: float = 0.25,
    pauses_s: Mapping[tuple[str, int], float] | None = None,
) -> dict[str, SegmentTimings]:
    """Estimated timings from a calibrated WPM (previz before alignment, and the UI).

    `segments` is an ordered list of `(segment_key, text)`. Each word gets an equal share of the
    minute at `wpm`; `pauses_s[(segment_key, word)]` adds silence after that word, and `gap_s`
    separates consecutive segments.
    """
    if wpm <= 0:
        raise ValueError("wpm must be positive")
    per_word = 60.0 / wpm
    pauses = pauses_s or {}
    out: dict[str, SegmentTimings] = {}
    t = start_s
    for index, (key, text) in enumerate(segments):
        if index:
            t += gap_s
        words: list[WordTiming] = []
        for token in tokenize(text):
            words.append(WordTiming(round(t, 6), round(t + per_word, 6)))
            t += per_word + pauses.get((key, token.index), 0.0)
        if not words:
            raise AnchorError(f"segment {key} has no words")
        out[key] = SegmentTimings(tuple(words))
    return out


class AnchorResolver:
    """Maps anchors to seconds for one scene or video.

    `segment_order` lists segment keys in timeline order; `shot_starts` gives each shot's
    start time (resolved earlier from the shot's own span); `scene_bounds` gives each scene's
    (start, end) in seconds.
    """

    def __init__(
        self,
        timings: Mapping[str, SegmentTimings],
        *,
        segment_order: Sequence[str],
        shot_starts: Mapping[str, float] | None = None,
        scene_bounds: Mapping[str, tuple[float, float]] | None = None,
    ) -> None:
        self._timings = timings
        self._order = {key: i for i, key in enumerate(segment_order)}
        self._shots = dict(shot_starts or {})
        self._scenes = dict(scene_bounds or {})

    def _segment(self, key: str) -> SegmentTimings:
        try:
            return self._timings[key]
        except KeyError:
            raise AnchorError(f"unknown segment {key}") from None

    def word(self, ref: WordRef) -> WordTiming:
        timings = self._segment(ref.segment_key)
        if ref.word >= len(timings.words):
            raise AnchorError(f"word {ref.word} out of range for {ref.segment_key} ({len(timings.words)} words)")
        return timings.words[ref.word]

    def point(self, ref: TimePoint) -> float:
        if isinstance(ref, WordRef):
            return self.word(ref).start_s
        if isinstance(ref, SegmentRef):
            timings = self._segment(ref.segment_key)
            return timings.start_s if ref.edge == "start" else timings.end_s
        if ref.shot_key not in self._shots:
            raise AnchorError(f"unknown shot {ref.shot_key}")
        return self._shots[ref.shot_key] + ref.offset_ms / 1000.0

    def span(self, span: WordSpan | DurationSpan | SceneSpan) -> tuple[float, float]:
        if isinstance(span, WordSpan):
            self.check_order(span)
            return self.word(span.start).start_s, self.word(span.end).end_s
        if isinstance(span, DurationSpan):
            start = self.point(span.after)
            return start, start + span.duration_ms / 1000.0
        if span.scene_key not in self._scenes:
            raise AnchorError(f"unknown scene {span.scene_key}")
        return self._scenes[span.scene_key]

    def check_order(self, span: WordSpan) -> None:
        """A word span must run forward: same segment with start ≤ end, or segments in timeline order."""
        for ref in (span.start, span.end):
            if ref.segment_key not in self._order:
                raise AnchorError(f"segment {ref.segment_key} is not in this timeline")
            self.word(ref)
        a, b = self._order[span.start.segment_key], self._order[span.end.segment_key]
        if a > b or (a == b and span.start.word > span.end.word):
            raise AnchorError(
                f"word span runs backwards: {span.start.segment_key}:{span.start.word} → "
                f"{span.end.segment_key}:{span.end.word}"
            )
