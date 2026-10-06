"""Screen-shot planning (§27): script spans → screen ranges (anchors on newly visible text),
speed segments for dead time, zoom windows on what changed, the webcam-bubble corner."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from ce_config.schemas import ScreenConfig
from ce_core.spec.anchors import DurationSpan, ShotRef
from ce_core.spec.videospec import Shot
from ce_director.screen import ShotWords, _out_time, plan_screen, retime

CFG = ScreenConfig()


def _shot(profile: str = "screen_only") -> Shot:
    return Shot.model_validate(
        {
            "key": "sht_9",
            "type": "screen",
            "layer": "overlay",
            "span": {"kind": "duration", "after": {"segment_key": "seg_1", "edge": "start"}, "duration_ms": 14000},
            "camera": {"profile_id": profile, "framing": "insert", "angle": "eye_level"},
            "screen": {"asset_id": str(uuid.uuid4())},
        }
    )


def _analysis() -> dict[str, Any]:
    """18 s: a settings page, a cut at 6 s to billing, an invoice line at ~9 s (bottom right),
    then nothing until the end; the menu text sits top left."""
    menu = [{"text": "Settings", "bbox": [0.01, 0.01, 0.08, 0.05], "confidence": 0.9}]
    billing = [{"text": "Billing overview", "bbox": [0.06, 0.22, 0.2, 0.05], "confidence": 0.9}]
    invoice = {"text": "Invoice 2026-10 paid", "bbox": [0.59, 0.72, 0.29, 0.06], "confidence": 0.9}
    return {
        "duration_s": 18.0,
        "scenes": [{"index": 0, "start_s": 0.0, "end_s": 6.0}, {"index": 1, "start_s": 6.0, "end_s": 18.0}],
        "keyframes": [
            {"t_s": 0.4, "scene": 0, "ocr": menu, "added": ["Settings"], "removed": [], "changed_regions": []},
            {"t_s": 5.8, "scene": 0, "ocr": menu, "added": [], "removed": [], "changed_regions": []},
            {
                "t_s": 6.4,
                "scene": 1,
                "ocr": billing,
                "added": ["Billing overview"],
                "removed": ["settings"],
                "changed_regions": [],
                "changed_fraction": 0.9,
            },
            {
                "t_s": 9.4,
                "scene": 1,
                "ocr": [*billing, invoice],
                "added": ["Invoice 2026-10 paid"],
                "removed": [],
                "changed_regions": [[0.59, 0.71, 0.29, 0.07]],
            },
            {"t_s": 12.4, "scene": 1, "ocr": [*billing, invoice], "added": [], "removed": [], "changed_regions": []},
            {"t_s": 17.8, "scene": 1, "ocr": [*billing, invoice], "added": [], "removed": [], "changed_regions": []},
        ],
        "dead_time": [{"start_s": 0.9, "end_s": 5.3}, {"start_s": 9.9, "end_s": 17.3}],
    }


def _words(texts: list[str], step: float = 0.5, duration: float = 14.0) -> ShotWords:
    return ShotWords(
        [("seg_1", i, t) for i, t in enumerate(texts)],
        [(round(i * step, 3), round(i * step + 0.4, 3)) for i in range(len(texts))],
        duration,
    )


def test_retime_and_output_clock() -> None:
    # anchor recording 9 s at output 3 s: 3× until then; dead time after it at 4×
    segments = retime([(9.0, 3.0)], [(9.9, 17.3)], output_s=14.0, dead_speed=4.0)
    assert segments == [(0.0, 3.0, 3.0), (3.9, 5.75, 4.0)]
    assert _out_time(segments, 9.0) == pytest.approx(3.0)
    assert _out_time(segments, 9.5) == pytest.approx(3.5)
    assert _out_time(segments, 17.3) == pytest.approx(5.75)
    assert _out_time(segments, 18.0) == pytest.approx(6.45)
    # near 1× is 1×: no segment, nothing shifts
    assert retime([(5.1, 5.0)], [], output_s=10.0, dead_speed=4.0) == []
    # clamped to 8×
    assert retime([(17.0, 1.0)], [], output_s=10.0, dead_speed=4.0) == [(0.0, 2.125, 8.0)]


def test_plan_anchors_spoken_words_on_new_text() -> None:
    words = _words(["now", "open", "billing", "and", "check", "the", "invoice", "line", "here"])
    plan = plan_screen(_shot(), _analysis(), words, CFG, existing_keys={"zm_1"})
    assert [a["text"] for a in plan.anchors] == ["billing", "invoice"]
    billing, invoice = plan.anchors
    assert billing["rec_s"] == pytest.approx(6.0) and billing["out_s"] == pytest.approx(1.0)
    assert invoice["rec_s"] == pytest.approx(9.0) and invoice["out_s"] == pytest.approx(3.0)
    speeds = [(s.span.after.offset_ms, s.span.duration_ms, s.speed) for s in plan.screen.speed_segments]  # type: ignore[union-attr]
    assert speeds[0] == (0, 1000, 6.0)  # 0–6 s of recording in the 1 s before "billing"
    assert speeds[1] == (1000, 2000, 1.5)  # 6–9 s in the 2 s up to "invoice"
    assert speeds[2][2] == CFG.dead_time_speed  # the quiet stretch after the invoice line
    for segment in plan.screen.speed_segments:
        assert isinstance(segment.span, DurationSpan) and isinstance(segment.span.after, ShotRef)
        assert segment.span.after.shot_key == "sht_9"
    # a zoom on the invoice line, from its output time
    assert len(plan.screen.zooms) == 1
    zoom = plan.screen.zooms[0]
    assert zoom.key == "zm_2" and zoom.span.after.offset_ms == 3000  # type: ignore[union-attr]
    x, y, w, h = zoom.rect
    assert x <= 0.59 and x + w >= 0.88 and y <= 0.71 and y + h >= 0.78 and w < CFG.zoom_max_width
    assert plan.screen.webcam_bubble.enabled is False


def test_plan_without_anchors_compresses_dead_time_and_places_the_bubble() -> None:
    words = _words(["a", "short", "narration", "with", "nothing", "on", "screen", "named"], duration=9.0)
    plan = plan_screen(_shot("screen_webcam_bubble"), _analysis(), words, CFG)
    assert plan.anchors == []
    segments = [(s.span.after.offset_ms / 1000, s.speed) for s in plan.screen.speed_segments]  # type: ignore[union-attr]
    assert segments[0] == (0.9, 4.0) and len(segments) == 2
    bubble = plan.screen.webcam_bubble
    assert bubble.enabled and bubble.size == CFG.bubble_size
    assert bubble.corner == "bottom_left"  # text sits top left and bottom right
    assert any("webcam bubble" in n for n in plan.notes)


def test_nothing_to_zoom_on() -> None:
    analysis = {
        "duration_s": 5.0,
        "scenes": [{"index": 0, "start_s": 0.0, "end_s": 5.0}],
        "keyframes": [],
        "dead_time": [],
    }
    plan = plan_screen(_shot(), analysis, _words(["hello"], duration=6.0), CFG)
    assert plan.screen.zooms == [] and plan.screen.speed_segments == []
    assert any("no zoom" in n for n in plan.notes) and any("last frame holds" in n for n in plan.notes)
