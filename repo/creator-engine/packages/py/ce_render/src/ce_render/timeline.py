"""Timeline / EDL (§27): every time resolved from anchors against the aligned dialogue.

The dialogue track is the verified segments in timeline order: the first starts at `lead_s`,
consecutive segments are `gap_s` apart (pauses inside a segment are in its audio), and the video
ends `tail_s` after the last word. Word times come from `align.segment`, relative to each
segment's audio, and are shifted to the timeline here.

Tracks:
- **base**: base-layer shots cut back to back. A slot runs from its shot's span start to the next
  base shot's start; the first starts at 0 and the last ends at the end of the video.
- **overlay**: overlay shots at their exact spans (B-roll, screen, inserts, reaction clips).
- **titles**: title-card shots, drawn by `render.final`.
- **music** cues and **SFX** points, **caption words** with emphasis flags.

A talking shot's audio (`shot_audio`) is the covered segments' audio over its word span with
`pad_in`/`pad_out` seconds of silence around it; the avatar clip is placed at
`span_start - pad_in` so its lips match the dialogue track exactly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ce_core.enums import AnnotationType, ShotLayer, ShotType
from ce_core.spec.anchors import AnchorResolver, SegmentTimings, WordSpan, WordTiming
from ce_core.spec.videospec import Shot, VideoSpec
from ce_core.text import tokenize

__all__ = [
    "AudioPiece",
    "SegmentAudio",
    "ShotAudio",
    "Slot",
    "Timeline",
    "build_timeline",
    "chunk_windows",
    "local_shot_audio",
    "scene_clock",
]

LEAD_S = 0.2
GAP_S = 0.25
TAIL_S = 0.6
PAD_IN_S = 0.2
PAD_OUT_S = 0.3


@dataclass(frozen=True)
class SegmentAudio:
    """One verified segment: its audio duration and aligned word times (relative to its audio)."""

    duration_s: float
    words: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Slot:
    shot_key: str
    scene_key: str
    shot_type: str
    layer: str
    start_s: float  # on the timeline
    end_s: float
    span_start_s: float  # the shot's own span (word span or duration)
    span_end_s: float

    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 6)


@dataclass(frozen=True)
class AudioPiece:
    segment_key: str
    from_s: float  # within the segment's audio
    to_s: float
    at_s: float  # within the shot audio


@dataclass(frozen=True)
class ShotAudio:
    shot_key: str
    clip_start_s: float  # timeline time of the shot audio's t=0
    duration_s: float
    pieces: tuple[AudioPiece, ...]
    words: tuple[tuple[float, float], ...]  # the shot's words, relative to the shot audio


@dataclass
class Timeline:
    total_s: float
    segment_offsets: dict[str, float]
    segment_durations: dict[str, float]
    word_times: dict[str, list[tuple[float, float]]]
    scenes: dict[str, tuple[float, float]]
    base: list[Slot]
    overlays: list[Slot]
    titles: list[tuple[Slot, str]]
    music: dict[str, tuple[float, float]]
    sfx: dict[str, float]
    caption_words: list[tuple[str, float, float, bool]]
    resolver: AnchorResolver
    shots: dict[str, Slot] = field(default_factory=dict)

    def speech_intervals(self, pad_s: float = 0.1) -> list[tuple[float, float]]:
        """Merged intervals where someone speaks (word times ± `pad_s`), for music ducking."""
        spans = sorted((max(0.0, a - pad_s), b + pad_s) for words in self.word_times.values() for a, b in words)
        merged: list[tuple[float, float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged


def _emphasis(spec: VideoSpec) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for segment in spec.script.segments:
        for annotation in segment.annotations:
            span = annotation.span
            if (
                annotation.type == AnnotationType.EMPHASIS
                and isinstance(span, WordSpan)
                and span.start.segment_key == span.end.segment_key == segment.key
            ):
                out.update((segment.key, w) for w in range(span.start.word, span.end.word + 1))
    return out


def build_timeline(
    spec: VideoSpec,
    segments: Mapping[str, SegmentAudio],
    *,
    lead_s: float = LEAD_S,
    gap_s: float = GAP_S,
    tail_s: float = TAIL_S,
) -> Timeline:
    scenes = sorted(spec.scenes, key=lambda s: s.order)
    order = [k for scene in scenes for k in scene.segment_keys]
    offsets: dict[str, float] = {}
    word_times: dict[str, list[tuple[float, float]]] = {}
    t = lead_s
    for index, key in enumerate(order):
        if index:
            t += gap_s
        audio = segments[key]
        expected = len(tokenize(spec.script.segment(key).text))
        if len(audio.words) != expected:
            raise ValueError(f"alignment of {key} has {len(audio.words)} words, the script has {expected}")
        offsets[key] = round(t, 6)
        word_times[key] = [(round(t + a, 6), round(t + b, 6)) for a, b in audio.words]
        t += audio.duration_s
    total = round(t + tail_s, 6)

    timings = {k: SegmentTimings(tuple(WordTiming(a, b) for a, b in v)) for k, v in word_times.items()}
    bounds: dict[str, tuple[float, float]] = {}
    for index, scene in enumerate(scenes):
        first = word_times[scene.segment_keys[0]][0][0] if scene.segment_keys else 0.0
        start = 0.0 if index == 0 else first
        bounds[scene.key] = (round(start, 6), 0.0)
    keys = [s.key for s in scenes]
    for index, key in enumerate(keys):
        end = bounds[keys[index + 1]][0] if index + 1 < len(keys) else total
        bounds[key] = (bounds[key][0], end)

    # Word-spanned shots first, then duration spans that may refer to them.
    shot_starts: dict[str, float] = {}
    resolver = AnchorResolver(timings, segment_order=order, scene_bounds=bounds)
    pending: list[tuple[str, Shot]] = []
    spans: dict[str, tuple[float, float]] = {}
    for scene in scenes:
        for shot in scene.shots:
            if isinstance(shot.span, WordSpan):
                spans[shot.key] = resolver.span(shot.span)
                shot_starts[shot.key] = spans[shot.key][0]
            else:
                pending.append((scene.key, shot))
    for _ in range(len(pending) + 1):
        unresolved = []
        for scene_key, shot in pending:
            resolver = AnchorResolver(timings, segment_order=order, shot_starts=shot_starts, scene_bounds=bounds)
            try:
                spans[shot.key] = resolver.span(shot.span)
                shot_starts[shot.key] = spans[shot.key][0]
            except ValueError:
                unresolved.append((scene_key, shot))
        pending = unresolved
        if not pending:
            break
    if pending:
        raise ValueError(f"shots {[s.key for _, s in pending]} do not resolve")
    resolver = AnchorResolver(timings, segment_order=order, shot_starts=shot_starts, scene_bounds=bounds)

    scene_of = {shot.key: scene.key for scene in scenes for shot in scene.shots}
    shot_of = {shot.key: shot for scene in scenes for shot in scene.shots}
    base_keys = sorted(
        (k for k, s in shot_of.items() if s.layer == ShotLayer.BASE and s.type != ShotType.TITLE_CARD),
        key=lambda k: (spans[k][0], k),
    )
    base: list[Slot] = []
    for index, key in enumerate(base_keys):
        shot = shot_of[key]
        start = 0.0 if index == 0 else spans[key][0]
        end = spans[base_keys[index + 1]][0] if index + 1 < len(base_keys) else total
        base.append(
            Slot(key, scene_of[key], str(shot.type), str(shot.layer), round(start, 6), round(end, 6), *spans[key])
        )
    overlays: list[Slot] = []
    titles: list[tuple[Slot, str]] = []
    for key, shot in sorted(shot_of.items(), key=lambda kv: (spans[kv[0]][0], kv[0])):
        a, b = spans[key]
        slot = Slot(key, scene_of[key], str(shot.type), str(shot.layer), round(a, 6), round(min(b, total), 6), a, b)
        if shot.type == ShotType.TITLE_CARD:
            titles.append((slot, shot.title.text if shot.title else ""))
        elif shot.layer != ShotLayer.BASE:
            overlays.append(slot)
    slots = {s.shot_key: s for s in [*base, *overlays, *(t for t, _ in titles)]}

    music = {cue.key: tuple(round(x, 6) for x in resolver.span(cue.span)) for cue in spec.audio.music.cues}
    sfx = {event.key: round(resolver.point(event.at), 6) for event in spec.audio.sfx}
    emphasis = _emphasis(spec)
    caption_words = [
        (token.text, *word_times[key][token.index], (key, token.index) in emphasis)
        for key in order
        for token in tokenize(spec.script.segment(key).text)
    ]
    return Timeline(
        total_s=total,
        segment_offsets=offsets,
        segment_durations={k: segments[k].duration_s for k in order},
        word_times=word_times,
        scenes=bounds,
        base=base,
        overlays=overlays,
        titles=titles,
        music={k: (v[0], v[1]) for k, v in music.items()},
        sfx=sfx,
        caption_words=caption_words,
        resolver=resolver,
        shots=slots,
    )


def shot_audio(
    timeline: Timeline, spec: VideoSpec, shot_key: str, *, pad_in_s: float = PAD_IN_S, pad_out_s: float = PAD_OUT_S
) -> ShotAudio:
    """The audio an avatar shot is rendered against (see the module docstring)."""
    shot = next(s for _, s in spec.shots() if s.key == shot_key)
    if not isinstance(shot.span, WordSpan):
        raise ValueError(f"shot {shot_key} has no word span")
    start_s, end_s = timeline.resolver.span(shot.span)
    clip_start = max(0.0, start_s - pad_in_s)
    clip_end = min(timeline.total_s, end_s + pad_out_s)
    order = list(timeline.segment_offsets)
    a, b = order.index(shot.span.start.segment_key), order.index(shot.span.end.segment_key)
    pieces: list[AudioPiece] = []
    words: list[tuple[float, float]] = []
    for position in range(a, b + 1):
        key = order[position]
        offset = timeline.segment_offsets[key]
        times = timeline.word_times[key]
        first = shot.span.start.word if position == a else 0
        last = shot.span.end.word if position == b else len(times) - 1
        seg_from = 0.0 if first == 0 else times[first][0] - offset
        seg_to = timeline.segment_durations[key] if last == len(times) - 1 else times[last][1] - offset
        seg_from = max(seg_from, clip_start - offset)  # keep the piece inside the clip window
        seg_to = min(seg_to, clip_end - offset)
        pieces.append(AudioPiece(key, round(seg_from, 6), round(seg_to, 6), round(offset + seg_from - clip_start, 6)))
        words += [(round(x - clip_start, 6), round(y - clip_start, 6)) for x, y in times[first : last + 1]]
    return ShotAudio(shot_key, round(clip_start, 6), round(clip_end - clip_start, 6), tuple(pieces), tuple(words))


def chunk_windows(audio: ShotAudio, chunk_word_counts: Sequence[int]) -> list[tuple[float, float]]:
    """Time windows of the shot audio per chunk: boundaries halfway between the last word of a chunk
    and the first word of the next; the first starts at 0 and the last ends at the audio's end."""
    if sum(chunk_word_counts) != len(audio.words):
        raise ValueError("chunk word counts do not add up to the shot's words")
    windows: list[tuple[float, float]] = []
    start = 0.0
    index = 0
    for n, count in enumerate(chunk_word_counts):
        index += count
        if n == len(chunk_word_counts) - 1:
            end = audio.duration_s
        else:
            end = round((audio.words[index - 1][1] + audio.words[index][0]) / 2.0, 6)
        windows.append((round(start, 6), end))
        start = end
    return windows


def local_shot_audio(
    spec: VideoSpec,
    shot_key: str,
    segments: Mapping[str, SegmentAudio],
    *,
    gap_s: float = GAP_S,
    pad_in_s: float = PAD_IN_S,
    pad_out_s: float = PAD_OUT_S,
) -> ShotAudio:
    """`shot_audio` from the covered segments alone (what a shot's nodes depend on). Equal to the
    timeline-based result whenever `lead_s ≥ pad_in_s` and `tail_s ≥ pad_out_s` (the defaults)."""
    shot = next(s for _, s in spec.shots() if s.key == shot_key)
    if not isinstance(shot.span, WordSpan):
        raise ValueError(f"shot {shot_key} has no word span")
    scene = next(sc for sc, s in spec.shots() if s.key == shot_key)
    order = list(scene.segment_keys)
    a, b = order.index(shot.span.start.segment_key), order.index(shot.span.end.segment_key)
    covered = order[a : b + 1]
    offsets: dict[str, float] = {}
    t = 0.0
    for index, key in enumerate(covered):
        if index:
            t += gap_s
        offsets[key] = t
        t += segments[key].duration_s
    times = {k: [(offsets[k] + x, offsets[k] + y) for x, y in segments[k].words] for k in covered}
    start_s = times[covered[0]][shot.span.start.word][0]
    end_s = times[covered[-1]][shot.span.end.word][1]
    clip_start = start_s - pad_in_s
    clip_end = end_s + pad_out_s
    pieces: list[AudioPiece] = []
    words: list[tuple[float, float]] = []
    for position, key in enumerate(covered):
        seg_times = times[key]
        first = shot.span.start.word if position == 0 else 0
        last = shot.span.end.word if position == len(covered) - 1 else len(seg_times) - 1
        seg_from = 0.0 if first == 0 else seg_times[first][0] - offsets[key]
        seg_to = segments[key].duration_s if last == len(seg_times) - 1 else seg_times[last][1] - offsets[key]
        seg_from = max(seg_from, clip_start - offsets[key])
        seg_to = min(seg_to, clip_end - offsets[key])
        pieces.append(
            AudioPiece(key, round(seg_from, 6), round(seg_to, 6), round(offsets[key] + seg_from - clip_start, 6))
        )
        words += [(round(x - clip_start, 6), round(y - clip_start, 6)) for x, y in seg_times[first : last + 1]]
    return ShotAudio(shot_key, 0.0, round(clip_end - clip_start, 6), tuple(pieces), tuple(words))


def scene_clock(
    spec: VideoSpec, scene_key: str, segments: Mapping[str, SegmentAudio], *, gap_s: float = GAP_S
) -> tuple[AnchorResolver, dict[str, tuple[float, float]]]:
    """Anchors of one scene on a local clock (its first segment's audio at 0; only the scene's
    segments needed, as for `local_shot_audio`): the resolver and each shot's `(start, end)`.
    Differences equal the timeline's, since segments follow each other at `gap_s` everywhere."""
    scene = spec.scene(scene_key)
    order = list(scene.segment_keys)
    timings: dict[str, SegmentTimings] = {}
    t = 0.0
    for index, key in enumerate(order):
        if index:
            t += gap_s
        audio = segments[key]
        timings[key] = SegmentTimings(tuple(WordTiming(round(t + a, 6), round(t + b, 6)) for a, b in audio.words))
        t += audio.duration_s
    bounds = {scene.key: (0.0, round(t, 6))}
    starts: dict[str, float] = {}
    spans: dict[str, tuple[float, float]] = {}
    pending = list(scene.shots)
    for _ in range(len(pending) + 1):
        unresolved = []
        resolver = AnchorResolver(timings, segment_order=order, shot_starts=starts, scene_bounds=bounds)
        for shot in pending:
            try:
                spans[shot.key] = resolver.span(shot.span)
                starts[shot.key] = spans[shot.key][0]
            except ValueError:
                unresolved.append(shot)
        pending = unresolved
        if not pending:
            break
    if pending:
        raise ValueError(f"shots {[s.key for s in pending]} do not resolve in scene {scene_key}")
    return AnchorResolver(timings, segment_order=order, shot_starts=starts, scene_bounds=bounds), spans
