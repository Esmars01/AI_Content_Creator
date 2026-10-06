"""Exact-script extraction (§11, §13 stages 1 and 5).

In exact-script mode the Director never re-types the user's text. The model returns character
offsets into `brief.raw_input` — the script span (stage 1) and segment boundaries (stage 5) — and
this module slices the raw input, removes canonical acting tags (`ce_voice.tags`) and verifies:

1. every boundary lies inside the script span and on a word boundary;
2. the segments plus the whitespace between them cover the script span exactly (nothing dropped,
   nothing invented);
3. each segment's cleaned text plus its removed tags reassembles to its raw slice byte for byte.

Any failure raises `ExactScriptError` with the problems (the Director's repair loop shows them to
the model). `sentence_spans()` is the deterministic fallback: sentence-sized segments.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import regex

from ce_voice.tags import TaggedText, parse_tags, reassemble

__all__ = ["ExactScriptError", "ExactSegment", "extract_segments", "sentence_spans"]

_WORDCHAR = regex.compile(r"[\p{L}\p{M}\p{N}]")
_SENTENCE = regex.compile(r"\S.*?(?:[.!?…]+[\"'”’»)\]]*(?=\s|$)|$)", flags=regex.DOTALL)


class ExactScriptError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class ExactSegment:
    raw_start: int
    raw_end: int
    raw: str
    tagged: TaggedText

    @property
    def text(self) -> str:
        return self.tagged.text


def _inside_word(text: str, offset: int) -> bool:
    if offset <= 0 or offset >= len(text):
        return False
    return bool(_WORDCHAR.match(text[offset - 1]) and _WORDCHAR.match(text[offset]))


def sentence_spans(raw: str, start: int, end: int) -> list[tuple[int, int]]:
    """Sentence-sized (start, end) offsets covering the non-whitespace text of raw[start:end]."""
    out: list[tuple[int, int]] = []
    for match in _SENTENCE.finditer(raw, start, end):
        s, e = match.start(), match.end()
        while e > s and raw[e - 1].isspace():
            e -= 1
        if e > s:
            out.append((s, e))
    return out


def extract_segments(
    raw: str, script_span: tuple[int, int], boundaries: Sequence[tuple[int, int]]
) -> list[ExactSegment]:
    span_start, span_end = script_span
    problems: list[str] = []
    if not (0 <= span_start < span_end <= len(raw)):
        raise ExactScriptError([f"script span {script_span} is outside the input (length {len(raw)})"])
    if not boundaries:
        raise ExactScriptError(["no segment boundaries"])
    ordered = list(boundaries)
    for i, (s, e) in enumerate(ordered):
        if not (span_start <= s < e <= span_end):
            problems.append(f"segment {i} ({s}, {e}) is outside the script span {script_span}")
            continue
        if _inside_word(raw, s) or _inside_word(raw, e):
            problems.append(f"segment {i} ({s}, {e}) cuts a word")
    if problems:
        raise ExactScriptError(problems)
    cursor = span_start
    for i, (s, e) in enumerate(ordered):
        gap = raw[cursor:s]
        if s < cursor:
            problems.append(f"segment {i} overlaps the previous one")
        elif gap.strip():
            problems.append(f"text {gap.strip()[:40]!r} before segment {i} belongs to no segment")
        cursor = max(cursor, e)
    tail = raw[cursor:span_end]
    if tail.strip():
        problems.append(f"text {tail.strip()[:40]!r} after the last segment belongs to no segment")
    if problems:
        raise ExactScriptError(problems)
    segments: list[ExactSegment] = []
    for i, (s, e) in enumerate(ordered):
        piece = raw[s:e]
        tagged = parse_tags(piece)
        if reassemble(tagged.text, tagged.removed) != piece:  # pragma: no cover - invariant of parse_tags
            problems.append(f"segment {i} does not reassemble to its raw text")
        if not tagged.text.strip():
            problems.append(f"segment {i} has no words once tags are removed")
        segments.append(ExactSegment(s, e, piece, tagged))
    if problems:
        raise ExactScriptError(problems)
    return segments
