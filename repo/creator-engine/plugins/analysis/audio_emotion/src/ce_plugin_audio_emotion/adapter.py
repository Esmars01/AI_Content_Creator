"""emotion2vec+ adapter (`audio.emotion`, §16.1). The audio is resampled to 16 kHz mono and
classified in sliding windows (`window_s`, `hop_s`); `classes` is the mean probability per class
over the windows inside the requested word span (the whole clip without word timings). Classes are
the model's nine coarse labels; mapping them to canonical emotion labels is the vocabulary's job."""

from __future__ import annotations

from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import read_audio

__all__ = ["LABELS", "AudioEmotionAdapter", "windows"]

RATE = 16_000
LABELS = ("angry", "disgusted", "fearful", "happy", "neutral", "other", "sad", "surprised", "unknown")


def windows(n: int, window_s: float, hop_s: float) -> list[tuple[int, int]]:
    """Sample ranges of the sliding windows (the last one ends at the clip's end)."""
    size, hop = int(window_s * RATE), max(1, int(hop_s * RATE))
    if n <= size:
        return [(0, n)]
    out = [(start, start + size) for start in range(0, n - size + 1, hop)]
    if out[-1][1] < n:
        out.append((n - size, n))
    return out


class AudioEmotionAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_audio_emotion.backend import Emotion2VecBackend

        assert self.paths is not None
        return Emotion2VecBackend(model_dir=self.paths.model("emotion2vec-plus-base"), scratch=ctx.scratch_dir)

    async def run_audio_emotion(self, request: m.AudioAnalysisRequest, ctx: RunContext) -> m.AudioEmotionResult:
        backend = self.require_backend()
        samples = read_audio(await ctx.read_artifact(request.audio), RATE)
        if request.word_timings:
            start = min(w.start_s for w in request.word_timings)
            end = max(w.end_s for w in request.word_timings)
            samples = samples[int(start * RATE) : int(end * RATE)]
        if len(samples) < int(0.3 * RATE):
            raise ValueError("audio.emotion needs at least 0.3 s of audio")
        spans = windows(len(samples), float(self.defaults.get("window_s", 2.0)), float(self.defaults.get("hop_s", 1.0)))
        probabilities = []
        for a, b in spans:
            scores = await self.call(ctx, backend.classify, samples[a:b])
            probabilities.append([float(scores.get(label, 0.0)) for label in LABELS])
        mean = np.asarray(probabilities, dtype=np.float64).mean(axis=0)
        total = float(mean.sum()) or 1.0
        return m.AudioEmotionResult(
            classes={label: round(float(v) / total, 4) for label, v in zip(LABELS, mean, strict=True)}
        )
