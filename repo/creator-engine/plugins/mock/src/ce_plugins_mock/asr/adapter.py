"""Mock ASR and aligner (§37): the transcript and timings the mock TTS stored in its WAV.

For audio without the mock timings chunk, transcription returns the hint text (or nothing) and
alignment spreads the words evenly over the audio, marked `coarse`.
"""

from __future__ import annotations

import json

from ce_contracts.common import RunContext
from ce_contracts.interfaces import ASREngine
from ce_contracts.models import (
    AlignedWord,
    AlignRequest,
    AlignResult,
    LidRequest,
    LidResult,
    TranscribeRequest,
    TranscribeResult,
)

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import probe_duration, read_wav_chunk

__all__ = ["MockASR"]


class MockASR(MockAdapter, ASREngine):
    seconds_per_unit = 0.02

    async def _stored(self, request: TranscribeRequest | AlignRequest | LidRequest, ctx: RunContext) -> dict | None:  # type: ignore[type-arg]
        path = await ctx.read_artifact(request.audio)
        chunk = read_wav_chunk(path) if path.suffix.lower() == ".wav" or path.read_bytes()[:4] == b"RIFF" else None
        return json.loads(chunk.decode("utf-8")) if chunk else None

    async def run_asr_transcribe(self, request: TranscribeRequest, ctx: RunContext) -> TranscribeResult:
        stored = await self._stored(request, ctx)
        if stored is not None:
            return TranscribeResult(
                text=stored["text"],
                words=[AlignedWord.model_validate(w) for w in stored["words"]],
                language=stored.get("language") or request.language or "en",
            )
        return TranscribeResult(text=request.hint_text or "", words=[], language=request.language or "en")

    async def run_asr_align(self, request: AlignRequest, ctx: RunContext) -> AlignResult:
        stored = await self._stored(request, ctx)
        words = request.words or [w for w in request.text.split() if any(c.isalnum() for c in w)]
        if stored is not None and len(stored["words"]) == len(words):
            return AlignResult(words=[AlignedWord.model_validate(w) for w in stored["words"]], precision="fine")
        duration = await probe_duration(await ctx.read_artifact(request.audio))
        step = duration / max(1, len(words))
        return AlignResult(
            words=[
                AlignedWord(index=i, word=w, start_s=round(i * step, 4), end_s=round((i + 1) * step, 4), confidence=0.3)
                for i, w in enumerate(words)
            ],
            precision="coarse",
        )

    async def run_asr_lid(self, request: LidRequest, ctx: RunContext) -> LidResult:
        stored = await self._stored(request, ctx)
        language = (stored or {}).get("language") or "en"
        return LidResult(language=language.split("-")[0], confidence=0.9 if stored else 0.2)
