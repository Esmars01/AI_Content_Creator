"""The canonical word tokenizer (§11). Word indices in anchors are positions in its output.

Rules (tokenizer_version "1"):

1. Words are found with Unicode default word boundaries (UAX #29) through the `regex`
   module. A word is a boundary-delimited run that contains a letter, mark or digit, so
   in-word apostrophes (`they're`, `İstanbul'da`) and decimal points (`3.14`) stay inside it.
2. In-word hyphens are kept: words joined by a hyphen with no whitespace (`state-of-the-art`)
   form one token.
3. Punctuation directly after a word attaches to that word (`chatbots.`, `thing...`).
   Punctuation directly before a word at the start of a whitespace-delimited chunk attaches
   to the following word (`«Bonjour»`, `¿Qué`). Punctuation between two words of the same
   chunk attaches to the preceding word (`and/` + `or`).
4. A chunk of punctuation surrounded by whitespace (a lone `—`) is not a word and belongs to
   no token.
5. Tokens are slices of the original text (`start`/`end` are character offsets); the text is
   never normalized. `norm` is a comparison form (NFKC, case-folded, punctuation stripped).

Scripts written without spaces (Chinese, Japanese, Thai) are out of scope (roadmap).
The tokenizer is deterministic; any change to these rules must bump `TOKENIZER_VERSION`,
because nodes that resolve anchors include it in their cache keys.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import regex

__all__ = ["TOKENIZER_VERSION", "Token", "tokenize", "word_count"]

TOKENIZER_VERSION = "1"

_BOUNDARY = regex.compile(r"(?w)\b", flags=regex.V1)
_WORDLIKE = regex.compile(r"[\p{L}\p{M}\p{N}]")
_HYPHENS = frozenset("-‐‑")
_STRIP = regex.compile(r"^[^\p{L}\p{M}\p{N}]+|[^\p{L}\p{M}\p{N}]+$")


@dataclass(frozen=True, slots=True)
class Token:
    index: int
    text: str
    start: int
    end: int
    norm: str


def _norm(text: str) -> str:
    stripped = _STRIP.sub("", text)
    return unicodedata.normalize("NFKC", stripped).casefold()


def _pieces(chunk: str) -> list[str]:
    return [p for p in _BOUNDARY.split(chunk) if p]


def tokenize(text: str) -> list[Token]:
    """Splits `text` into word tokens with character offsets into `text`."""
    spans: list[tuple[int, int]] = []
    for chunk_match in regex.finditer(r"\S+", text):
        chunk_start = chunk_match.start()
        pos = chunk_start
        leading_start: int | None = None  # punctuation waiting for the chunk's first word
        current: list[int] | None = None  # [start, end] of the token being built in this chunk
        joining = False  # the previous piece was an in-word hyphen directly after a word
        for piece in _pieces(chunk_match.group(0)):
            piece_start, piece_end = pos, pos + len(piece)
            pos = piece_end
            if _WORDLIKE.search(piece):
                if current is not None and joining:
                    current[1] = piece_end
                else:
                    if current is not None:
                        spans.append((current[0], current[1]))
                    start = leading_start if leading_start is not None else piece_start
                    current = [start, piece_end]
                    leading_start = None
                joining = False
            else:
                if current is None:
                    if leading_start is None:
                        leading_start = piece_start
                    continue
                current[1] = piece_end
                joining = piece in _HYPHENS
        if current is not None:
            spans.append((current[0], current[1]))
    return [Token(index=i, text=text[s:e], start=s, end=e, norm=_norm(text[s:e])) for i, (s, e) in enumerate(spans)]


def word_count(text: str) -> int:
    return len(tokenize(text))
