"""Qwen3-ASR language names (`qwen_asr.inference.utils.SUPPORTED_LANGUAGES`) ↔ ISO 639 codes."""

from __future__ import annotations

__all__ = ["ALIGNER_LANGUAGES", "ASR_LANGUAGES", "code_of", "name_of"]

ASR_LANGUAGES = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ar": "Arabic", "de": "German", "fr": "French",
    "es": "Spanish", "pt": "Portuguese", "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish", "hi": "Hindi", "ms": "Malay",
    "nl": "Dutch", "sv": "Swedish", "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "fil": "Filipino", "fa": "Persian", "el": "Greek", "hu": "Hungarian", "mk": "Macedonian", "ro": "Romanian",
}  # fmt: skip
# README model table: Qwen3-ForcedAligner-0.6B aligns 11 languages.
ALIGNER_LANGUAGES = frozenset({"zh", "en", "yue", "fr", "de", "it", "ja", "ko", "pt", "ru", "es"})
_ALIASES = {"tl": "fil", "iw": "he", "cmn": "zh"}


def _primary(code: str) -> str:
    primary = code.split("-")[0].lower()
    return _ALIASES.get(primary, primary)


def name_of(code: str, *, aligner: bool = False) -> str:
    primary = _primary(code)
    if primary not in ASR_LANGUAGES or (aligner and primary not in ALIGNER_LANGUAGES):
        what = "Qwen3-ForcedAligner" if aligner else "Qwen3-ASR"
        raise ValueError(f"{what} does not support {code!r}")
    return ASR_LANGUAGES[primary]


def code_of(name: str) -> str:
    """A Qwen3-ASR language name (or a merged `"Chinese,English"`) → the code of the first one; '' if unknown."""
    first = name.split(",")[0].strip().lower()
    for code, known in ASR_LANGUAGES.items():
        if known.lower() == first:
            return code
    return ""
