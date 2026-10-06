"""The canonical tokenizer (§11): examples in en, de, es, tr, az, ru, ar and properties."""

from __future__ import annotations

import pytest
from ce_core.text import TOKENIZER_VERSION, tokenize, word_count
from hypothesis import given
from hypothesis import strategies as st


def words(text: str) -> list[str]:
    return [t.text for t in tokenize(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The §11 example segments: annotation word indices depend on these exact splits.
        (
            "Everyone thinks AI agents are just smarter chatbots.",
            ["Everyone", "thinks", "AI", "agents", "are", "just", "smarter", "chatbots."],
        ),
        ("But here's the thing... they're not.", ["But", "here's", "the", "thing...", "they're", "not."]),
        # en: hyphens, abbreviations, decimals
        (
            "A state-of-the-art model, e.g. 3.14 times faster.",
            ["A", "state-of-the-art", "model,", "e.g.", "3.14", "times", "faster."],
        ),
        # de: umlauts, ß, compound with hyphen
        ("Das Straßen-Café öffnet um 9 Uhr.", ["Das", "Straßen-Café", "öffnet", "um", "9", "Uhr."]),
        # es: inverted punctuation attaches to the following word
        ("¿Qué tal? ¡Muy bien!", ["¿Qué", "tal?", "¡Muy", "bien!"]),
        # tr: dotted/dotless i and apostrophe suffixes stay inside the word
        ("İstanbul'da ılık bir gün, değil mi?", ["İstanbul'da", "ılık", "bir", "gün,", "değil", "mi?"]),
        # az: schwa and other Azerbaijani letters
        ("Mən Bakıda yaşayıram, çox gözəl şəhərdir.", ["Mən", "Bakıda", "yaşayıram,", "çox", "gözəl", "şəhərdir."]),
        # ru: Cyrillic, em dash between words attaches to the preceding word
        ("Это — не чат-бот, а агент.", ["Это", "не", "чат-бот,", "а", "агент."]),
        # ar: right-to-left text, Arabic comma and question mark attach
        ("مرحبا بالعالم، كيف حالك؟", ["مرحبا", "بالعالم،", "كيف", "حالك؟"]),
        # quotes and a lone dash surrounded by spaces (not a word)
        ("«Bonjour» — dit-il.", ["«Bonjour»", "dit-il."]),
        ("Wait—what?", ["Wait—", "what?"]),
        ("", []),
        ("   ...   ", []),
    ],
)
def test_examples(text: str, expected: list[str]) -> None:
    assert words(text) == expected


def test_offsets_slice_the_original_text_without_normalization() -> None:
    text = "Café au lait"  # decomposed é must stay decomposed in the slice
    tokens = tokenize(text)
    assert [text[t.start : t.end] for t in tokens] == [t.text for t in tokens]
    assert tokens[0].text == "Café"
    assert tokens[0].norm == "café"  # comparison form is NFKC + casefold


def test_norm_strips_punctuation_and_casefolds() -> None:
    assert [t.norm for t in tokenize("Hello, WORLD!")] == ["hello", "world"]


def test_version_is_pinned() -> None:
    assert TOKENIZER_VERSION == "1"


def test_word_count() -> None:
    assert word_count("one two three.") == 3


@given(st.text(alphabet=st.characters(categories=["L", "N", "P", "Zs", "Mn"]), max_size=80))
def test_tokens_are_ordered_disjoint_slices(text: str) -> None:
    tokens = tokenize(text)
    last_end = 0
    for i, token in enumerate(tokens):
        assert token.index == i
        assert token.start >= last_end
        assert text[token.start : token.end] == token.text
        assert token.text.strip() == token.text
        last_end = token.end


@given(st.lists(st.from_regex(r"[A-Za-zÀ-ÖØ-öø-ÿİıŞşĞğ]{1,8}[.,!?]?", fullmatch=True), max_size=12))
def test_space_joined_words_tokenize_back_to_themselves(parts: list[str]) -> None:
    assert words(" ".join(parts)) == parts
