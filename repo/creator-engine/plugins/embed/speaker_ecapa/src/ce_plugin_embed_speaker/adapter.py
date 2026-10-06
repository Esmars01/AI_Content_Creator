"""SpeechBrain ECAPA-TDNN adapter (`voice.embed`, §21 voice identity / drift checks).

The audio is resampled to 16 kHz mono; with word timings only the spoken spans (padded 50 ms) are
embedded, so long pauses and room tone do not dilute the identity. The vector is L2-normalized
(cosine similarity is a dot product); clips under `min_speech_s` of speech are refused rather
than embedded unreliably."""

from __future__ import annotations

from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import read_audio

__all__ = ["SpeakerEcapaAdapter", "speech_only"]

RATE = 16_000


def speech_only(samples: np.ndarray, words: list[m.AlignedWord], pad_s: float = 0.05) -> np.ndarray:
    """The samples inside the words' spans (merged where they touch), padded by `pad_s`."""
    if not words:
        return samples
    spans: list[list[int]] = []
    for w in sorted(words, key=lambda x: x.start_s):
        start = max(0, int((w.start_s - pad_s) * RATE))
        end = min(len(samples), int((w.end_s + pad_s) * RATE))
        if end <= start:
            continue
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    if not spans:
        return samples[:0]
    return np.concatenate([samples[s:e] for s, e in spans])


class SpeakerEcapaAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_embed_speaker.backend import EcapaBackend

        assert self.paths is not None
        return EcapaBackend(model_dir=self.paths.model("spkrec-ecapa-voxceleb"), scratch=ctx.scratch_dir)

    async def run_voice_embed(self, request: m.AudioAnalysisRequest, ctx: RunContext) -> m.EmbeddingResult:
        backend = self.require_backend()
        samples = read_audio(await ctx.read_artifact(request.audio), RATE)
        speech = speech_only(samples, list(request.word_timings))
        minimum = float(self.defaults.get("min_speech_s", 1.0))
        if len(speech) < minimum * RATE:
            raise ValueError(f"voice.embed needs at least {minimum:g} s of speech; got {len(speech) / RATE:.2f} s")
        vector = np.asarray(await self.call(ctx, backend.embed, speech), dtype=np.float64).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm == 0:
            raise ValueError("the speaker embedding is degenerate")
        unit = (vector / norm).round(6).tolist()
        return m.EmbeddingResult(vectors=[unit], dim=len(unit))
