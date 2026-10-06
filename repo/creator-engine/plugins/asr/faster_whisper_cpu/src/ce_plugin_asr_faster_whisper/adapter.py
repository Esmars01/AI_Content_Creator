"""faster-whisper on CPU.

`asr.transcribe` never sees the script (no prompt, no hints): verification must hear what was
actually said. `asr.align` transcribes with word timestamps and maps the heard words onto the
script's spoken words (`AlignRequest.spoken_words`, from the language normalizer) with a global
alignment over folded words (exact match, fuzzy match by character similarity, or a gap). Script
words without a heard counterpart get times interpolated between their neighbors, weighted by
length. Every canonical word gets a time span: the union of its spoken words' spans.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
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
from ce_plugin_kit.align import align_words, fold, to_canonical

__all__ = ["FasterWhisperCpu", "align_words", "fold"]


def decode_16k(path: Path) -> np.ndarray:
    """Mono float32 PCM at 16 kHz through FFmpeg (faster-whisper's PyAV decoder is not used: it
    tracks PyAV's API closely and broke across releases)."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is not installed")
    raw = subprocess.run(  # noqa: S603 - fixed argv, the path is an artifact the worker downloaded
        [binary, "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
        check=True,
        timeout=300,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


class FasterWhisperCpu(ASREngine):
    seconds_per_unit = 0.6

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        path = Path(ctx.model_cache_dir) / str(self.config["model_dir"])
        if not (path / "model.bin").is_file():
            raise FileNotFoundError(f"{path} is missing: run `make fetch-cpu-assets`")
        self._model = await asyncio.to_thread(self._open, path)

    def _open(self, path: Path) -> Any:
        from faster_whisper import WhisperModel

        return WhisperModel(
            str(path),
            device="cpu",
            compute_type=str(self.config["compute_type"]),
            cpu_threads=max(1, min(4, os.cpu_count() or 1)),
            local_files_only=True,
        )

    async def unload(self) -> None:
        self._model = None
        await super().unload()

    def _transcribe(
        self, audio: Path, language: str | None
    ) -> tuple[list[tuple[str, float, float, float]], str, float]:
        # Silence around the clip: synthesized speech starts on its first phoneme, and Whisper
        # mishears an abrupt onset ("But here's" → "about it here's"); word times are shifted back
        # and clamped to the clip.
        pad = float(self.config.get("pad_s", 0.5))
        pcm = decode_16k(audio)
        duration = pcm.size / 16000.0
        silence = np.zeros(int(16000 * pad), dtype=np.float32)
        segments, info = self._model.transcribe(
            np.concatenate([silence, pcm, silence]),
            language=language.split("-")[0] if language else None,
            beam_size=int(self.config["beam_size"]),
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
            temperature=0.0,
        )
        words: list[tuple[str, float, float, float]] = []
        for segment in segments:
            for w in segment.words or []:
                start = min(max(0.0, float(w.start) - pad), duration)
                end = min(max(start, float(w.end) - pad), duration)
                words.append((w.word.strip(), round(start, 3), round(end, 3), float(w.probability)))
        return words, str(info.language), duration

    async def _run(self, audio: Path, language: str | None) -> tuple[list[tuple[str, float, float, float]], str, float]:
        if self._model is None:
            raise RuntimeError("faster_whisper_cpu is not loaded")
        async with self._lock:
            return await asyncio.to_thread(self._transcribe, audio, language)

    async def run_asr_transcribe(self, request: TranscribeRequest, ctx: RunContext) -> TranscribeResult:
        words, language, _ = await self._run(await ctx.read_artifact(request.audio), request.language)
        text = " ".join(w for w, *_ in words if w)
        aligned = [
            AlignedWord(index=i, word=w, start_s=max(0.0, s), end_s=max(0.0, e), confidence=min(1.0, max(0.0, p)))
            for i, (w, s, e, p) in enumerate(words)
        ]
        return TranscribeResult(text=text, words=aligned, language=request.language or language)

    async def run_asr_align(self, request: AlignRequest, ctx: RunContext) -> AlignResult:
        words, _, duration = await self._run(await ctx.read_artifact(request.audio), request.language)
        out = to_canonical(request, [(w, s, e) for w, s, e, _ in words], duration)
        return AlignResult(words=out, precision="coarse")

    async def run_asr_lid(self, request: LidRequest, ctx: RunContext) -> LidResult:
        if self._model is None:
            raise RuntimeError("faster_whisper_cpu is not loaded")
        audio = await ctx.read_artifact(request.audio)

        def detect() -> tuple[str, float]:
            language, probability, _ = self._model.detect_language(decode_16k(audio))
            return str(language), float(probability)

        async with self._lock:
            language, probability = await asyncio.to_thread(detect)
        return LidResult(language=language, confidence=min(1.0, max(0.0, probability)))
