"""Acting-tag parser and exact-script extraction (§11, §21): unit cases and property tests over
Unicode text, punctuation and Turkish/Azerbaijani letters — the cleaned text is the input minus
the tags, byte for byte, and always reassembles to the input."""

from __future__ import annotations

import unicodedata

import pytest
from ce_core.text.tokenize import tokenize
from ce_core.vocab import Vocabulary
from ce_voice import CANONICAL_TAGS, ExactScriptError, extract_segments, parse_tags, reassemble, sentence_spans
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

LETTERS = "abcçdefgğhıijklmnoöprsştuüvyzqwxəABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZQWXƏéñßøåжшщ"
WORD = st.text(alphabet=LETTERS + "0123456789", min_size=1, max_size=9)
INNER = st.sampled_from(["", "", "", "'", "’", "-", "'da", "'nın"])
TRAIL = st.sampled_from(["", "", "", ",", ";", ":", ".", "!", "?", "...", "…", "”", "»"])
LEAD = st.sampled_from(["", "", "", "«", "“", "¿", "("])
SPACE = st.sampled_from([" ", " ", " ", "  ", "\n", " "])
TAG = st.sampled_from([f"[{t}]" for t in CANONICAL_TAGS] + ["[Short  Pause]", "[LAUGHS]"])


@st.composite
def tagged_scripts(draw: st.DrawFn) -> tuple[str, str]:
    """(text with tags, the same text without them) — tags sit between words with one space."""
    with_tags: list[str] = []
    without: list[str] = []
    for i in range(draw(st.integers(min_value=1, max_value=14))):
        word = draw(LEAD) + draw(WORD) + draw(INNER) + draw(TRAIL)
        sep = draw(SPACE) if i else ""
        if draw(st.booleans()) and draw(st.booleans()):
            tag = draw(TAG)
            with_tags.append(sep + tag + " " + word)
            without.append(sep + word)
        else:
            with_tags.append(sep + word)
            without.append(sep + word)
    return "".join(with_tags), "".join(without)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tagged_scripts())
def test_tags_are_removed_and_everything_else_is_kept(pair: tuple[str, str]) -> None:
    text, expected = pair
    tagged = parse_tags(text)
    assert tagged.text == expected
    assert reassemble(tagged.text, tagged.removed) == text
    count = len(tokenize(tagged.text))
    for a in tagged.annotations:
        assert 0 <= a.start_word <= a.end_word < count
    for e in tagged.emotions:
        assert 0 <= e.start_word <= e.end_word < count
    for ev in tagged.events:
        assert 0 <= ev.word < count


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=LETTERS + " []\n.,!?'«»-‐0123456789", max_size=80))
def test_any_text_reassembles_exactly(text: str) -> None:
    tagged = parse_tags(text)
    assert reassemble(tagged.text, tagged.removed) == text


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tagged_scripts(), st.sampled_from(["Here is my exact script:\n\n", "Script:\n", ""]))
def test_exact_script_extraction_covers_the_script_byte_for_byte(pair: tuple[str, str], preface: str) -> None:
    script, expected = pair
    raw = preface + script + "\n"
    start = len(preface)
    while start < len(raw) and raw[start].isspace():
        start += 1
    end = len(raw.rstrip())
    if end <= start:
        return
    spans = sentence_spans(raw, start, end)
    segments = extract_segments(raw, (start, end), spans)
    rebuilt, cleaned = [], []
    cursor = start
    for seg in segments:
        rebuilt.append(raw[cursor : seg.raw_start])
        cleaned.append(raw[cursor : seg.raw_start])
        rebuilt.append(reassemble(seg.text, seg.tagged.removed))
        cleaned.append(seg.text)
        cursor = seg.raw_end
    rebuilt.append(raw[cursor:end])
    cleaned.append(raw[cursor:end])
    assert "".join(rebuilt) == raw[start:end]  # nothing dropped, nothing invented
    assert "".join(cleaned) == expected.strip()  # the script minus its tags, byte for byte


def test_unicode_is_never_normalized() -> None:
    nfd = unicodedata.normalize("NFD", "İstanbul'da şeker ve ğüçö")  # decomposed letters
    raw = f"{nfd} [laughs] tamam."
    tagged = parse_tags(raw)
    assert tagged.text == f"{nfd} tamam." and tagged.text != unicodedata.normalize("NFC", tagged.text)
    (segment,) = extract_segments(raw, (0, len(raw)), [(0, len(raw))])
    assert segment.text.encode("utf-8") == f"{nfd} tamam.".encode()


def test_tag_semantics() -> None:
    t = parse_tags(
        "[excited] This is huge! Then [short pause] we wait [laughs]. [serious] Not [whispers] really [looks away] ok."
    )
    assert t.text == "This is huge! Then we wait. Not really ok."
    assert [(e.label, e.start_word, e.end_word) for e in t.emotions] == [("excited", 0, 2), ("serious", 6, 8)]
    by_type = {(a.type, a.tag): (a.start_word, a.end_word) for a in t.annotations}
    assert by_type[("pause", "short_pause")] == (3, 3)  # after "Then"
    assert by_type[("nonverbal_audio", "laugh")] == (4, 4)  # after "wait"
    assert by_type[("delivery", "whisper")] == (7, 8)  # "really ok." to the end of the sentence
    assert [(e.type, e.word) for e in t.events] == [("look_away", 8)]
    assert parse_tags("[short pause] Hello").issues  # nothing before it to pause after
    assert parse_tags("Say [hello] to [maybe later]").text == "Say [hello] to [maybe later]"  # not canonical


def test_canonical_tags_map_onto_the_vocabulary(repo_vocab: Vocabulary) -> None:
    for name, spec in CANONICAL_TAGS.items():
        if spec.kind == "emotion":
            assert spec.target in repo_vocab.emotions, name
        elif spec.kind == "event":
            assert spec.target in repo_vocab.events, name
        else:
            assert repo_vocab.has(f"annotation_tag.{spec.annotation_type}", spec.target), name


def test_extraction_rejects_offsets_that_drop_or_cut_text() -> None:
    raw = "Intro. Here is the script: Hello world. Second line!"
    start = raw.index("Hello")
    with pytest.raises(ExactScriptError, match="belongs to no segment"):
        extract_segments(raw, (start, len(raw)), [(start, start + 12)])  # "Second line!" dropped
    with pytest.raises(ExactScriptError, match="cuts a word"):
        extract_segments(raw, (start, len(raw)), [(start, start + 3), (start + 3, len(raw))])
    with pytest.raises(ExactScriptError, match="outside"):
        extract_segments(raw, (start, len(raw)), [(0, 5)])
    ok = extract_segments(raw, (start, len(raw)), [(start, start + 12), (start + 13, len(raw))])
    assert [s.text for s in ok] == ["Hello world.", "Second line!"]
