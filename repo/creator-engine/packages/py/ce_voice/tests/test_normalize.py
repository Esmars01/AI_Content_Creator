"""Language normalizers (§21): numbers, dates, currencies, abbreviations, lexicon and Turkic letters,
with the source-token mapping the coarse aligner and the exact-script loop rely on."""

from __future__ import annotations

import pytest
from ce_core.text import tokenize
from ce_voice import cardinal, comparison_words, fold, normalize, script_error_rates
from ce_voice.normalize import spoken_sources

CARDINALS = {
    "en": {21: "twenty-one", 101: "one hundred one", 2026: "two thousand twenty-six", 1_000_000: "one million"},
    "de": {1: "eins", 21: "einundzwanzig", 101: "einhunderteins", 2026: "zweitausendsechsundzwanzig"},
    "es": {21: "veintiuno", 100: "cien", 101: "ciento uno", 21_000: "veintiún mil", 1_000_000: "un millón"},
    "fr": {71: "soixante et onze", 80: "quatre-vingts", 81: "quatre-vingt-un", 200: "deux cents", 2000: "deux mille"},
    "it": {21: "ventuno", 23: "ventitré", 28: "ventotto", 1000: "mille", 2026: "duemilaventisei"},
    "ru": {21: "двадцать один", 2000: "две тысячи", 5000: "пять тысяч", 1_000_000: "один миллион"},
    "tr": {100: "yüz", 1000: "bin", 1500: "bin beş yüz", 21: "yirmi bir"},
    "az": {4: "dörd", 7: "yeddi", 1000: "min", 1250: "min iki yüz əlli"},
    "ar": {21: "واحد وعشرون", 12: "اثنا عشر", 1000: "ألف", 2000: "ألفان"},
}


@pytest.mark.parametrize("language", sorted(CARDINALS))
def test_cardinals(language: str) -> None:
    for number, words in CARDINALS[language].items():
        assert cardinal(number, language) == words, (language, number)


def test_english_numbers_dates_times_currency_and_abbreviations() -> None:
    n = normalize("It costs $5.99 on 2026-10-04 at 10:30, Dr. Smith said: 50% of 21st.", "en")
    assert n.tts_text == (
        "It costs five dollars and ninety-nine cents on October fourth twenty twenty-six at ten thirty, "
        "Doctor Smith said: fifty percent of twenty-first."
    )
    assert len(n.pieces) == len(tokenize("It costs $5.99 on 2026-10-04 at 10:30, Dr. Smith said: 50% of 21st."))


@pytest.mark.parametrize(
    ("language", "text", "spoken"),
    [
        (
            "de",
            "Es kostet 3,50 € am 04.10.2026.",
            "Es kostet drei Euro und fünfzig Cent am vierten Oktober zweitausendsechsundzwanzig.",
        ),
        ("es", "Cuesta 21 $ y el 50% de 1.000.000.", "Cuesta veintiún dólares y el cincuenta por ciento de un millón."),
        ("fr", "Il y a 71 chats et 1.000 chiens.", "Il y a soixante et onze chats et mille chiens."),
        ("ru", "У нас 1000 рублей и 50%.", "У нас одна тысяча рублей и пятьдесят процентов."),
        (
            "tr",
            "İstanbul’da 3 kişi %50 indirimle 1.500 ₺ ödedi.",
            "İstanbul’da üç kişi yüzde elli indirimle bin beş yüz lira ödedi.",
        ),
        ("ar", "لدينا ١٢٣ كتابا.", "لدينا مائة وثلاثة وعشرون كتابا."),
    ],
)
def test_languages(language: str, text: str, spoken: str) -> None:
    assert normalize(text, language).tts_text == spoken


def test_turkic_letters_are_preserved_and_folded_correctly() -> None:
    assert fold("İSTANBUL", "tr") == "istanbul"
    assert fold("IŞIK", "tr") == "ışık"
    assert fold("ILIK", "en") == "ilik"  # outside Turkic languages I is i
    words = normalize("Əli və İlqar ğ ç ö ü ş x q ı", "az").words
    assert words == ["əli", "və", "ilqar", "ğ", "ç", "ö", "ü", "ş", "x", "q", "ı"]


def test_lexicon_and_source_mapping() -> None:
    text = "Kubernetes runs 3 pods."
    n = normalize(text, "en", {"Kubernetes": "koo-ber-net-eez"})
    assert n.pieces[0] == "koo-ber-net-eez" and n.pieces[2] == "three"
    sources = spoken_sources(n, len(tokenize(text)))
    assert [n.tokens[j].text for j in sources[2]] == ["three"]
    assert all(t.source < len(tokenize(text)) for t in n.tokens)


def test_comparison_drops_fillers_and_non_lexical_annotations() -> None:
    assert comparison_words("Um, so [laughs] it is (sighs) 50 % done", "en") == [
        "so",
        "it",
        "is",
        "fifty",
        "percent",
        "done",
    ]


def test_wer_and_cer_after_normalization() -> None:
    rates = script_error_rates("It costs $5.99, um, today.", "it costs five dollars and ninety-nine cents today", "en")
    assert rates.wer == 0.0 and rates.cer == 0.0
    near = script_error_rates("It costs a lot today.", "It cost a lot today.", "en")
    assert near.wer == 0.2 and near.cer < 0.1
    compound = script_error_rates("eintausend Menschen", "ein tausend Menschen", "de")
    assert compound.wer > 0 and compound.cer == 0.0
    repeat = script_error_rates("I think so.", "I I think so.", "en", allow_repetitions=True)
    assert repeat.wer == 0.0


def test_english_contractions_compare_equal_to_their_expansions() -> None:
    rates = script_error_rates("But here's the thing... they're not.", "But here's the thing, they are not.", "en")
    assert rates.wer == 0.0 and rates.cer == 0.0
    assert comparison_words("We won't stop; Alex's dog can't.", "en") == [
        "we",
        "will",
        "not",
        "stop",
        "alexs",
        "dog",
        "can",
        "not",
    ]
    # the spoken text keeps the contraction (only the comparison expands it)
    assert normalize("they're here", "en").tts_text == "they're here"
