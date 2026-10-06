"""Audio assembly and the mix (§27): dialogue stem, ducked music beds, SFX, two-pass loudnorm.

Decoding and encoding go through FFmpeg; the arithmetic (placement, gain, the ducking envelope)
is numpy on float32 PCM at 48 kHz, so the mix is deterministic and the ducking depth is exact
(`sidechaincompress` is not used, §27).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ce_render.ffmpeg import FFmpegError, Loudness, measure_loudness, run_ffmpeg

__all__ = [
    "SAMPLE_RATE",
    "MixInput",
    "MixResult",
    "assemble",
    "decode",
    "duck_envelope",
    "encode_wav",
    "loudnorm_two_pass",
    "mix",
]

SAMPLE_RATE = 48_000


async def decode(path: Path, *, channels: int = 2, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Float32 PCM, shape (samples, channels)."""
    raw = await run_ffmpeg(["-i", str(path), "-vn", "-f", "f32le", "-ac", str(channels), "-ar", str(sample_rate), "-"])
    data = np.frombuffer(raw, dtype=np.float32)
    return data.reshape(-1, channels).copy()


async def encode_wav(samples: np.ndarray, out: Path, *, sample_rate: int = SAMPLE_RATE) -> Path:
    """16-bit PCM WAV (dithering is unnecessary for the levels involved)."""
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    pcm = np.clip(samples, -1.0, 1.0).astype(np.float32).tobytes()
    out.parent.mkdir(parents=True, exist_ok=True)
    await run_ffmpeg(
        ["-f", "f32le", "-ac", str(channels), "-ar", str(sample_rate), "-i", "-", "-c:a", "pcm_s16le", str(out)],
        input_bytes=pcm,
    )
    return out


def _place(target: np.ndarray, clip: np.ndarray, at_s: float, sample_rate: int = SAMPLE_RATE) -> None:
    start = round(at_s * sample_rate)
    if start < 0:
        clip = clip[-start:]
        start = 0
    end = min(target.shape[0], start + clip.shape[0])
    if end > start:
        target[start:end] += clip[: end - start]


async def assemble(
    pieces: Sequence[tuple[Path, float, float, float]], duration_s: float, out: Path, *, channels: int = 1
) -> Path:
    """Places `(path, from_s, to_s, at_s)` pieces on silence of `duration_s` (talking-shot audio)."""
    total = np.zeros((round(duration_s * SAMPLE_RATE), channels), dtype=np.float32)
    for path, from_s, to_s, at_s in pieces:
        clip = await decode(path, channels=channels)
        a, b = round(from_s * SAMPLE_RATE), round(to_s * SAMPLE_RATE)
        _place(total, clip[a:b], at_s)
    return await encode_wav(total if channels > 1 else total[:, 0], out)


def duck_envelope(
    n_samples: int,
    intervals: Sequence[tuple[float, float]],
    *,
    duck_db: float,
    attack_s: float = 0.08,
    release_s: float = 0.3,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """Gain per sample: `duck_db` inside speech intervals, 0 dB outside, with linear ramps (in
    dB) of `attack_s` before each interval and `release_s` after it."""
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    depth = np.zeros(n_samples, dtype=np.float64)  # 0 → no duck, 1 → full duck
    for a, b in intervals:
        inside = (t >= a) & (t <= b)
        depth[inside] = 1.0
        if attack_s > 0:
            ramp = (t >= a - attack_s) & (t < a)
            depth[ramp] = np.maximum(depth[ramp], (t[ramp] - (a - attack_s)) / attack_s)
        if release_s > 0:
            ramp = (t > b) & (t <= b + release_s)
            depth[ramp] = np.maximum(depth[ramp], 1.0 - (t[ramp] - b) / release_s)
    return (10.0 ** (duck_db * depth / 20.0)).astype(np.float32)


def _fade(clip: np.ndarray, seconds: float) -> None:
    n = min(clip.shape[0] // 2, round(seconds * SAMPLE_RATE))
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
        clip[:n] *= ramp
        clip[-n:] *= ramp[::-1]


@dataclass(frozen=True)
class MixInput:
    dialogue: Sequence[tuple[Path, float]]  # (segment audio, timeline offset)
    music: Sequence[tuple[Path, float, float, float]]  # (bed, start_s, end_s, duck_db)
    sfx: Sequence[tuple[Path, float, float]]  # (sound, at_s, gain_db)
    speech: Sequence[tuple[float, float]]  # timeline intervals for ducking
    total_s: float
    beds: Sequence[tuple[Path, float, float]] = ()  # (room tone / ambience, start_s, end_s) per scene
    integrated_lufs: float = -14.0
    true_peak_dbtp: float = -1.0
    lra: float = 11.0


@dataclass(frozen=True)
class MixResult:
    path: Path
    measured: Loudness
    premix: Loudness | None
    normalization: str


_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


async def _loudnorm_measure(path: Path, target: str) -> dict[str, str]:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(path),
        "-af",
        f"{target}:print_format=json",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(process.communicate(), 300)
    match = _JSON.search(err.decode("utf-8", "replace"))
    if match is None:
        raise FFmpegError("loudnorm did not report measurements")
    data: dict[str, str] = json.loads(match.group(0))
    return data


async def _ffmpeg_filter(src: Path, out: Path, graph: str) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(
        binary, "-hide_banner", "-nostdin", "-y", "-i", str(src), "-af", graph, "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s24le", str(out), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    _, err = await asyncio.wait_for(process.communicate(), 300)
    if process.returncode != 0:
        raise FFmpegError("audio filter failed: " + err.decode("utf-8", "replace")[-400:])


async def loudnorm_two_pass(src: Path, out: Path, *, i: float, tp: float, lra: float) -> str:
    """Pass 1 measures, pass 2 applies in linear mode with the measured values (§27). Returns the
    normalization type (`linear`, `limited+linear`, or `dynamic` if loudnorm still had to fall back).

    When the gain linear mode needs would push the true peak over `tp`, loudnorm would switch to its
    dynamic mode, an AGC that flattens word-level dynamics (emphasis, softer asides). Instead the
    gain is applied first with a delay-compensated look-ahead limiter on the peaks (4× oversampled,
    so true peaks are caught), and the limited premix is then normalized linearly."""
    target = f"loudnorm=I={i}:TP={tp}:LRA={lra}"
    measured = await _loudnorm_measure(src, target)
    limited = False
    gain = i - float(measured["input_i"])
    if float(measured["input_tp"]) + gain > tp:
        ceiling = 10 ** ((tp - 0.5) / 20.0)
        staged = out.with_name(out.stem + ".limited.wav")
        await _ffmpeg_filter(
            src,
            staged,
            f"volume={gain:.3f}dB,aresample={SAMPLE_RATE * 4},"
            f"alimiter=limit={ceiling:.5f}:attack=1:release=60:level=false:latency=true,aresample={SAMPLE_RATE}",
        )
        src, limited = staged, True
        measured = await _loudnorm_measure(src, target)
    second = (
        f"{target}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true:print_format=json"
    )
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-af",
        second,
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(out),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(process.communicate(), 300)
    if process.returncode != 0:
        raise FFmpegError("loudnorm pass 2 failed: " + err.decode("utf-8", "replace")[-400:])
    report = _JSON.search(err.decode("utf-8", "replace"))
    kind = str(json.loads(report.group(0)).get("normalization_type", "unknown")) if report else "unknown"
    if limited:
        src.unlink(missing_ok=True)
        return f"limited+{kind}"
    return kind


async def mix(inputs: MixInput, workdir: Path) -> MixResult:
    """Dialogue + ducked music + SFX + room-tone beds → premix → two-pass loudnorm → verified (§27
    steps 1–4). Room acoustics and mic processing are applied upstream (`audio.room`)."""
    workdir.mkdir(parents=True, exist_ok=True)
    n = round(inputs.total_s * SAMPLE_RATE)
    stem = np.zeros((n, 2), dtype=np.float32)
    for path, offset in inputs.dialogue:
        _place(stem, await decode(path), offset)
    for path, start, end, duck_db in inputs.music:
        bed = await decode(path)
        length = max(0, round((end - start) * SAMPLE_RATE))
        if bed.shape[0] < length:  # loop a short bed to the cue length
            reps = int(np.ceil(length / max(bed.shape[0], 1)))
            bed = np.tile(bed, (reps, 1))
        bed = bed[:length].copy()
        _fade(bed, 0.3)
        local = [(max(0.0, a - start), b - start) for a, b in inputs.speech if b > start and a < end]
        bed *= duck_envelope(bed.shape[0], local, duck_db=duck_db)[:, None]
        _place(stem, bed, start)
    for path, at, gain_db in inputs.sfx:
        sound = await decode(path) * np.float32(10.0 ** (gain_db / 20.0))
        _place(stem, sound, at)
    for path, start, end in inputs.beds:  # room tone: unducked, already at the world's noise floor
        bed = await decode(path)
        length = max(0, round((end - start) * SAMPLE_RATE))
        if bed.shape[0] < length:
            bed = np.tile(bed, (int(np.ceil(length / max(bed.shape[0], 1))), 1))
        bed = bed[:length].copy()
        _fade(bed, 0.2)
        _place(stem, bed, start)
    premix = await encode_wav(stem, workdir / "premix.wav")
    before = await measure_loudness(premix) if np.any(stem) else None
    out = workdir / "mix.wav"
    if before is None:  # silence cannot be normalized
        shutil.copyfile(premix, out)
        kind = "silent"
    else:
        kind = await loudnorm_two_pass(premix, out, i=inputs.integrated_lufs, tp=inputs.true_peak_dbtp, lra=inputs.lra)
    return MixResult(out, await measure_loudness(out), before, kind)
