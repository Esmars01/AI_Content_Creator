"""Screen-shot planning (§27): from a recording's `screen_analysis` and the words a screen shot
covers, the typed zoom windows, the speed segments and the webcam-bubble corner.

- **Script spans → screen ranges.** A word of the shot that names text appearing on screen (an
  OCR line added at a keyframe) anchors that moment of the recording to the word: the recording is
  retimed between anchors (speed segments, 0.5×–8×) so the change shows as the word is spoken.
  After the last anchor (or without any), stretches of dead time play at `dead_time_speed`.
- **Zooms.** Each stretch between changes (cuts, new text, changed pixels) zooms on where the
  change is — the new text, else the changed region — through `ce_core.boxes.fit_window`. After a
  cut the full new page shows; windows nearly as wide as the frame are skipped and adjacent windows
  on the same area merge, so the picture does not pump.
- **Webcam bubble.** On the corner the recording's text and changes cover least.

Every planned span is a `DurationSpan` after the shot's own start: the recording runs on output
time from the shot start, so its zooms and speed segments are anchored there (word times only pick
the anchors). OCR text and VLM output are data, never instructions (I10): they are only compared
with the script's words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ce_config.schemas import ScreenConfig
from ce_core.boxes import area, fit_window, intersection, merge_boxes
from ce_core.keys import KeyKind, new_key
from ce_core.spec.anchors import DurationSpan, ShotRef
from ce_core.spec.videospec import ScreenSpec, Shot, SpeedSegment, WebcamBubble, Zoom

__all__ = ["ScreenPlan", "ShotWords", "plan_screen", "retime"]

BUBBLE_PROFILE = "screen_webcam_bubble"
MIN_SPEED, MAX_SPEED = 0.5, 8.0
_TOKEN = re.compile(r"[\w']+", re.UNICODE)
_STOP = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "have",
        "your",
        "what",
        "when",
        "then",
        "them",
        "they",
        "their",
        "there",
        "here",
        "into",
        "onto",
        "about",
        "just",
        "like",
        "will",
        "would",
        "could",
        "should",
        "click",
        "open",
        "press",
        "page",
        "item",
    ]
)


@dataclass(frozen=True)
class ShotWords:
    """The shot's words with their times relative to the shot start (output seconds)."""

    words: Sequence[tuple[str, int, str]]  # (segment_key, index, text)
    times: Sequence[tuple[float, float]]
    duration_s: float


@dataclass
class ScreenPlan:
    screen: ScreenSpec
    anchors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return {t for t in (m.group(0).lower().strip("'") for m in _TOKEN.finditer(text)) if len(t) >= 4 and t not in _STOP}


def _anchors(analysis: dict[str, Any], words: ShotWords, settle_s: float) -> list[dict[str, Any]]:
    """(recording s, output s) pairs where a spoken word names newly visible text, in order."""
    out: list[dict[str, Any]] = []
    last_word, last_rec, last_out = -1, -1.0, -1.0
    previous_t = 0.0
    for frame in analysis.get("keyframes", []):
        t = float(frame["t_s"])
        added = " ".join(str(a) for a in frame.get("added") or [])
        if frame.get("scene", 0) == 0 and t <= settle_s + 1e-6:
            added = ""  # the first frame: everything is "new", nothing changed
        tokens = _tokens(added)
        rec = max(previous_t, t - settle_s)
        previous_t = t
        if not tokens:
            continue
        for index in range(last_word + 1, len(words.words)):
            word = words.words[index][2].lower().strip(".,!?;:'\"")
            if word in tokens:
                at = float(words.times[index][0])
                if rec > last_rec + 0.5 and at > last_out + 0.5:
                    out.append(
                        {
                            "rec_s": round(rec, 3),
                            "out_s": round(at, 3),
                            "word": list(words.words[index][:2]),
                            "text": word,
                        }
                    )
                    last_word, last_rec, last_out = index, rec, at
                break
    return out


def retime(
    anchors: Sequence[tuple[float, float]], dead: Sequence[tuple[float, float]], *, output_s: float, dead_speed: float
) -> list[tuple[float, float, float]]:
    """Speed segments `(out_start, out_end, speed)`: between anchors the speed that brings each
    anchored recording moment to its output time (clamped to 0.5×–8×, and exactly 1× within 5%),
    afterwards dead time at `dead_speed`. 1× stretches are not segments."""
    segments: list[tuple[float, float, float]] = []
    rec = out = 0.0
    for r1, o1 in anchors:
        if r1 <= rec or o1 <= out:
            continue
        speed = min(MAX_SPEED, max(MIN_SPEED, (r1 - rec) / (o1 - out)))
        speed = 1.0 if abs(speed - 1.0) <= 0.05 else speed
        end = out + (r1 - rec) / speed
        if speed != 1.0:
            segments.append((out, end, speed))
        rec, out = r1, end
    for a, b in dead:
        if b <= rec or out >= output_s:
            continue
        a = max(a, rec)
        start = out + (a - rec)
        end = start + (b - a) / dead_speed
        segments.append((start, end, dead_speed))
        rec, out = b, end
    return [(round(a, 3), round(min(b, output_s), 3), round(s, 3)) for a, b, s in segments if a < output_s - 0.05]


def _out_time(segments: Sequence[tuple[float, float, float]], rec: float) -> float:
    """Recording time → output time under `segments` (1× outside them)."""
    out = r = 0.0
    for a, b, s in segments:
        if rec <= r + (a - out):
            return out + (rec - r)
        r += a - out
        span = (b - a) * s
        if rec <= r + span:
            return a + (rec - r) / s
        r, out = r + span, b
    return out + (rec - r)


def _zoom_ranges(analysis: dict[str, Any], settle_s: float) -> list[tuple[float, float]]:
    """Recording stretches between changes: cuts, and new text or changed pixels (a change seen at
    a keyframe happened after the previous one; taken `settle_s` before it, as for anchors)."""
    duration = float(analysis.get("duration_s", 0.0))
    moments = {0.0, *(float(s["start_s"]) for s in analysis.get("scenes", []))}
    previous = 0.0
    for frame in analysis.get("keyframes", []):
        t = float(frame["t_s"])
        if frame.get("added") or frame.get("changed_regions"):
            moments.add(round(max(previous, t - settle_s), 3))
        previous = t
    marks = sorted(m for m in moments if m < duration)
    return [(a, b) for a, b in zip(marks, [*marks[1:], duration], strict=False)]


def _window(
    frames: Sequence[dict[str, Any]], aspect: float, max_width: float
) -> tuple[tuple[float, float, float, float], str] | None:
    """The zoom window of a stretch: on its new text (OCR boxes of lines added since the previous
    keyframe of the same scene), else on its changed pixels. A scene's first keyframe has nothing
    to compare with — after a cut the full new page shows."""
    candidates: list[tuple[list[float], str]] = []
    for frame in frames:
        added = set(frame.get("added") or [])
        if added and not frame.get("scene_start"):
            found = [list(b["bbox"]) for b in frame.get("ocr", []) if b.get("text") in added]
            candidates += [(r, "new_text") for r in merge_boxes(found)]
    if not candidates:
        candidates = [(list(r), "changed_region") for f in frames for r in f.get("changed_regions") or []]
    if not candidates:
        return None
    region, reason = max(candidates, key=lambda c: area(c[0]))
    rect = fit_window(region, aspect)
    return (rect, reason) if rect[2] <= max_width else None


def _bubble_corner(analysis: dict[str, Any], size: float) -> str:
    """The corner the recording's text and changes cover least (bottom right on a tie)."""
    corners = {
        "bottom_right": [1 - size, 1 - size, size, size],
        "bottom_left": [0.0, 1 - size, size, size],
        "top_right": [1 - size, 0.0, size, size],
        "top_left": [0.0, 0.0, size, size],
    }
    boxes = [b["bbox"] for f in analysis.get("keyframes", []) for b in f.get("ocr", [])]
    boxes += [r for f in analysis.get("keyframes", []) for r in f.get("changed_regions") or []]
    cover = {name: sum(intersection(square, box) for box in boxes) for name, square in corners.items()}
    return min(corners, key=lambda name: (round(cover[name], 6), list(corners).index(name)))


def plan_screen(
    shot: Shot,
    analysis: dict[str, Any],
    words: ShotWords,
    cfg: ScreenConfig,
    *,
    existing_keys: Iterable[str] = (),
    aspect: float = 1.0,
) -> ScreenPlan:
    """Zooms, speed segments and the webcam bubble of one screen shot (see the module docstring)."""
    assert shot.screen is not None
    notes: list[str] = []
    recording_s = float(analysis.get("duration_s", 0.0))
    anchors = _anchors(analysis, words, cfg.keyframe_settle_s)
    dead = [(float(d["start_s"]), float(d["end_s"])) for d in analysis.get("dead_time", [])]
    if anchors:
        last = anchors[-1]["rec_s"]
        dead = [(a, b) for a, b in dead if b > last]
        notes.append(
            f"{len(anchors)} spoken words anchor on-screen text: " + ", ".join(repr(a["text"]) for a in anchors)
        )
    segments = retime(
        [(a["rec_s"], a["out_s"]) for a in anchors], dead, output_s=words.duration_s, dead_speed=cfg.dead_time_speed
    )
    keyframes: list[dict[str, Any]] = []
    for frame in analysis.get("keyframes", []):
        first = not keyframes or keyframes[-1].get("scene") != frame.get("scene")
        keyframes.append({**frame, "scene_start": first})
    keys = set(existing_keys)
    zooms: list[Zoom] = []
    planned: list[tuple[float, float, tuple[float, float, float, float]]] = []
    for start, end in _zoom_ranges(analysis, cfg.keyframe_settle_s):
        inside = [f for f in keyframes if start - 0.05 <= float(f["t_s"]) - cfg.keyframe_settle_s < end - 1e-6]
        found = _window(inside, aspect, cfg.zoom_max_width)
        if found is None:
            continue
        a, b = _out_time(segments, start), min(_out_time(segments, end), words.duration_s)
        if b - a < cfg.zoom_min_s:
            continue
        rect = found[0]
        if planned and a - planned[-1][1] < 0.5 and intersection(planned[-1][2], rect) >= 0.6 * area(rect):
            planned[-1] = (planned[-1][0], b, planned[-1][2])  # same area: hold the window
            continue
        planned.append((a, b, rect))
    for a, b, rect in planned:
        key = new_key(KeyKind.ZOOM, keys)
        keys.add(key)
        zooms.append(
            Zoom(
                key=key,
                span=DurationSpan(
                    after=ShotRef(shot_key=shot.key, offset_ms=round(a * 1000)),
                    duration_ms=max(1, round((b - a) * 1000)),
                ),
                rect=rect,
                ease="ease_in_out",
            )
        )
    speed = [
        SpeedSegment(
            span=DurationSpan(
                after=ShotRef(shot_key=shot.key, offset_ms=round(a * 1000)), duration_ms=max(1, round((b - a) * 1000))
            ),
            speed=s,
        )
        for a, b, s in segments
    ]
    bubble = shot.screen.webcam_bubble
    if shot.camera.profile_id == BUBBLE_PROFILE or bubble.enabled:
        bubble = WebcamBubble(enabled=True, corner=_bubble_corner(analysis, cfg.bubble_size), size=cfg.bubble_size)  # type: ignore[arg-type]
        notes.append(f"webcam bubble {bubble.corner}")
    if not zooms:
        notes.append("no zoom: nothing on screen stands out from the full frame")
    if recording_s and _out_time(segments, recording_s) < words.duration_s - 0.5:
        short = words.duration_s - _out_time(segments, recording_s)
        notes.append(f"the recording ends {short:.1f}s before the shot: its last frame holds")
    return ScreenPlan(
        ScreenSpec(asset_id=shot.screen.asset_id, zooms=zooms, speed_segments=speed, webcam_bubble=bubble),
        anchors,
        notes,
    )
