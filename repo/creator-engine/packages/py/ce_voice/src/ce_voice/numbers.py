"""Cardinal numbers spelled out per language (§21 text normalization: en, de, es, fr, it, ru, tr, az, ar).

Forms are the citation forms a TTS engine should read: masculine nominative where gender or case
matters (Russian thousands are feminine, Arabic uses the masculine counting forms), German and
Italian compounds written as one word, French and Spanish with their irregular tens. The goal is
speakable, consistent text for synthesis and for WER comparison (both sides go through the same
normalizer), not every register of every language.
"""

from __future__ import annotations

__all__ = ["SUPPORTED", "cardinal", "digits", "ordinal_en", "ru_plural", "year_en"]

SUPPORTED = ("en", "de", "es", "fr", "it", "ru", "tr", "az", "ar")

# ---------------------------------------------------------------------- English

_EN_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_EN_SCALES = [(10**9, "billion"), (10**6, "million"), (1000, "thousand")]


def _en_99(n: int) -> str:
    if n < 20:
        return _EN_ONES[n]
    t, u = divmod(n, 10)
    return _EN_TENS[t] + (f"-{_EN_ONES[u]}" if u else "")


def _en_999(n: int) -> str:
    h, r = divmod(n, 100)
    parts = [f"{_EN_ONES[h]} hundred"] if h else []
    if r:
        parts.append(_en_99(r))
    return " ".join(parts)


def _en(n: int) -> str:
    if n == 0:
        return "zero"
    parts = []
    for scale, word in _EN_SCALES:
        if n >= scale:
            q, n = divmod(n, scale)
            parts.append(f"{_en(q) if q >= 1000 else _en_999(q)} {word}")
    if n:
        parts.append(_en_999(n))
    return " ".join(parts)


_EN_ORD_IRREGULAR = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}


def ordinal_en(n: int) -> str:
    words = _en(n)
    head, sep, last = words.rpartition(" ")
    stem_head, hyphen, unit = last.rpartition("-")
    if unit in _EN_ORD_IRREGULAR:
        unit = _EN_ORD_IRREGULAR[unit]
    elif unit.endswith("y"):
        unit = unit[:-1] + "ieth"
    else:
        unit += "th"
    return head + sep + stem_head + hyphen + unit


def year_en(n: int) -> str:
    """Years as spoken in English: 1999 → nineteen ninety-nine, 2026 → twenty twenty-six."""
    if 2000 <= n <= 2009 or n % 1000 == 0 or not 1100 <= n <= 2099:
        return _en(n)
    hi, lo = divmod(n, 100)
    if lo == 0:
        return f"{_en_99(hi)} hundred"
    return f"{_en_99(hi)} {'oh ' + _EN_ONES[lo] if lo < 10 else _en_99(lo)}"


# ---------------------------------------------------------------------- German

_DE_ONES = [
    "null",
    "eins",
    "zwei",
    "drei",
    "vier",
    "fünf",
    "sechs",
    "sieben",
    "acht",
    "neun",
    "zehn",
    "elf",
    "zwölf",
    "dreizehn",
    "vierzehn",
    "fünfzehn",
    "sechzehn",
    "siebzehn",
    "achtzehn",
    "neunzehn",
]
_DE_TENS = ["", "", "zwanzig", "dreißig", "vierzig", "fünfzig", "sechzig", "siebzig", "achtzig", "neunzig"]


def _de_99(n: int, final: bool) -> str:
    if n == 1:
        return "eins" if final else "ein"
    if n < 20:
        return _DE_ONES[n]
    t, u = divmod(n, 10)
    if not u:
        return _DE_TENS[t]
    return ("ein" if u == 1 else _DE_ONES[u]) + "und" + _DE_TENS[t]


def _de_999(n: int, final: bool = True) -> str:
    h, r = divmod(n, 100)
    out = (("ein" if h == 1 else _DE_ONES[h]) + "hundert") if h else ""
    return out + (_de_99(r, final) if r else "")


def _de(n: int) -> str:
    if n == 0:
        return "null"
    parts = []
    for scale, one, many in ((10**9, "eine Milliarde", "Milliarden"), (10**6, "eine Million", "Millionen")):
        if n >= scale:
            q, n = divmod(n, scale)
            parts.append(one if q == 1 else f"{_de_999(q, final=False)} {many}")
    word = ""
    if n >= 1000:
        q, n = divmod(n, 1000)
        word = _de_999(q, final=False) + "tausend"
    if n:
        word += _de_999(n)
    if word:
        parts.append(word)
    return " ".join(parts)


# ---------------------------------------------------------------------- Spanish

_ES_ONES = [
    "cero",
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
]
_ES_TENS = ["", "", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
_ES_HUNDREDS = [
    "",
    "ciento",
    "doscientos",
    "trescientos",
    "cuatrocientos",
    "quinientos",
    "seiscientos",
    "setecientos",
    "ochocientos",
    "novecientos",
]


def _es_99(n: int) -> str:
    if n < 30:
        return _ES_ONES[n]
    t, u = divmod(n, 10)
    return _ES_TENS[t] + (f" y {_ES_ONES[u]}" if u else "")


def _es_999(n: int) -> str:
    h, r = divmod(n, 100)
    if h == 1 and r == 0:
        return "cien"
    parts = [_ES_HUNDREDS[h]] if h else []
    if r:
        parts.append(_es_99(r))
    return " ".join(parts)


def _es_apocope(words: str) -> str:
    """`uno` before a noun or `mil`/`millón`: un, veintiún, treinta y un."""
    if words.endswith("veintiuno"):
        return words[:-9] + "veintiún"
    if words == "uno" or words.endswith(" uno"):
        return words[:-3] + "un"
    return words


def _es(n: int) -> str:
    if n == 0:
        return "cero"
    parts = []
    if n >= 10**6:
        q, n = divmod(n, 10**6)
        parts.append("un millón" if q == 1 else f"{_es_apocope(_es(q))} millones")
    if n >= 1000:
        q, n = divmod(n, 1000)
        parts.append("mil" if q == 1 else f"{_es_apocope(_es_999(q))} mil")
    if n:
        parts.append(_es_999(n))
    return " ".join(parts)


# ---------------------------------------------------------------------- French

_FR_ONES = [
    "zéro",
    "un",
    "deux",
    "trois",
    "quatre",
    "cinq",
    "six",
    "sept",
    "huit",
    "neuf",
    "dix",
    "onze",
    "douze",
    "treize",
    "quatorze",
    "quinze",
    "seize",
    "dix-sept",
    "dix-huit",
    "dix-neuf",
]
_FR_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _fr_99(n: int, final: bool = True) -> str:
    if n < 20:
        return _FR_ONES[n]
    if n < 70:
        t, u = divmod(n, 10)
        return _FR_TENS[t] + ("" if u == 0 else " et un" if u == 1 else f"-{_FR_ONES[u]}")
    if n < 80:
        return "soixante et onze" if n == 71 else f"soixante-{_FR_ONES[n - 60]}"
    if n == 80:
        return "quatre-vingts" if final else "quatre-vingt"
    return f"quatre-vingt-{_FR_ONES[n - 80]}"


def _fr_999(n: int, final: bool = True) -> str:
    h, r = divmod(n, 100)
    if not h:
        return _fr_99(r, final)
    head = "cent" if h == 1 else f"{_FR_ONES[h]} cent" + ("s" if r == 0 and final else "")
    return head + (f" {_fr_99(r, final)}" if r else "")


def _fr(n: int) -> str:
    if n == 0:
        return "zéro"
    parts = []
    for scale, word in ((10**9, "milliard"), (10**6, "million")):
        if n >= scale:
            q, n = divmod(n, scale)
            parts.append(f"{_fr_999(q)} {word}" + ("s" if q > 1 else ""))
    if n >= 1000:
        q, n = divmod(n, 1000)
        parts.append("mille" if q == 1 else f"{_fr_999(q, final=False)} mille")
    if n:
        parts.append(_fr_999(n))
    return " ".join(parts)


# ---------------------------------------------------------------------- Italian

_IT_ONES = [
    "zero",
    "uno",
    "due",
    "tre",
    "quattro",
    "cinque",
    "sei",
    "sette",
    "otto",
    "nove",
    "dieci",
    "undici",
    "dodici",
    "tredici",
    "quattordici",
    "quindici",
    "sedici",
    "diciassette",
    "diciotto",
    "diciannove",
]
_IT_TENS = ["", "", "venti", "trenta", "quaranta", "cinquanta", "sessanta", "settanta", "ottanta", "novanta"]


def _it_99(n: int) -> str:
    if n < 20:
        return _IT_ONES[n]
    t, u = divmod(n, 10)
    tens = _IT_TENS[t]
    if u in (1, 8):
        tens = tens[:-1]
    if not u:
        return tens
    return tens + ("tré" if u == 3 else _IT_ONES[u])


def _it_999(n: int) -> str:
    h, r = divmod(n, 100)
    head = ("cento" if h == 1 else _IT_ONES[h] + "cento") if h else ""
    return head + (_it_99(r) if r else "")


def _it(n: int) -> str:
    if n == 0:
        return "zero"
    parts = []
    for scale, one, many in ((10**9, "un miliardo", "miliardi"), (10**6, "un milione", "milioni")):
        if n >= scale:
            q, n = divmod(n, scale)
            parts.append(one if q == 1 else f"{_it_999(q)} {many}")
    word = ""
    if n >= 1000:
        q, n = divmod(n, 1000)
        stem = _it_999(q)
        word = "mille" if q == 1 else (stem[:-1] if stem.endswith("uno") else stem) + "mila"
    if n:
        word += _it_999(n)
    if word:
        parts.append(word)
    return " ".join(parts)


# ---------------------------------------------------------------------- Russian

_RU_ONES = [
    "ноль",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
]
_RU_TENS = [
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
]
_RU_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def _ru_999(n: int, feminine: bool = False) -> str:
    h, r = divmod(n, 100)
    parts = [_RU_HUNDREDS[h]] if h else []
    if r >= 20:
        t, u = divmod(r, 10)
        parts.append(_RU_TENS[t])
        r = u
    if r:
        if feminine and r in (1, 2):
            parts.append("одна" if r == 1 else "две")
        else:
            parts.append(_RU_ONES[r])
    return " ".join(parts)


def _ru(n: int) -> str:
    if n == 0:
        return "ноль"
    parts = []
    for scale, forms, feminine in (
        (10**9, ("миллиард", "миллиарда", "миллиардов"), False),
        (10**6, ("миллион", "миллиона", "миллионов"), False),
        (1000, ("тысяча", "тысячи", "тысяч"), True),
    ):
        if n >= scale:
            q, n = divmod(n, scale)
            parts.append(f"{_ru_999(q, feminine)} {ru_plural(q, forms)}")
    if n:
        parts.append(_ru_999(n))
    return " ".join(parts)


# ---------------------------------------------------------------------- Turkish and Azerbaijani


def _turkic(ones: list[str], tens: list[str], hundred: str, thousand: str, million: str, billion: str):  # type: ignore[no-untyped-def]
    def below(n: int) -> str:
        h, r = divmod(n, 100)
        t, u = divmod(r, 10)
        parts = []
        if h:
            parts.append(hundred if h == 1 else f"{ones[h]} {hundred}")
        if t:
            parts.append(tens[t])
        if u:
            parts.append(ones[u])
        return " ".join(parts)

    def spell(n: int) -> str:
        if n == 0:
            return ones[0]
        parts = []
        for scale, word in ((10**9, billion), (10**6, million)):
            if n >= scale:
                q, n = divmod(n, scale)
                parts.append(f"{below(q)} {word}")
        if n >= 1000:
            q, n = divmod(n, 1000)
            parts.append(thousand if q == 1 else f"{below(q)} {thousand}")
        if n:
            parts.append(below(n))
        return " ".join(parts)

    return spell


_tr = _turkic(
    ["sıfır", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"],
    ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"],
    "yüz",
    "bin",
    "milyon",
    "milyar",
)
_az = _turkic(
    ["sıfır", "bir", "iki", "üç", "dörd", "beş", "altı", "yeddi", "səkkiz", "doqquz"],
    ["", "on", "iyirmi", "otuz", "qırx", "əlli", "altmış", "yetmiş", "səksən", "doxsan"],
    "yüz",
    "min",
    "milyon",
    "milyard",
)

# ---------------------------------------------------------------------- Arabic (MSA, masculine counting forms)

_AR_ONES = ["صفر", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة", "عشرة"]
_AR_TEENS = {
    11: "أحد عشر",
    12: "اثنا عشر",
    13: "ثلاثة عشر",
    14: "أربعة عشر",
    15: "خمسة عشر",
    16: "ستة عشر",
    17: "سبعة عشر",
    18: "ثمانية عشر",
    19: "تسعة عشر",
}
_AR_TENS = ["", "", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_AR_HUNDREDS = [
    "",
    "مائة",
    "مائتان",
    "ثلاثمائة",
    "أربعمائة",
    "خمسمائة",
    "ستمائة",
    "سبعمائة",
    "ثمانمائة",
    "تسعمائة",
]


def _ar_99(n: int) -> str:
    if n <= 10:
        return _AR_ONES[n]
    if n < 20:
        return _AR_TEENS[n]
    t, u = divmod(n, 10)
    return f"{_AR_ONES[u]} و{_AR_TENS[t]}" if u else _AR_TENS[t]


def _ar_999(n: int) -> str:
    h, r = divmod(n, 100)
    parts = [_AR_HUNDREDS[h]] if h else []
    if r:
        parts.append(_ar_99(r))
    return " و".join(parts)


def _ar_scale(q: int, one: str, two: str, few: str, many: str) -> str:
    if q == 1:
        return one
    if q == 2:
        return two
    if 3 <= q <= 10:
        return f"{_ar_99(q)} {few}"
    return f"{_ar_999(q)} {many}"


def _ar(n: int) -> str:
    if n == 0:
        return "صفر"
    parts = []
    for scale, forms in (
        (10**9, ("مليار", "ملياران", "مليارات", "مليار")),
        (10**6, ("مليون", "مليونان", "ملايين", "مليون")),
        (1000, ("ألف", "ألفان", "آلاف", "ألف")),
    ):
        if n >= scale:
            q, n = divmod(n, scale)
            parts.append(_ar_scale(q, *forms))
    if n:
        parts.append(_ar_999(n))
    return " و".join(parts)


_SPELLERS = {"en": _en, "de": _de, "es": _es, "fr": _fr, "it": _it, "ru": _ru, "tr": _tr, "az": _az, "ar": _ar}
_MINUS = {
    "en": "minus",
    "de": "minus",
    "es": "menos",
    "fr": "moins",
    "it": "meno",
    "ru": "минус",
    "tr": "eksi",
    "az": "mənfi",
    "ar": "سالب",
}


def cardinal(n: int, language: str) -> str:
    """`n` spelled out in `language` (primary subtag); unsupported languages keep the digits."""
    speller = _SPELLERS.get(language)
    if speller is None or abs(n) >= 10**12:
        return str(n)
    if n < 0:
        return f"{_MINUS[language]} {speller(-n)}"
    return str(speller(n))


def digits(text: str, language: str) -> str:
    """Digit by digit (decimal fractions, codes)."""
    return " ".join(cardinal(int(d), language) for d in text if d.isdigit())
