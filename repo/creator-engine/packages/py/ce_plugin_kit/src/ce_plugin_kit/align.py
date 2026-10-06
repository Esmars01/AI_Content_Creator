"""Word alignment shared by ASR-based aligners (Phase 7 `faster_whisper_cpu`, Phase 8 Qwen3 and CTC
aligners): heard units with times → the script's spoken words → the canonical words (§21).

Heard words are folded and split on hyphens; a global alignment over folded words (exact match,
fuzzy match by character similarity, or a gap) maps them onto the spoken words; spoken words
without a heard counterpart get times interpolated between their neighbors, weighted by length.
Every canonical word gets a span: the union of its spoken words' spans.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from itertools import pairwise

import numpy as np
from ce_contracts.models import AlignedWord, AlignRequest

__all__ = [
    "align_words",
    "canonical_from_spans",
    "ctc_viterbi",
    "fold",
    "split_heard",
    "spoken_words",
    "to_canonical",
]


def fold(word: str, language: str = "") -> str:
    text = unicodedata.normalize("NFC", word)
    if language.split("-")[0] in ("tr", "az"):
        text = text.replace("I", "ı").replace("İ", "i")
    text = text.casefold().replace("i̇", "i")
    return "".join(ch for ch in text if ch.isalnum())


def split_heard(words: list[tuple[str, float, float]], language: str) -> list[tuple[str, float, float]]:
    """Heard words split on hyphens and folded; a split word shares its span by length."""
    out: list[tuple[str, float, float]] = []
    for text, start, end in words:
        parts = [fold(p, language) for p in text.replace("‐", "-").replace("‑", "-").split("-")]
        parts = [p for p in parts if p]
        total = sum(len(p) for p in parts) or 1
        t = start
        for p in parts:
            dt = (end - start) * len(p) / total
            out.append((p, t, t + dt))
            t += dt
    return out


def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def align_words(
    heard: list[tuple[str, float, float]], spoken: list[str], duration_s: float
) -> list[tuple[float, float, float]]:
    """(start, end, confidence) for each spoken word."""
    n, m = len(spoken), len(heard)
    if n == 0:
        return []
    gap = -0.6
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]  # 0 diag, 1 up (skip spoken), 2 left (skip heard)
    for i in range(1, n + 1):
        score[i][0], back[i][0] = i * gap, 1
    for j in range(1, m + 1):
        score[0][j], back[0][j] = j * gap, 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sim = _similar(spoken[i - 1], heard[j - 1][0])
            match = (2.0 if sim == 1.0 else (sim * 1.5 if sim >= 0.6 else -1.0)) + score[i - 1][j - 1]
            up, left = score[i - 1][j] + gap, score[i][j - 1] + gap
            best = max(match, up, left)
            score[i][j] = best
            back[i][j] = 0 if best == match else (1 if best == up else 2)
    spans: list[tuple[float, float, float] | None] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        move = back[i][j]
        if move == 0:
            sim = _similar(spoken[i - 1], heard[j - 1][0])
            if sim >= 0.6:
                _, s, e = heard[j - 1]
                spans[i - 1] = (s, e, 1.0 if sim == 1.0 else 0.6)
            i, j = i - 1, j - 1
        elif move == 1:
            i -= 1
        else:
            j -= 1
    # interpolate the unmatched runs between their matched neighbors, weighted by length
    out: list[tuple[float, float, float]] = []
    k = 0
    while k < n:
        if spans[k] is not None:
            out.append(spans[k])  # type: ignore[arg-type]
            k += 1
            continue
        run_end = k
        while run_end < n and spans[run_end] is None:
            run_end += 1
        left = out[-1][1] if out else 0.0
        right = spans[run_end][0] if run_end < n else duration_s  # type: ignore[index]
        right = max(right, left)
        lengths = [max(1, len(w)) for w in spoken[k:run_end]]
        total = sum(lengths)
        t = left
        for length in lengths:
            dt = (right - left) * length / total
            out.append((t, t + dt, 0.3))
            t += dt
        k = run_end
    # monotonic and inside the audio
    fixed: list[tuple[float, float, float]] = []
    cursor = 0.0
    for s, e, c in out:
        s = min(max(s, cursor), duration_s)
        e = min(max(e, s), duration_s)
        fixed.append((round(s, 4), round(e, 4), c))
        cursor = s
    return fixed


def to_canonical(
    request: AlignRequest, heard: Sequence[tuple[str, float, float]], duration_s: float
) -> list[AlignedWord]:
    """Heard units `(text, start, end)` (unfolded) → one AlignedWord per canonical word of the request."""
    units = split_heard(list(heard), request.language)
    spoken = spoken_words(request)
    return canonical_from_spans(request, align_words(units, spoken, duration_s))


def spoken_words(request: AlignRequest) -> list[str]:
    """The comparison words of the request (the normalizer's spoken words, or the folded canonical words)."""
    return list(request.spoken_words) or [fold(w, request.language) for w in request.words]


def canonical_from_spans(request: AlignRequest, spans: Sequence[tuple[float, float, float]]) -> list[AlignedWord]:
    """One `(start, end, confidence)` per spoken word → one AlignedWord per canonical word: the union
    of its spoken words' spans; a canonical word with nothing spoken gets zero length at the cursor."""
    spoken = spoken_words(request)
    sources = list(request.spoken_sources) or list(range(len(spoken)))
    per_word: list[list[tuple[float, float, float]]] = [[] for _ in request.words]
    for span, source in zip(spans, sources, strict=True):
        if 0 <= source < len(per_word):
            per_word[source].append(span)
    out: list[AlignedWord] = []
    cursor = 0.0
    for index, word in enumerate(request.words):
        group = per_word[index]
        if group:
            start, end = min(s for s, _, _ in group), max(e for _, e, _ in group)
            confidence = min(c for _, _, c in group)
        else:  # a word with nothing spoken (merged abbreviation): zero length at the cursor
            start = end = cursor
            confidence = 0.3
        start = max(start, cursor)
        end = max(end, start)
        out.append(
            AlignedWord(index=index, word=word, start_s=round(start, 4), end_s=round(end, 4), confidence=confidence)
        )
        cursor = start
    return out


def ctc_viterbi(log_probs: np.ndarray, targets: Sequence[int], blank: int = 0) -> list[tuple[int, int]]:
    """CTC forced alignment (Viterbi over the blank-extended target): for each target token, the
    first and last emission frame it occupies. `log_probs` is `[T, V]` (log-softmax). Raises
    ValueError when the audio has too few frames for the tokens."""
    lp = np.asarray(log_probs, dtype=np.float64)
    frames, tokens = lp.shape[0], len(targets)
    if tokens == 0:
        return []
    ext = [blank]
    for t in targets:
        ext += [int(t), blank]
    states = len(ext)
    repeats = sum(1 for a, b in pairwise(targets) if a == b)
    if frames < tokens + repeats:
        raise ValueError(f"CTC alignment needs at least {tokens + repeats} frames, the audio has {frames}")
    neg = -np.inf
    alpha = np.full((frames, states), neg)
    back = np.zeros((frames, states), dtype=np.int8)  # 0 stay, 1 from s-1, 2 from s-2
    ext_arr = np.asarray(ext)
    emit = lp[:, ext_arr]
    alpha[0, 0] = emit[0, 0]
    alpha[0, 1] = emit[0, 1]
    skip = np.zeros(states, dtype=bool)
    for s in range(2, states):
        skip[s] = ext[s] != blank and ext[s] != ext[s - 2]
    for t in range(1, frames):
        prev = alpha[t - 1]
        stay = prev
        one = np.concatenate([[neg], prev[:-1]])
        two = np.where(skip, np.concatenate([[neg, neg], prev[:-2]]), neg)
        stacked = np.stack([stay, one, two])
        choice = np.argmax(stacked, axis=0)
        alpha[t] = stacked[choice, np.arange(states)] + emit[t]
        back[t] = choice
    s = states - 1 if alpha[-1, -1] >= alpha[-1, -2] else states - 2
    if not np.isfinite(alpha[-1, s]):
        raise ValueError("no CTC path through the tokens")
    path = np.empty(frames, dtype=np.int64)
    for t in range(frames - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])
    spans: list[tuple[int, int]] = []
    for index in range(tokens):
        hits = np.nonzero(path == 2 * index + 1)[0]
        spans.append((int(hits[0]), int(hits[-1])))
    return spans
