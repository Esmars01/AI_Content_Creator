"""Text normalization per language (§21): numbers, decimals, currencies, percentages, times, dates,
abbreviations and the voice's pronunciation lexicon, for `en, de, es, fr, it, ru, tr, az, ar`.

`normalize(text, language, lexicon)` returns:

- `tts_text`: what the engine speaks — each canonical token's core replaced by its spoken form,
  its surrounding punctuation kept (sentence intonation and pauses depend on it);
- `tokens`: comparison words, each mapped to the index of the canonical token it came from
  (`ce_core.text.tokenize`). The exact-script loop compares ASR hypotheses against them and the
  coarse aligner maps ASR word times back to script words through `source`.

Comparison words are folded per language: NFC, Turkish/Azerbaijani dotted and dotless i (`I → ı`,
`İ → i`), case-folded, punctuation and hyphens removed, Arabic harakat and tatweel removed and
alef/ya/ta-marbuta variants unified. Letters such as ı, İ, ş, ğ, ç, ö, ü, ə, x, q are preserved
(no accent stripping). Hesitation fillers are dropped from comparison words on both sides (§21:
`inserted_disfluency` words never count as errors).

`NORMALIZER_VERSION` enters the TTS cache key: changing a rule must bump it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import regex
from ce_core.text import tokenize

from ce_voice.numbers import _es_apocope, cardinal, digits, ordinal_en, ru_plural, year_en

__all__ = [
    "FILLERS",
    "NORMALIZER_VERSION",
    "Normalized",
    "SpokenToken",
    "comparison_words",
    "fold",
    "normalize",
]

NORMALIZER_VERSION = "1"

# ---------------------------------------------------------------------- per-language data

_DECIMAL = {"en": "."}  # every other supported language writes a decimal comma
_POINT = {
    "en": "point",
    "de": "Komma",
    "es": "coma",
    "fr": "virgule",
    "it": "virgola",
    "ru": "запятая",
    "tr": "virgül",
    "az": "tam",
    "ar": "فاصلة",
}
_PERCENT = {
    "en": "percent",
    "de": "Prozent",
    "es": "por ciento",
    "fr": "pour cent",
    "it": "per cento",
    "tr": "yüzde",  # prefix in Turkish: %50 → yüzde elli
    "az": "faiz",
    "ar": "بالمئة",
}
_PERCENT_PREFIX = {"tr"}
_RU_PERCENT = ("процент", "процента", "процентов")

# currency symbol → (singular, plural, subunit singular, subunit plural) per language
_CURRENCY_CODES = {"$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY", "₼": "AZN", "₽": "RUB"}
_CURRENCY: dict[str, dict[str, tuple[str, str, str, str]]] = {
    "USD": {
        "en": ("dollar", "dollars", "cent", "cents"),
        "de": ("Dollar", "Dollar", "Cent", "Cent"),
        "es": ("dólar", "dólares", "centavo", "centavos"),
        "fr": ("dollar", "dollars", "cent", "cents"),
        "it": ("dollaro", "dollari", "centesimo", "centesimi"),
        "tr": ("dolar", "dolar", "sent", "sent"),
        "az": ("dollar", "dollar", "sent", "sent"),
        "ar": ("دولار", "دولار", "سنت", "سنت"),
    },
    "EUR": {
        "en": ("euro", "euros", "cent", "cents"),
        "de": ("Euro", "Euro", "Cent", "Cent"),
        "es": ("euro", "euros", "céntimo", "céntimos"),
        "fr": ("euro", "euros", "centime", "centimes"),
        "it": ("euro", "euro", "centesimo", "centesimi"),
        "tr": ("avro", "avro", "sent", "sent"),
        "az": ("avro", "avro", "sent", "sent"),
        "ar": ("يورو", "يورو", "سنت", "سنت"),
    },
    "GBP": {
        "en": ("pound", "pounds", "penny", "pence"),
        "de": ("Pfund", "Pfund", "Penny", "Pence"),
        "es": ("libra", "libras", "penique", "peniques"),
        "fr": ("livre", "livres", "penny", "pence"),
        "it": ("sterlina", "sterline", "penny", "pence"),
        "tr": ("sterlin", "sterlin", "peni", "peni"),
        "az": ("funt", "funt", "pens", "pens"),
        "ar": ("جنيه", "جنيه", "بنس", "بنس"),
    },
    "TRY": {"tr": ("lira", "lira", "kuruş", "kuruş"), "en": ("lira", "lira", "kurus", "kurus")},
    "AZN": {"az": ("manat", "manat", "qəpik", "qəpik"), "en": ("manat", "manat", "qepik", "qepik")},
}
_RU_CURRENCY = {
    "USD": (("доллар", "доллара", "долларов"), ("цент", "цента", "центов")),
    "EUR": (("евро", "евро", "евро"), ("цент", "цента", "центов")),
    "RUB": (("рубль", "рубля", "рублей"), ("копейка", "копейки", "копеек")),
    "GBP": (("фунт", "фунта", "фунтов"), ("пенс", "пенса", "пенсов")),
}
_AND = {"en": "and", "de": "und", "es": "con", "fr": "et", "it": "e", "ru": "и", "tr": "", "az": "", "ar": "و"}

_MONTHS = {
    "en": "January February March April May June July August September October November December",
    "de": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "fr": "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "it": "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "ru": "января февраля марта апреля мая июня июля августа сентября октября ноября декабря",
    "tr": "Ocak Şubat Mart Nisan Mayıs Haziran Temmuz Ağustos Eylül Ekim Kasım Aralık",
    "az": "yanvar fevral mart aprel may iyun iyul avqust sentyabr oktyabr noyabr dekabr",
    "ar": "يناير فبراير مارس أبريل مايو يونيو يوليو أغسطس سبتمبر أكتوبر نوفمبر ديسمبر",
}

# single-token abbreviations (matched case-sensitively, with their dot)
_ABBREV: dict[str, dict[str, str]] = {
    "en": {
        "Dr.": "Doctor",
        "Mr.": "Mister",
        "Mrs.": "Missus",
        "Ms.": "Miz",
        "Prof.": "Professor",
        "St.": "Saint",
        "vs.": "versus",
        "etc.": "et cetera",
        "approx.": "approximately",
        "e.g.": "for example",
        "i.e.": "that is",
        "No.": "number",
        "Jr.": "Junior",
        "Sr.": "Senior",
    },
    "de": {
        "Dr.": "Doktor",
        "Prof.": "Professor",
        "Hr.": "Herr",
        "Fr.": "Frau",
        "Nr.": "Nummer",
        "usw.": "und so weiter",
        "bzw.": "beziehungsweise",
        "ca.": "circa",
        "z.B.": "zum Beispiel",
        "d.h.": "das heißt",
    },
    "es": {
        "Sr.": "señor",
        "Sra.": "señora",
        "Srta.": "señorita",
        "Dr.": "doctor",
        "Dra.": "doctora",
        "etc.": "etcétera",
        "Ud.": "usted",
        "Uds.": "ustedes",
    },
    "fr": {"M.": "monsieur", "Mme": "madame", "Mlle": "mademoiselle", "Dr": "docteur", "etc.": "et cetera"},
    "it": {"Sig.": "signor", "Sig.ra": "signora", "Dott.": "dottor", "ecc.": "eccetera", "Prof.": "professor"},
    "ru": {"т.е.": "то есть", "т.к.": "так как", "г.": "год", "др.": "другие"},
    "tr": {"Dr.": "Doktor", "Prof.": "Profesör", "vb.": "ve benzeri", "vs.": "vesaire"},
    "az": {"Dr.": "Doktor", "Prof.": "Professor", "və s.": "və sairə"},
    "ar": {},
}
# two-token abbreviations ("z. B.") → expansion (assigned to the first token)
_ABBREV2: dict[str, dict[tuple[str, str], str]] = {
    "en": {("e.", "g."): "for example", ("i.", "e."): "that is"},
    "de": {("z.", "B."): "zum Beispiel", ("d.", "h."): "das heißt", ("u.", "a."): "unter anderem"},
    "es": {("p.", "ej."): "por ejemplo"},
    "fr": {("c.-à-d.", ""): "c'est-à-dire"},
    "ru": {("т.", "е."): "то есть", ("т.", "к."): "так как", ("и", "т.д."): "и так далее"},
    "az": {("və", "s."): "və sairə"},
}

FILLERS: dict[str, frozenset[str]] = {
    "en": frozenset({"um", "uh", "er", "erm", "ah", "hmm", "mm", "mhm", "uhm"}),
    "de": frozenset({"äh", "ähm", "öh", "hm", "hmm", "mhm"}),
    "es": frozenset({"eh", "em", "mmm", "hmm", "este"}),
    "fr": frozenset({"euh", "heu", "hum", "hmm", "bah"}),
    "it": frozenset({"ehm", "eh", "mmm", "hmm"}),
    "ru": frozenset({"э", "эм", "ээ", "хм", "мм"}),
    "tr": frozenset({"ıı", "ııı", "hmm", "eee", "ee"}),
    "az": frozenset({"ıı", "eee", "hmm", "ee"}),
    "ar": frozenset({"امم", "اه", "ممم", "هممم"}),
}

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_HARAKAT = regex.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NON_WORD = regex.compile(r"[^\p{L}\p{M}\p{N}]+")
_EDGE = regex.compile(r"^(?P<lead>[\p{P}\p{S}--[$€£₺₼₽%+\-]]*)(?P<core>.*?)(?P<trail>[\p{P}--[%]]*)$", flags=regex.V1)
_TIME = regex.compile(r"^(\d{1,2}):(\d{2})$")
_HOUR = regex.compile(r"^(\d{1,2}):$")
_MINUTE = regex.compile(r"^(\d{2})$")
_ISO_DATE = regex.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DOT_DATE = regex.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_SLASH_DATE = regex.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_ORDINAL_EN = regex.compile(r"^(\d+)(st|nd|rd|th)$", flags=regex.I)
_SYM = "$€£₺₼₽"
_NUM = r"[+\-−]?\d[\d.,   ]*\d|[+\-−]?\d"
_CURRENCY_PRE = regex.compile(rf"^([{_SYM}])({_NUM})$")
_CURRENCY_POST = regex.compile(rf"^({_NUM})\s?([{_SYM}])$")
_PERCENT_POST = regex.compile(rf"^({_NUM})%$")
_PERCENT_PRE = regex.compile(rf"^%({_NUM})$")
_NUMBER = regex.compile(rf"^({_NUM})$")


@dataclass(frozen=True)
class SpokenToken:
    text: str
    source: int


@dataclass(frozen=True)
class Normalized:
    language: str
    tts_text: str
    tokens: tuple[SpokenToken, ...]
    pieces: tuple[str, ...] = ()  # per canonical token: its spoken form with punctuation ("" when merged)

    @property
    def words(self) -> list[str]:
        return [t.text for t in self.tokens]


def _primary(language: str) -> str:
    return language.split("-")[0].lower()


def fold(word: str, language: str) -> str:
    """Comparison form of one word (no punctuation; letters with diacritics kept)."""
    lang = _primary(language)
    text = unicodedata.normalize("NFC", word).replace("’", "'").replace("ʼ", "'")
    if lang in ("tr", "az"):
        text = text.replace("I", "ı").replace("İ", "i")
    text = text.casefold().replace("i̇", "i")
    if lang == "ar":
        text = _HARAKAT.sub("", text)
        text = text.translate(str.maketrans("أإآٱىة", "اااايه"))
    return _NON_WORD.sub("", unicodedata.normalize("NFC", text))


_EN_IS = frozenset({"it", "that", "there", "here", "what", "who", "where", "how", "he", "she", "this", "when", "why"})
_EN_SPECIAL = {
    "can't": "can not",
    "cannot": "can not",
    "won't": "will not",
    "shan't": "shall not",
    "let's": "let us",
    "ain't": "is not",
}
_EN_SUFFIX = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'m", " am"), ("'d", " would"))


def _expand_en(word: str) -> str:
    """Comparison form of an English contraction (`they're` → `they are`): ASR systems write either
    form, and the comparison must not count that as an error. Possessives (`Alex's`) are kept."""
    low = word.replace("’", "'").lower()
    if low in _EN_SPECIAL:
        return _EN_SPECIAL[low]
    for suffix, expansion in _EN_SUFFIX:
        if low.endswith(suffix) and len(low) > len(suffix):
            return low[: -len(suffix)] + expansion
    if low.endswith("'s") and low[:-2] in _EN_IS:
        return low[:-2] + " is"
    return word


def _parse_number(raw: str, language: str) -> tuple[int, str] | None:
    """(integer part, fractional digits) using the language's separators; None if ambiguous."""
    text = raw.translate(_ARABIC_DIGITS).replace("٫", ",").replace("٬", ".").replace("−", "-")
    text = text.replace(" ", "").replace(" ", "").replace(" ", "")
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    decimal = _DECIMAL.get(language, ",")
    thousands = "," if decimal == "." else "."
    if language == "ar":  # Arabic text uses either convention; a single separator with 3 digits after is thousands
        decimal, thousands = ".", ","
        if text.count(",") == 1 and "." not in text and len(text.split(",")[1]) != 3:
            decimal, thousands = ",", "."
    integer, _, fraction = text.partition(decimal)
    groups = integer.split(thousands)
    if len(groups) > 1 and (any(len(g) != 3 for g in groups[1:]) or not 1 <= len(groups[0]) <= 3):
        return None
    integer = "".join(groups)
    if not integer.isdigit() or (fraction and not fraction.isdigit()):
        return None
    return sign * int(integer), fraction


def _number_words(value: int, fraction: str, language: str) -> str:
    words = cardinal(value, language)
    if fraction:
        if language in ("es", "fr", "it") and not fraction.startswith("0") and len(fraction) <= 2:
            words += f" {_POINT[language]} {cardinal(int(fraction), language)}"
        else:
            words += f" {_POINT.get(language, 'point')} {digits(fraction, language)}"
    return words


def _currency_words(code: str, value: int, fraction: str, language: str) -> str:
    cents = int((fraction + "00")[:2]) if fraction else 0
    if language == "ru" and code in _RU_CURRENCY:
        major, minor = _RU_CURRENCY[code]
        out = f"{cardinal(value, 'ru')} {ru_plural(value, major)}"
        if cents:
            out += f" {cardinal(cents, 'ru')} {ru_plural(cents, minor)}"
        return out
    names = _CURRENCY.get(code, {}).get(language) or _CURRENCY.get(code, {}).get("en")
    if names is None:
        return _number_words(value, fraction, language)
    major_word = names[0] if abs(value) == 1 else names[1]
    amount = cardinal(value, language)
    if language == "es":
        amount = _es_apocope(amount)
    out = f"{amount} {major_word}"
    if cents:
        minor_word = names[2] if cents == 1 else names[3]
        joiner = _AND.get(language, "")
        if language == "ar":
            out += f" و{cardinal(cents, language)} {minor_word}"
        else:
            out += f" {joiner + ' ' if joiner else ''}{cardinal(cents, language)} {minor_word}"
    return out


def _percent_words(value: int, fraction: str, language: str) -> str:
    number = _number_words(value, fraction, language)
    if language == "ru":
        return f"{number} {_RU_PERCENT[2] if fraction else ru_plural(value, _RU_PERCENT)}"
    word = _PERCENT.get(language, "percent")
    return f"{word} {number}" if language in _PERCENT_PREFIX else f"{number} {word}"


def _time_words(hour: int, minute: int, language: str) -> str:
    h = cardinal(hour, language)
    if language == "en":
        if minute == 0:
            return f"{h} o'clock"
        return f"{h} {'oh ' + cardinal(minute, 'en') if minute < 10 else cardinal(minute, 'en')}"
    if language == "de":
        return f"{h} Uhr" + (f" {cardinal(minute, 'de')}" if minute else "")
    if language == "ru":
        return f"{h} {cardinal(minute, 'ru')}" if minute else f"{h} ноль ноль"
    if minute == 0:
        return {"es": f"{h} en punto", "fr": f"{h} heures", "it": f"le {h}"}.get(language, h)
    sep = {"es": " y ", "fr": " heures ", "it": " e "}.get(language, " ")
    return f"{h}{sep}{cardinal(minute, language)}"


def _minute_words(minute: int, language: str) -> str:
    if language == "en":
        return "o'clock" if minute == 0 else (f"oh {cardinal(minute, 'en')}" if minute < 10 else cardinal(minute, "en"))
    if language == "de":
        return "Uhr" if minute == 0 else f"Uhr {cardinal(minute, 'de')}"
    return cardinal(minute, language)


_DE_ORDINAL_DAY = {1: "ersten", 3: "dritten", 7: "siebten", 8: "achten"}


def _de_day(day: int) -> str:
    """Dative ordinal (`am vierten Oktober`), the form dates take after am/vom/zum."""
    if day in _DE_ORDINAL_DAY:
        return _DE_ORDINAL_DAY[day]
    return cardinal(day, "de") + ("ten" if day < 20 else "sten")


def _date_words(year: int, month: int, day: int, language: str) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    name = _MONTHS[language].split()[month - 1]
    if language == "en":
        return f"{name} {ordinal_en(day)} {year_en(year)}"
    y = cardinal(year, language)
    if language == "es":
        return f"{cardinal(day, 'es') if day > 1 else 'primero'} de {name} de {y}"
    if language == "fr":
        return f"{'premier' if day == 1 else cardinal(day, 'fr')} {name} {y}"
    if language == "it":
        return f"{'primo' if day == 1 else cardinal(day, 'it')} {name} {y}"
    if language == "de":
        return f"{_de_day(day)} {name} {y}"
    return f"{cardinal(day, language)} {name} {y}"


def _spoken(core: str, language: str) -> str | None:
    """The spoken form of one token core, or None to keep it as written."""
    text = core.translate(_ARABIC_DIGITS) if language == "ar" or any("٠" <= c <= "۹" for c in core) else core
    if not any(c.isdigit() for c in text):
        return None
    if m := _TIME.match(text):
        hour, minute = int(m[1]), int(m[2])
        if hour < 24 and minute < 60:
            return _time_words(hour, minute, language)
    if m := _ISO_DATE.match(text):
        return _date_words(int(m[1]), int(m[2]), int(m[3]), language)
    if m := _DOT_DATE.match(text):
        return _date_words(int(m[3]), int(m[2]), int(m[1]), language)
    if m := _SLASH_DATE.match(text):
        month, day = (int(m[1]), int(m[2])) if language == "en" else (int(m[2]), int(m[1]))
        return _date_words(int(m[3]), month, day, language)
    if language == "en" and (m := _ORDINAL_EN.match(text)):
        return ordinal_en(int(m[1]))
    for pattern, symbol_first in ((_CURRENCY_PRE, True), (_CURRENCY_POST, False)):
        if m := pattern.match(text):
            symbol, number = (m[1], m[2]) if symbol_first else (m[2], m[1])
            parsed = _parse_number(number, language)
            if parsed is not None:
                return _currency_words(_CURRENCY_CODES[symbol], parsed[0], parsed[1], language)
    for pattern in (_PERCENT_POST, _PERCENT_PRE):
        if m := pattern.match(text):
            parsed = _parse_number(m[1], language)
            if parsed is not None:
                return _percent_words(parsed[0], parsed[1], language)
    if m := _NUMBER.match(text):
        parsed = _parse_number(m[1], language)
        if parsed is not None:
            value, fraction = parsed
            if language == "en" and not fraction and text.isdigit() and len(text) == 4:
                return year_en(value)
            return _number_words(value, fraction, language)
    return None


def _split_edges(token: str) -> tuple[str, str, str]:
    m = _EDGE.match(token)
    if m is None:  # pragma: no cover - the pattern matches every string
        return "", token, ""
    return m["lead"], m["core"], m["trail"]


def normalize(text: str, language: str, lexicon: Mapping[str, str] | None = None) -> Normalized:
    """Spoken text and source-mapped comparison words for one segment's script text."""
    lang = _primary(language)
    lex = {fold(k, lang): v for k, v in (lexicon or {}).items() if k.strip()}
    abbrev = _ABBREV.get(lang, {})
    abbrev2 = _ABBREV2.get(lang, {})
    fillers = FILLERS.get(lang, frozenset())
    tokens = tokenize(text)
    pieces: list[str] = []
    per_token = [""] * len(tokens)
    spoken_tokens: list[SpokenToken] = []
    cursor = 0
    skip_next = False
    hour_pending: int | None = None
    for i, token in enumerate(tokens):
        pieces.append(text[cursor : token.start])
        cursor = token.end
        if skip_next:
            skip_next = False
            continue
        lead, core, trail = _split_edges(token.text)
        spoken: str | None = None
        nxt = tokens[i + 1].text if i + 1 < len(tokens) else ""
        if (token.text, nxt) in abbrev2:
            spoken, trail = abbrev2[(token.text, nxt)], ""
            skip_next = True
            cursor = tokens[i + 1].end
            pieces.append(lead + spoken + text[token.end : tokens[i + 1].start].strip())
            per_token[i] = lead + spoken
        else:
            key = fold(core, lang)
            if key and key in lex:
                spoken = lex[key]
            elif token.text.rstrip(",;:!?") in abbrev:
                bare = token.text.rstrip(",;:!?")
                spoken, core, trail = abbrev[bare], bare, token.text[len(bare) :]
            elif core in abbrev:
                spoken = abbrev[core]
            hour = _HOUR.match(token.text) if spoken is None else None
            if spoken is not None:
                pass  # lexicon or abbreviation
            elif hour_pending is not None and (m := _MINUTE.match(core)):
                spoken = _minute_words(int(m[1]), lang)
                if lang == "de" and _split_edges(nxt)[1] == "Uhr":  # "10:30 Uhr": the text says Uhr itself
                    spoken = cardinal(int(m[1]), "de") if int(m[1]) else ""
            elif hour and _MINUTE.match(_split_edges(nxt)[1]) and int(hour[1]) < 24:
                spoken, core, trail = cardinal(int(hour[1]), lang), hour[1], " "
            else:
                gap = text[token.end : tokens[i + 1].start] if i + 1 < len(tokens) else text[token.end :]
                symbol = regex.match(rf"^\s?([{_SYM}])", gap) if not trail else None
                if symbol is not None and _NUMBER.match(core):
                    spoken = _spoken(core + symbol[1], lang)
                    if spoken is not None:  # the symbol after the number is spoken with it
                        cursor = token.end + symbol.end()
                else:
                    spoken = _spoken(core, lang)
            hour_pending = int(hour[1]) if hour and spoken is not None and trail == " " else None
            pieces.append(token.text if spoken is None else f"{lead}{spoken}{trail}")
            per_token[i] = token.text if spoken is None else f"{lead}{spoken}{trail}".strip()
        said = spoken if spoken is not None else core
        if lang == "en":
            said = " ".join(_expand_en(w) for w in said.split())
        for word in regex.split(r"[\s\-‐‑]+", said):
            folded = fold(word, lang)
            if folded and folded not in fillers:
                spoken_tokens.append(SpokenToken(folded, i))
    pieces.append(text[cursor:])
    return Normalized(lang, "".join(pieces), tuple(spoken_tokens), tuple(per_token))


def comparison_words(text: str, language: str) -> list[str]:
    """The comparison words of any text (an ASR hypothesis): bracketed non-lexical annotations
    such as `[laughs]` or `(sighs)` are removed before normalizing."""
    cleaned = regex.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪", " ", text)
    # ASR spacing artifacts: "$5 .99" → "$5.99", "50 %" → "50%", "$ 5" → "$5"
    cleaned = regex.sub(r"(\d)\s+([.,]\d)", r"\1\2", cleaned)
    cleaned = regex.sub(r"(\d)\s+%", r"\1%", cleaned)
    cleaned = regex.sub(rf"([{_SYM}])\s+(\d)", r"\1\2", cleaned)
    return normalize(cleaned, language).words


def spoken_sources(normalized: Normalized, count: int) -> Sequence[list[int]]:
    """For each of `count` canonical tokens, the indices of its comparison words."""
    out: list[list[int]] = [[] for _ in range(count)]
    for j, token in enumerate(normalized.tokens):
        if 0 <= token.source < count:
            out[token.source].append(j)
    return out
