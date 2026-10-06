"""The canonical acting-tag parser (§21, §10.6): `[excited] [whispers] [laughs] [short pause]
[long pause] [confused] [serious] [sad] [angry] [curious] [sigh] [breath] [emphasize]
[looks away] [smiles]`.

- Audio tags become annotations: pauses (`after` the previous word; always inserted silence),
  non-verbal sounds (after the previous word), delivery (`[whispers]`, to the end of the sentence)
  and emphasis (the next word).
- Emotion tags become acting-state overrides for their span: from the next word to the end of its
  sentence, or to the next emotion tag (`source: user_tag`).
- Visual tags become acting events at the next word.

Only canonical tags are converted (case and inner spacing do not matter); any other bracketed text
is the user's text and stays. A converted tag is removed together with one adjacent whitespace
run, so `a [laughs] b` becomes `a b`; every other character is kept byte for byte, with no Unicode
normalization. `reassemble()` puts the removed substrings back and must return the input exactly.
Word indices are positions in the canonical tokenizer's output of the cleaned text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import regex
from ce_core.text.tokenize import tokenize

__all__ = ["CANONICAL_TAGS", "RemovedTag", "TagSpec", "TaggedText", "parse_tags", "reassemble"]

Kind = Literal["emotion", "annotation", "event"]


@dataclass(frozen=True)
class TagSpec:
    kind: Kind
    target: str  # emotion label, annotation tag or event type
    annotation_type: str | None = None
    anchor: Literal["next", "previous", "sentence"] = "next"


CANONICAL_TAGS: dict[str, TagSpec] = {
    "excited": TagSpec("emotion", "excited", anchor="sentence"),
    "confused": TagSpec("emotion", "confused", anchor="sentence"),
    "serious": TagSpec("emotion", "serious", anchor="sentence"),
    "sad": TagSpec("emotion", "sad", anchor="sentence"),
    "angry": TagSpec("emotion", "angry", anchor="sentence"),
    "curious": TagSpec("emotion", "curious", anchor="sentence"),
    "whispers": TagSpec("annotation", "whisper", "delivery", anchor="sentence"),
    "laughs": TagSpec("annotation", "laugh", "nonverbal_audio", anchor="previous"),
    "sigh": TagSpec("annotation", "sigh", "nonverbal_audio", anchor="previous"),
    "breath": TagSpec("annotation", "breath", "nonverbal_audio", anchor="previous"),
    "short pause": TagSpec("annotation", "short_pause", "pause", anchor="previous"),
    "long pause": TagSpec("annotation", "long_pause", "pause", anchor="previous"),
    "emphasize": TagSpec("annotation", "emphasize", "emphasis", anchor="next"),
    "looks away": TagSpec("event", "look_away", anchor="next"),
    "smiles": TagSpec("event", "small_smile", anchor="next"),
}

_TAG = regex.compile(r"\[\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)\s*\]")
_SENTENCE_END = regex.compile(r"[.!?…][\"'”’»)\]]*$")


@dataclass(frozen=True)
class RemovedTag:
    """`text` was removed from the original at `start` (an offset into the original)."""

    start: int
    text: str
    tag: str
    clean_offset: int  # where the tag sat in the cleaned text


@dataclass(frozen=True)
class TagAnnotation:
    type: str
    tag: str
    start_word: int
    end_word: int


@dataclass(frozen=True)
class TagEmotion:
    label: str
    start_word: int
    end_word: int


@dataclass(frozen=True)
class TagEvent:
    type: str
    word: int


@dataclass
class TaggedText:
    original: str
    text: str
    removed: list[RemovedTag] = field(default_factory=list)
    annotations: list[TagAnnotation] = field(default_factory=list)
    emotions: list[TagEmotion] = field(default_factory=list)
    events: list[TagEvent] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _canonical(raw: str) -> str:
    return " ".join(raw.lower().split())


def _strip_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, tag) of the substrings to remove: each canonical tag with one adjacent
    whitespace run (the following one, or the preceding one at the end of the text)."""
    spans: list[tuple[int, int, str]] = []
    for match in _TAG.finditer(text):
        name = _canonical(match.group(1))
        if name not in CANONICAL_TAGS:
            continue
        start, end = match.start(), match.end()
        after = regex.match(r"\s+", text[end:])
        if after is not None:
            end += after.end()
        else:
            before = regex.search(r"\s+$", text[:start])
            if before is not None and start > 0:
                start = before.start()
        if spans and start < spans[-1][1]:  # adjacent tags share the whitespace already taken
            start = spans[-1][1]
        spans.append((start, end, name))
    return spans


def reassemble(clean: str, removed: list[RemovedTag]) -> str:
    """Inverse of the removal: the original text, exactly."""
    out: list[str] = []
    pos = 0
    for item in sorted(removed, key=lambda r: r.clean_offset):
        out.append(clean[pos : item.clean_offset])
        out.append(item.text)
        pos = item.clean_offset
    out.append(clean[pos:])
    return "".join(out)


def parse_tags(text: str) -> TaggedText:
    spans = _strip_spans(text)
    removed: list[RemovedTag] = []
    pieces: list[str] = []
    pos = 0
    clean_len = 0
    for start, end, name in spans:
        pieces.append(text[pos:start])
        clean_len += start - pos
        removed.append(RemovedTag(start=start, text=text[start:end], tag=name, clean_offset=clean_len))
        pos = end
    pieces.append(text[pos:])
    clean = "".join(pieces)
    result = TaggedText(original=text, text=clean, removed=removed)
    tokens = tokenize(clean)
    if not tokens:
        if removed:
            result.issues.append("tags without words: nothing to attach them to")
        return result
    sentence_ends = [i for i, t in enumerate(tokens) if _SENTENCE_END.search(t.text)]

    def next_word(offset: int) -> int | None:
        return next((t.index for t in tokens if t.start >= offset), None)

    def previous_word(offset: int) -> int | None:
        before = [t.index for t in tokens if t.end <= offset]
        return before[-1] if before else None

    def sentence_end(word: int) -> int:
        return next((i for i in sentence_ends if i >= word), len(tokens) - 1)

    emotion_starts = sorted(
        (w, r.tag)
        for r in removed
        if CANONICAL_TAGS[r.tag].kind == "emotion" and (w := next_word(r.clean_offset)) is not None
    )
    for item in removed:
        spec = CANONICAL_TAGS[item.tag]
        nxt, prev = next_word(item.clean_offset), previous_word(item.clean_offset)
        if spec.anchor == "previous":
            word = prev if prev is not None else None
            if word is None:
                result.issues.append(f"[{item.tag}] before the first word has nothing to follow; ignored")
                continue
            result.annotations.append(TagAnnotation(str(spec.annotation_type), spec.target, word, word))
            continue
        first = nxt if nxt is not None else prev
        if first is None:
            continue
        if spec.anchor == "next":
            if spec.kind == "event":
                result.events.append(TagEvent(spec.target, first))
            else:
                result.annotations.append(TagAnnotation(str(spec.annotation_type), spec.target, first, first))
            continue
        end = sentence_end(first)
        if spec.kind == "emotion":
            later = [w for w, _ in emotion_starts if w > first]
            if later:
                end = min(end, later[0] - 1)
            result.emotions.append(TagEmotion(spec.target, first, end))
        else:
            result.annotations.append(TagAnnotation(str(spec.annotation_type), spec.target, first, end))
    return result
