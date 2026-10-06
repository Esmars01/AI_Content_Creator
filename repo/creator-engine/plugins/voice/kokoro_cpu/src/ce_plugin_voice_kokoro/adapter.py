"""Kokoro-82M on CPU through kokoro-onnx (ONNX Runtime).

- Text: the normalizer's spoken words (`TTSRequest.spoken`) when present, else `text`.
- Speed: `rate × wpm / base_wpm`, clamped, times a small seeded jitter: exact-script retries use a
  new attempt seed, so each attempt differs slightly (Kokoro is otherwise deterministic).
- Pauses: the text is split after each paused word; chunks are synthesized separately and joined
  with exact silence.
- Output: 24 kHz mono 16-bit WAV (Kokoro's native rate); downstream decoding resamples.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import wave
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import VoiceEngine
from ce_contracts.models import TTSRequest, TTSResult, VoicePrepareRequest, VoicePrepareResult

from ce_plugin_voice_kokoro.translator import KokoroTranslator

__all__ = ["KokoroCpu", "choose_voice", "speed_for"]

SAMPLE_RATE = 24_000
_MALE = ("male", "man", "masculine", "baritone", "bass", "deep voice", "his ", " he ", "guy")
_FEMALE = ("female", "woman", "feminine", "soprano", "her ", " she ", "girl", "lady")


def _primary(language: str) -> str:
    return language.split("-")[0].lower()


def _gender(description: str) -> str | None:
    text = f" {description.lower()} "
    female = any(k in text for k in _FEMALE)
    male = any(k in text for k in _MALE)
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return None


def choose_voice(languages: dict[str, Any], language: str, description: str, basis: str) -> tuple[str, str]:
    """(voice id, Kokoro language code): a preset of the description's gender, picked by `basis`."""
    entry = languages.get(_primary(language)) or languages["en"]
    gender = _gender(description)
    pool = list(entry.get(gender, [])) if gender else [*entry.get("male", []), *entry.get("female", [])]
    pool = pool or [*entry.get("male", []), *entry.get("female", [])]
    index = int(hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8], 16) % len(pool)
    return str(pool[index]), str(entry["lang"])


def speed_for(rate: float, wpm: float, base_wpm: float, low: float, high: float, jitter: float, seed: int) -> float:
    unit = (int(hashlib.sha256(f"kokoro-attempt:{seed}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF) * 2.0 - 1.0
    speed = rate * (wpm / base_wpm) * (1.0 + jitter * unit)
    return round(min(high, max(low, speed)), 4)


def _chunks(words: list[str], pauses: dict[int, int]) -> list[tuple[str, int]]:
    """(text, silence_ms after it) — split after each paused word."""
    out: list[tuple[str, int]] = []
    current: list[str] = []
    for index, word in enumerate(words):
        if word:
            current.append(word)
        if index in pauses and current:
            out.append((" ".join(current), pauses[index]))
            current = []
    if current:
        out.append((" ".join(current), 0))
    return out


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


class KokoroCpu(VoiceEngine):
    seconds_per_unit = 2.5
    translator = KokoroTranslator()

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self._engine: Any = None
        self._lock = asyncio.Lock()

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        root = Path(ctx.model_cache_dir)
        model, voices = root / self.config["model_file"], root / self.config["voices_file"]
        for path in (model, voices):
            if not path.is_file():
                raise FileNotFoundError(f"{path} is missing: run `make fetch-cpu-assets`")
        self._engine = await asyncio.to_thread(self._open, model, voices)

    @staticmethod
    def _open(model: Path, voices: Path) -> Any:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        return Kokoro.from_session(session, str(voices))

    async def unload(self) -> None:
        self._engine = None
        await super().unload()

    async def _voice(self, request: TTSRequest, ctx: RunContext) -> tuple[str, str]:
        if request.voice.conditioning is not None:
            data = json.loads((await ctx.read_artifact(request.voice.conditioning)).read_text(encoding="utf-8"))
            if data.get("engine") == "kokoro" and data.get("voice"):
                entry = self.config["languages"].get(_primary(request.language))
                if entry is not None and str(data.get("language")) == _primary(request.language):
                    return str(data["voice"]), str(entry["lang"])
                return choose_voice(
                    self.config["languages"], request.language, data.get("description", ""), data["basis"]
                )
        return choose_voice(
            self.config["languages"], request.language, request.voice.description, request.voice.description
        )

    def _synthesize(self, chunks: list[tuple[str, int]], voice: str, lang: str, speed: float) -> np.ndarray:
        parts: list[np.ndarray] = []
        for text, silence_ms in chunks:
            samples, rate = self._engine.create(
                text, voice=voice, speed=speed, lang=lang, sentence_pause=float(self.config["sentence_pause_s"])
            )
            if rate != SAMPLE_RATE:  # pragma: no cover - kokoro v1.0 is 24 kHz
                raise RuntimeError(f"unexpected Kokoro sample rate {rate}")
            parts.append(np.asarray(samples, dtype=np.float32))
            if silence_ms:
                parts.append(np.zeros(round(SAMPLE_RATE * silence_ms / 1000.0), dtype=np.float32))
        return np.concatenate(parts) if parts else np.zeros(SAMPLE_RATE // 4, dtype=np.float32)

    async def run_voice_tts(self, request: TTSRequest, ctx: RunContext) -> TTSResult:
        if self._engine is None:
            raise RuntimeError("kokoro_cpu is not loaded")
        if request.behavior is not None and not request.engine:
            request = self.translator.translate(request.behavior, request)  # type: ignore[assignment]
        engine = request.engine
        words = list(request.spoken) if request.spoken else (request.words or request.text.split())
        pauses = {int(k): int(v) for k, v in (engine.get("pauses_ms") or {}).items()}
        low, high = (float(x) for x in self.config["speed_range"])
        speed = speed_for(
            float(engine.get("rate", 1.0)),
            float(request.wpm),
            float(self.config["base_wpm"]),
            low,
            high,
            float(self.config["attempt_jitter"]),
            int(ctx.seed or 0),
        )
        voice, lang = await self._voice(request, ctx)
        async with self._lock:  # one ONNX session; synthesis is CPU bound
            samples = await asyncio.to_thread(self._synthesize, _chunks(words, pauses), voice, lang, speed)
        out = Path(ctx.scratch_dir) / "kokoro" / "speech.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(out, samples)
        duration = round(len(samples) / SAMPLE_RATE, 4)
        ref = await ctx.write_artifact(
            out,
            "audio",
            {"adapter_id": self.manifest.id, "voice": voice, "speed": speed, "duration_s": duration},
            role="audio",
            mime="audio/wav",
        )
        return TTSResult(audio=ref, duration_s=duration, sample_rate=SAMPLE_RATE, word_timings=None)

    async def run_voice_clone_prepare(self, request: VoicePrepareRequest, ctx: RunContext) -> VoicePrepareResult:
        """Kokoro cannot clone: the conditioning names a preset voice chosen from the description
        (and the reference language), deterministically."""
        language = request.references[0].language if request.references else "en"
        basis = hashlib.sha256(
            (request.description + "|" + "|".join(r.audio.sha256 for r in request.references)).encode("utf-8")
        ).hexdigest()
        voice, _ = choose_voice(self.config["languages"], language, request.description, basis)
        data = {
            "engine": "kokoro",
            "adapter_id": self.manifest.id,
            "voice": voice,
            "language": _primary(language),
            "description": request.description,
            "basis": basis,
            "kind": "preset",
        }
        out = Path(ctx.scratch_dir) / "kokoro" / "conditioning.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        ref = await ctx.write_artifact(
            out, "voice_conditioning", {"adapter_id": self.manifest.id}, role="conditioning", mime="application/json"
        )
        return VoicePrepareResult(conditioning=ref)
