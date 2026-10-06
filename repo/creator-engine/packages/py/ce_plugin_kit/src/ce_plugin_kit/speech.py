"""Speech helpers shared by real TTS engines (§21).

- Pauses are always inserted silence (§21): the spoken words are split after each paused word,
  the chunks are synthesized separately and joined with exact silence.
- Inline non-verbal tags (engines that support them) are placed after their word.
- Rate changes an engine cannot make natively are a time-stretch within ±10% (§21 "post-processing
  within ±10% when the engine lacks them").
- Voice conditioning (`voice.prepare`, §21) travels as one zip bundle: `conditioning.json` plus the
  engine's files (the trimmed reference clip, cached prompt tensors), keyed by voice version,
  adapter and revision.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import ArtifactRef, RunContext

from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import ffmpeg, read_audio, resample_wav, write_wav

__all__ = [
    "Chunk",
    "SpeechAdapter",
    "assemble",
    "pick_reference",
    "read_bundle",
    "split_at_pauses",
    "stretch_factor",
    "time_stretch",
    "write_bundle",
]


@dataclass(frozen=True)
class Chunk:
    text: str
    silence_ms: int  # silence inserted after the chunk


def split_at_pauses(
    words: Sequence[str], pauses: Mapping[int, int], tags: Mapping[int, str] | None = None
) -> list[Chunk]:
    """Chunks of the spoken words, split after each paused word index; `tags` (word index → inline
    tag such as `[laugh]`) are appended after their word."""
    tags = tags or {}
    out: list[Chunk] = []
    current: list[str] = []
    for index, word in enumerate(words):
        if word:
            current.append(word)
        if index in tags:
            current.append(tags[index])
        if index in pauses and current:
            out.append(Chunk(" ".join(current), int(pauses[index])))
            current = []
    if current:
        out.append(Chunk(" ".join(current), 0))
    return out


def assemble(parts: Sequence[tuple[np.ndarray, int]], sample_rate: int) -> np.ndarray:
    """Concatenates (samples, silence_ms after) pieces at one sample rate."""
    pieces: list[np.ndarray] = []
    for samples, silence_ms in parts:
        pieces.append(np.asarray(samples, dtype=np.float32).reshape(-1))
        if silence_ms:
            pieces.append(np.zeros(round(sample_rate * silence_ms / 1000.0), dtype=np.float32))
    return np.concatenate(pieces) if pieces else np.zeros(sample_rate // 4, dtype=np.float32)


def stretch_factor(rate: float, limit: float = 0.10) -> tuple[float, bool]:
    """A ProsodyPlan rate → a time-stretch tempo within ±`limit`, and whether it had to be clamped."""
    clamped = min(1.0 + limit, max(1.0 - limit, float(rate)))
    return round(clamped, 4), abs(clamped - float(rate)) > 1e-6


def time_stretch(samples: np.ndarray, sample_rate: int, tempo: float, workdir: Path) -> np.ndarray:
    """Changes the speaking rate without changing pitch (FFmpeg `atempo`)."""
    if abs(tempo - 1.0) < 1e-4:
        return samples
    src, dst = workdir / "stretch_in.wav", workdir / "stretch_out.wav"
    write_wav(src, samples, sample_rate)
    ffmpeg("-i", str(src), "-filter:a", f"atempo={tempo:.4f}", "-ar", str(sample_rate), str(dst))
    return read_audio(dst, sample_rate)


def write_bundle(path: Path, meta: Mapping[str, Any], files: Mapping[str, Path]) -> Path:
    """A deterministic zip (fixed timestamps, sorted entries) so identical conditioning hashes identically."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        info = zipfile.ZipInfo("conditioning.json", date_time=(2020, 1, 1, 0, 0, 0))
        bundle.writestr(info, json.dumps(dict(meta), sort_keys=True, ensure_ascii=False))
        for name in sorted(files):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            bundle.writestr(entry, Path(files[name]).read_bytes())
    return path


def read_bundle(path: Path, dest: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        meta = json.loads(bundle.read("conditioning.json").decode("utf-8"))
        files: dict[str, Path] = {}
        for name in names:
            if name == "conditioning.json" or name.endswith("/") or ".." in Path(name).parts:
                continue
            target = dest / Path(name).name
            with bundle.open(name) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            files[name] = target
    return meta, files


def pick_reference(references: Sequence[Any], language: str | None) -> Any | None:
    """The reference recording in the request's language (primary subtag), else the first one."""
    if not references:
        return None
    primary = (language or "").split("-")[0].lower()
    for reference in references:
        if reference.language.split("-")[0].lower() == primary:
            return reference
    return references[0]


# ---------------------------------------------------------------------- the TTS adapter base


class SpeechAdapter(EngineAdapter):
    """Base of real TTS engines. A backend implements:

    - `synthesize(text, options) -> np.ndarray` — float samples at `native_rate`;
    - `prepare(reference_wav, transcript, workdir) -> dict[str, Path]` — the engine's cached
      conditioning files for a voice (may be empty: the reference clip itself is always bundled);
    - optionally `design(description, sample_text, language, seed) -> np.ndarray` (`voice.design`).

    Subclasses set `native_rate`, `translator` and `synth_options()`."""

    native_rate = 24_000
    translator: Any = None
    max_reference_s = 10.0

    def synth_options(
        self, request: m.TTSRequest, engine: dict[str, Any], voice: dict[str, Any], seed: int
    ) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def translate(self, request: m.TTSRequest) -> m.TTSRequest:
        if request.behavior is not None and not request.engine and self.translator is not None:
            translated = self.translator.translate(request.behavior, request)
            assert isinstance(translated, m.TTSRequest)
            return translated
        return request

    async def voice(self, request: m.TTSRequest, ctx: RunContext, work: Path) -> dict[str, Any]:
        """The prepared conditioning (`voice.prepare`) as {"meta": …, "files": {name: path}}, or an
        empty voice (the engine's default speaker) when the voice has no reference yet."""
        if request.voice.conditioning is None:
            return {"meta": {}, "files": {}}
        bundle = await ctx.read_artifact(request.voice.conditioning)
        meta, files = read_bundle(bundle, work / "voice")
        if meta.get("adapter_id") not in (None, self.manifest.id):
            raise ValueError(f"{self.manifest.id}: conditioning was prepared for {meta.get('adapter_id')}")
        return {"meta": meta, "files": files}

    async def run_voice_tts(self, request: m.TTSRequest, ctx: RunContext) -> m.TTSResult:
        backend = self.require_backend()
        request = self.translate(request)
        engine = dict(request.engine or {})
        work = self.scratch(ctx, f"tts-{request.labels.get('segment', 'seg')}")
        voice = await self.voice(request, ctx, work)
        words = list(request.spoken) if request.spoken else (request.words or request.text.split())
        pauses = {int(k): int(v) for k, v in (engine.get("pauses_ms") or {}).items()}
        tags = {int(k): str(v) for k, v in (engine.get("tags") or {}).items()}
        parts: list[tuple[np.ndarray, int]] = []
        for index, chunk in enumerate(split_at_pauses(words, pauses, tags)):
            options = self.synth_options(request, engine, voice, seed=(int(ctx.seed) + index) % (2**31))
            samples = await self.call(ctx, backend.synthesize, chunk.text, options)
            parts.append((np.asarray(samples, dtype=np.float32).reshape(-1), chunk.silence_ms))
        audio = assemble(parts, self.native_rate)
        audio = time_stretch(audio, self.native_rate, float(engine.get("tempo", 1.0)), work)
        native = write_wav(work / "native.wav", audio, self.native_rate)
        out = resample_wav(native, work / "speech.wav", request.sample_rate)
        duration = round(len(audio) / self.native_rate, 4)
        ref = await ctx.write_artifact(
            out,
            "audio",
            {
                "adapter_id": self.manifest.id,
                "duration_s": duration,
                "tempo": engine.get("tempo", 1.0),
                "chunks": len(parts),
            },
            role="audio",
            mime="audio/wav",
        )
        return m.TTSResult(audio=ref, duration_s=duration, sample_rate=request.sample_rate, word_timings=None)

    async def run_voice_clone_prepare(self, request: m.VoicePrepareRequest, ctx: RunContext) -> m.VoicePrepareResult:
        backend = self.require_backend()
        work = self.scratch(ctx, "prepare")
        reference = pick_reference(request.references, None)
        meta: dict[str, Any] = {
            "engine": self.manifest.id,
            "adapter_id": self.manifest.id,
            "description": request.description,
        }
        files: dict[str, Path] = {}
        if reference is not None:
            source = await ctx.read_artifact(reference.audio)
            clip = work / "reference.wav"
            ffmpeg(
                "-i", str(source), "-t", f"{self.max_reference_s:.2f}", "-ac", "1", "-ar", str(self.native_rate),
                "-c:a", "pcm_s16le", str(clip),
            )  # fmt: skip
            files["reference.wav"] = clip
            meta.update(
                {
                    "transcript": reference.transcript,
                    "language": reference.language,
                    "reference_sha256": reference.audio.sha256,
                }
            )
            extra = await self.call(ctx, backend.prepare, clip, reference.transcript, work)
            files.update({name: Path(path) for name, path in (extra or {}).items()})
        bundle = write_bundle(work / "conditioning.zip", meta, files)
        ref = await ctx.write_artifact(
            bundle, "voice_conditioning", {"adapter_id": self.manifest.id}, role="conditioning", mime="application/zip"
        )
        return m.VoicePrepareResult(conditioning=ref)

    async def run_voice_design(self, request: m.VoiceDesignRequest, ctx: RunContext) -> m.VoiceDesignResult:
        backend = self.require_backend()
        work = self.scratch(ctx, "design")
        candidates: list[ArtifactRef] = []
        for index in range(request.count):
            seed = (int(ctx.seed) + index) % (2**31)
            samples = await self.call(
                ctx, backend.design, request.description, request.sample_text, request.language, seed
            )
            native = write_wav(work / f"candidate_{index}.wav", np.asarray(samples, dtype=np.float32), self.native_rate)
            out = resample_wav(native, work / f"candidate_{index}_48k.wav", 48_000)
            candidates.append(
                await ctx.write_artifact(
                    out,
                    "audio",
                    {"adapter_id": self.manifest.id, "candidate": index, "seed": seed},
                    role="candidate",
                    mime="audio/wav",
                )
            )
        return m.VoiceDesignResult(candidates=candidates)
