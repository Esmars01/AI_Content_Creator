"""CTC forced alignment (§21: tr/ar alignment, `precision: fine`).

The backend returns the CTC model's frame log-probabilities for the audio and encodes each spoken
word into the model's character tokens; the adapter runs the Viterbi alignment
(`ce_plugin_kit.align.ctc_viterbi`) over the whole segment with word separators between words,
so every spoken word gets the frames of its first and last character. Characters outside the
model's vocabulary are dropped from the target (a word left with none is interpolated between its
neighbors). Confidence is the mean posterior of the word's aligned frames, floored at 0.3."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.align import canonical_from_spans, ctc_viterbi, spoken_words
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import probe, resample_wav

__all__ = ["CTCAlignerAdapter", "word_spans"]


def word_spans(
    log_probs: np.ndarray,
    words_tokens: list[list[int]],
    *,
    blank: int,
    separator: int | None,
    frame_s: float,
    duration_s: float,
) -> list[tuple[float, float, float]]:
    """(start, end, confidence) per word from one Viterbi pass over all words' tokens."""
    targets: list[int] = []
    owner: list[int] = []
    for index, tokens in enumerate(words_tokens):
        if not tokens:
            continue
        if targets and separator is not None:
            targets.append(separator)
            owner.append(-1)
        targets += tokens
        owner += [index] * len(tokens)
    spans_tok = ctc_viterbi(log_probs, targets, blank=blank) if targets else []
    found: dict[int, tuple[int, int, list[float]]] = {}
    for (first, last), token, who in zip(spans_tok, targets, owner, strict=True):
        if who < 0:
            continue
        posterior = [math.exp(float(log_probs[t, token])) for t in range(first, last + 1)]
        if who in found:
            f, _, probs = found[who]
            found[who] = (f, last, probs + posterior)
        else:
            found[who] = (first, last, posterior)
    out: list[tuple[float, float, float] | None] = []
    for index in range(len(words_tokens)):
        if index in found:
            first, last, probs = found[index]
            start, end = first * frame_s, min(duration_s, (last + 1) * frame_s)
            out.append((round(start, 4), round(end, 4), round(max(0.3, float(np.mean(probs))), 4)))
        else:
            out.append(None)
    return _interpolate(out, [len(t) for t in words_tokens], duration_s)


def _interpolate(
    spans: list[tuple[float, float, float] | None], weights: list[int], duration_s: float
) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    k, n = 0, len(spans)
    while k < n:
        if spans[k] is not None:
            out.append(spans[k])  # type: ignore[arg-type]
            k += 1
            continue
        end = k
        while end < n and spans[end] is None:
            end += 1
        left = out[-1][1] if out else 0.0
        right = spans[end][0] if end < n else duration_s  # type: ignore[index]
        right = max(left, right)
        share = [max(1, w) for w in weights[k:end]]
        t = left
        for w in share:
            dt = (right - left) * w / sum(share)
            out.append((round(t, 4), round(t + dt, 4), 0.3))
            t += dt
        k = end
    return out


class CTCAlignerAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_asr_ctc.backend import OmniCTCBackend

        assert self.paths is not None
        return OmniCTCBackend(model_dir=self.paths.model("omniasr-ctc-1b"), defaults=self.defaults)

    async def run_asr_align(self, request: m.AlignRequest, ctx: RunContext) -> m.AlignResult:
        backend = self.require_backend()
        work = self.scratch(ctx, "align")
        wav = resample_wav(await ctx.read_artifact(request.audio), work / "audio16k.wav", 16_000)
        duration = probe(wav).duration_s
        limit = float(self.defaults.get("max_seconds", 40.0))
        if duration > limit:  # the CTC model's input limit (non-streaming)
            raise ValueError(f"ctc_aligner aligns at most {limit:g} s per call; got {duration:.1f} s")
        spoken = spoken_words(request)
        if not spoken:
            return m.AlignResult(words=[], precision="fine")
        log_probs, frame_s = await self.call(ctx, backend.emissions, str(wav))
        tokens = await self.call(ctx, backend.encode_words, spoken, request.language)
        spans = word_spans(
            np.asarray(log_probs), tokens, blank=backend.blank, separator=backend.separator, frame_s=frame_s,
            duration_s=duration,
        )  # fmt: skip
        return m.AlignResult(words=canonical_from_spans(request, spans), precision="fine")
