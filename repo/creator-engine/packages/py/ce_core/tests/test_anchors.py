from __future__ import annotations

import pytest
from ce_core.spec.anchors import (
    AnchorError,
    AnchorResolver,
    DurationSpan,
    SceneSpan,
    SegmentRef,
    ShotRef,
    TimeSpan,
    WordRef,
    WordSpan,
    estimate_segment_timings,
)
from pydantic import TypeAdapter, ValidationError

SEGMENTS = [
    ("seg_1", "Everyone thinks AI agents are just smarter chatbots."),
    ("seg_2", "But here's the thing... they're not."),
]


@pytest.fixture
def resolver() -> AnchorResolver:
    timings = estimate_segment_timings(SEGMENTS, wpm=150, gap_s=0.3, pauses_s={("seg_2", 3): 0.35})
    return AnchorResolver(
        timings,
        segment_order=["seg_1", "seg_2"],
        shot_starts={"sht_1": 0.0, "sht_2": 4.0},
        scene_bounds={"scn_hook": (0.0, 6.0)},
    )


def test_estimated_timings_follow_wpm_gaps_and_pauses(resolver: AnchorResolver) -> None:
    # 150 wpm → 0.4 s per word; seg_1 has 8 words.
    assert resolver.point(SegmentRef(segment_key="seg_1", edge="end")) == pytest.approx(3.2)
    assert resolver.point(SegmentRef(segment_key="seg_2", edge="start")) == pytest.approx(3.5)
    # the pause after seg_2 word 3 delays word 4
    assert resolver.point(WordRef(segment_key="seg_2", word=4)) == pytest.approx(3.5 + 4 * 0.4 + 0.35)


def test_spans(resolver: AnchorResolver) -> None:
    span = WordSpan(start=WordRef(segment_key="seg_1", word=6), end=WordRef(segment_key="seg_2", word=0))
    assert resolver.span(span) == pytest.approx((2.4, 3.9))
    assert resolver.span(
        DurationSpan(after=ShotRef(shot_key="sht_2", offset_ms=500), duration_ms=1500)
    ) == pytest.approx((4.5, 6.0))
    assert resolver.span(SceneSpan(scene_key="scn_hook")) == (0.0, 6.0)


def test_backwards_and_out_of_range_spans_fail(resolver: AnchorResolver) -> None:
    with pytest.raises(AnchorError, match="backwards"):
        resolver.span(WordSpan(start=WordRef(segment_key="seg_2", word=1), end=WordRef(segment_key="seg_1", word=3)))
    with pytest.raises(AnchorError, match="out of range"):
        resolver.word(WordRef(segment_key="seg_1", word=8))
    with pytest.raises(AnchorError, match="unknown shot"):
        resolver.point(ShotRef(shot_key="sht_9"))


def test_time_span_union_discriminates_on_kind() -> None:
    adapter: TypeAdapter[WordSpan | DurationSpan | SceneSpan] = TypeAdapter(TimeSpan)
    assert isinstance(adapter.validate_python({"kind": "scene", "scene_key": "scn_hook"}), SceneSpan)
    parsed = adapter.validate_python(
        {"kind": "duration", "after": {"segment_key": "seg_1", "edge": "end"}, "duration_ms": 800}
    )
    assert isinstance(parsed, DurationSpan) and isinstance(parsed.after, SegmentRef)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {"kind": "words", "start": {"segment_key": "seg_1", "word": -1}, "end": {"segment_key": "seg_1", "word": 0}}
        )
    with pytest.raises(ValidationError):
        WordRef.model_validate({"segment_key": "seg_1", "word": 0, "extra": 1})


def test_estimate_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        estimate_segment_timings(SEGMENTS, wpm=0)
    with pytest.raises(AnchorError, match="no words"):
        estimate_segment_timings([("seg_1", "...")], wpm=150)
