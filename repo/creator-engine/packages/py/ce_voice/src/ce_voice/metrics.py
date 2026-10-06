"""Normalized WER and CER for the exact-script verification loop (§21).

Both sides go through `ce_voice.normalize`: numbers, dates and abbreviations are spelled the same
way, case and punctuation are folded per language, hesitation fillers are ignored, and bracketed
non-lexical annotations in the hypothesis (`[laughs]`, `(sighs)`) are dropped — tags that produce
non-lexical sounds never count as errors. With `allow_repetitions` (a segment that carries an
`inserted_disfluency` repetition or false start) immediate repeats in the hypothesis are collapsed
before scoring. CER is computed on the words joined without spaces, so a compound written as one
word (`eintausend`) and as two (`ein tausend`) costs nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ce_voice.normalize import comparison_words, normalize

__all__ = ["ErrorRates", "cer", "edit_distance", "script_error_rates", "wer"]


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, y in enumerate(b, start=1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y))
        previous = current
    return previous[-1]


def wer(reference: Sequence[str], hypothesis: Sequence[str]) -> float:
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return round(edit_distance(reference, hypothesis) / len(reference), 4)


def cer(reference: Sequence[str], hypothesis: Sequence[str]) -> float:
    ref, hyp = "".join(reference), "".join(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return round(edit_distance(list(ref), list(hyp)) / len(ref), 4)


def _collapse(words: list[str]) -> list[str]:
    out: list[str] = []
    for word in words:
        if not out or out[-1] != word:
            out.append(word)
    return out


@dataclass(frozen=True)
class ErrorRates:
    wer: float
    cer: float
    reference: tuple[str, ...]
    hypothesis: tuple[str, ...]


def script_error_rates(
    script: str,
    hypothesis: str,
    language: str,
    *,
    lexicon: dict[str, str] | None = None,
    allow_repetitions: bool = False,
) -> ErrorRates:
    reference = normalize(script, language, lexicon).words
    heard = comparison_words(hypothesis, language)
    if allow_repetitions:
        heard = _collapse(heard)
    return ErrorRates(wer(reference, heard), cer(reference, heard), tuple(reference), tuple(heard))
