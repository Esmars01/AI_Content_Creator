"""Qwen3-ASR and Qwen3-ForcedAligner adapters (§21).

`asr.transcribe` never sees the script (no context, no hints: verification must hear what was
said); with a language the ForcedAligner supports it also returns word timestamps (otherwise text
only). `asr.lid` transcribes without a language and reads the detected one. `asr.align` aligns
the script's spoken words with the ForcedAligner and maps its units onto the canonical words
(`ce_plugin_kit.align`), `precision: fine`. Qwen3 reports no per-word probability: transcribed
words carry confidence 1.0; aligned words carry the mapping confidence (1.0 exact, 0.6 fuzzy,
0.3 interpolated)."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import ArtifactRef, LoadContext, RunContext
from ce_plugin_kit.align import fold, spoken_words, to_canonical
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import probe, resample_wav

from ce_plugin_asr_qwen3.languages import ALIGNER_LANGUAGES, code_of, name_of

__all__ = ["Qwen3ASRAdapter", "Qwen3AlignerAdapter"]

MAX_ALIGN_S = 300.0  # README: the aligner handles up to 5 minutes of speech


class Qwen3ASRAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_asr_qwen3.backend import Qwen3ASRBackend

        assert self.paths is not None
        return Qwen3ASRBackend(
            asr_dir=self.paths.model("qwen3-asr-1.7b"),
            aligner_dir=self.paths.dependency("forced_aligner"),
            defaults=self.defaults,
        )

    async def _wav(self, ctx: RunContext, ref: ArtifactRef, name: str) -> tuple[str, float]:
        work = self.scratch(ctx, name)
        wav = resample_wav(await ctx.read_artifact(ref), work / "audio16k.wav", 16_000)
        return str(wav), probe(wav).duration_s

    async def run_asr_transcribe(self, request: m.TranscribeRequest, ctx: RunContext) -> m.TranscribeResult:
        backend = self.require_backend()
        wav, duration = await self._wav(ctx, request.audio, "transcribe")
        language = name_of(request.language) if request.language else None
        detected, text = await self.call(ctx, backend.transcribe, wav, language)
        code = request.language or code_of(detected)
        words: list[m.AlignedWord] = []
        primary = code.split("-")[0].lower() if code else ""
        if text.strip() and primary in ALIGNER_LANGUAGES and bool(self.defaults.get("word_timestamps", True)):
            items = await self.call(ctx, backend.align, wav, text, name_of(primary, aligner=True))
            for index, (unit, start, end) in enumerate(items):
                start = min(max(0.0, float(start)), duration)
                words.append(
                    m.AlignedWord(index=index, word=unit, start_s=start, end_s=min(max(start, float(end)), duration))
                )
        return m.TranscribeResult(text=text.strip(), words=words, language=code or "und")

    async def run_asr_lid(self, request: m.LidRequest, ctx: RunContext) -> m.LidResult:
        backend = self.require_backend()
        wav, _ = await self._wav(ctx, request.audio, "lid")
        detected, _ = await self.call(ctx, backend.transcribe, wav, None)
        code = code_of(detected)
        if not code:
            return m.LidResult(language="und", confidence=0.0)
        # Qwen3-ASR names the language without a probability: a mixed result ("Chinese,English")
        # gets half the configured confidence (an uncalibrated constant, not a probability).
        confidence = float(self.defaults.get("lid_confidence", 0.8))
        if "," in detected:
            confidence /= 2
        return m.LidResult(language=code, confidence=confidence)


class Qwen3AlignerAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_asr_qwen3.backend import Qwen3AlignerBackend

        assert self.paths is not None
        return Qwen3AlignerBackend(aligner_dir=self.paths.model("qwen3-forced-aligner-0.6b"), defaults=self.defaults)

    async def run_asr_align(self, request: m.AlignRequest, ctx: RunContext) -> m.AlignResult:
        backend = self.require_backend()
        language = name_of(request.language, aligner=True)
        work = self.scratch(ctx, "align")
        wav = resample_wav(await ctx.read_artifact(request.audio), work / "audio16k.wav", 16_000)
        duration = probe(wav).duration_s
        if duration > MAX_ALIGN_S:
            raise ValueError(f"Qwen3-ForcedAligner aligns at most {MAX_ALIGN_S:g} s; got {duration:.1f} s")
        spoken = spoken_words(request)
        if not spoken:
            return m.AlignResult(words=[], precision="fine")
        items = await self.call(ctx, backend.align, str(wav), " ".join(spoken), language)
        heard = [
            (str(unit), float(start), float(end)) for unit, start, end in items if fold(str(unit), request.language)
        ]
        return m.AlignResult(words=to_canonical(request, heard, duration), precision="fine")
