"""Mock voice engine (§37): tone bursts per word at the estimated duration, honoring pauses, with
synthetic word timings stored in the WAV (`ceTM` chunk) for the mock aligner."""

from __future__ import annotations

import hashlib
import json

from ce_contracts.common import RunContext
from ce_contracts.interfaces import VoiceEngine
from ce_contracts.models import (
    AlignedWord,
    AudioResult,
    TTSRequest,
    TTSResult,
    VoiceConvertRequest,
    VoiceDesignRequest,
    VoiceDesignResult,
    VoicePrepareRequest,
    VoicePrepareResult,
)

from ce_plugins_mock._base import MockAdapter, env_float
from ce_plugins_mock._media import read_wav, seed_int, synth_speech, write_wav
from ce_plugins_mock.voice.translator import MockVoiceTranslator

__all__ = ["MockVoice", "split_words"]


def split_words(text: str) -> list[str]:
    """Whitespace words (the orchestrator sends text whose word count matches the canonical tokenizer)."""
    return [w for w in text.split() if any(ch.isalnum() for ch in w)] or [text]


class MockVoice(MockAdapter, VoiceEngine):
    seconds_per_unit = 0.05
    translator = MockVoiceTranslator()

    async def _f0(self, request: TTSRequest, ctx: RunContext) -> float | None:
        if request.voice.conditioning is None:
            return None
        data = json.loads((await ctx.read_artifact(request.voice.conditioning)).read_text(encoding="utf-8"))
        return float(data["f0"])

    async def run_voice_tts(self, request: TTSRequest, ctx: RunContext) -> TTSResult:
        if request.behavior is not None and not request.engine:
            request = self.translator.translate(request.behavior, request)  # type: ignore[assignment]
        engine = request.engine
        words = request.words or split_words(request.text)
        pauses = {int(k): int(v) for k, v in (engine.get("pauses_ms") or {}).items()}
        samples, timings = synth_speech(
            words,
            wpm=request.wpm,
            seed=seed_int(ctx.seed, request.text),
            sample_rate=request.sample_rate,
            rate=float(engine.get("rate", 1.0)),
            energy=float(engine.get("energy", 0.6)),
            pitch_variation=float(engine.get("pitch_variation", 0.5)),
            f0=await self._f0(request, ctx),
            pauses_ms=pauses,
            emphasis=engine.get("emphasis") or (),
        )
        aligned = [
            AlignedWord(index=i, word=w, start_s=s, end_s=e)
            for i, (w, (s, e)) in enumerate(zip(words, timings, strict=True))
        ]
        spoken_text = request.text
        rate = env_float("MOCK_TTS_MISREAD_RATE", float(self.config.get("misread_rate", 0.0)))
        if rate > 0 and (seed_int(ctx.seed, "misread:" + request.text) % 10_000) / 10_000 < rate:
            # Failure injection for the exact-script loop (§21): this attempt "says" the last word wrong.
            spoken_text = " ".join([*request.text.split()[:-1], "banana."])
        chunk = json.dumps(
            {"text": spoken_text, "language": request.language, "words": [a.model_dump() for a in aligned]},
            ensure_ascii=False,
        ).encode("utf-8")
        out = self.workdir(ctx, "tts") / "speech.wav"
        write_wav(out, samples, request.sample_rate, extra_chunk=chunk)
        duration = round(len(samples) / request.sample_rate, 4)
        ref = await self.write(ctx, out, "audio", role="audio", mime="audio/wav", duration_s=duration)
        return TTSResult(audio=ref, duration_s=duration, sample_rate=request.sample_rate, word_timings=aligned)

    async def run_voice_design(self, request: VoiceDesignRequest, ctx: RunContext) -> VoiceDesignResult:
        candidates = []
        words = split_words(request.sample_text)
        for index in range(request.count):
            f0 = 95.0 + (seed_int(request.description, index) % 140)
            samples, _ = synth_speech(words, wpm=150, seed=seed_int(ctx.seed, index), f0=f0)
            out = self.workdir(ctx, "design") / f"candidate_{index + 1}.wav"
            write_wav(out, samples, 48_000)
            candidates.append(
                await self.write(ctx, out, "audio", role=f"candidate_{index + 1}", mime="audio/wav", f0=f0)
            )
        return VoiceDesignResult(candidates=candidates)

    async def run_voice_clone_prepare(self, request: VoicePrepareRequest, ctx: RunContext) -> VoicePrepareResult:
        """Per-engine conditioning: here just a pitch derived from the references and description."""
        basis = hashlib.sha256(
            (request.description + "|".join(r.audio.sha256 for r in request.references)).encode("utf-8")
        ).hexdigest()
        data = {"mock": True, "adapter_id": self.manifest.id, "f0": 100.0 + int(basis[:6], 16) % 120, "basis": basis}
        out = self.workdir(ctx, "prepare") / "conditioning.json"
        out.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        ref = await self.write(ctx, out, "voice_conditioning", role="conditioning", mime="application/json")
        return VoicePrepareResult(conditioning=ref)

    async def run_voice_convert(self, request: VoiceConvertRequest, ctx: RunContext) -> AudioResult:
        source = await ctx.read_artifact(request.audio)
        samples, rate = read_wav(source)
        out = self.workdir(ctx, "convert") / "converted.wav"
        write_wav(out, samples, rate)
        ref = await self.write(ctx, out, "audio", role="audio", mime="audio/wav")
        return AudioResult(audio=ref, duration_s=round(len(samples) / rate, 4), sample_rate=rate)
