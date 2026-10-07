"""Real media for mock engines (§37): FFmpeg, Pillow and numpy, deterministic given a seed.

Everything a mock writes is a real, playable file: PNG stills with a drawn face-like figure or a
labelled room, MP4 clips, WAV audio. Labels are burned in so a viewer can see which shot, take,
state and engine produced a frame. Nothing here pretends to be a model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import struct
import subprocess
import wave
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "TIMINGS_CHUNK",
    "FFmpegError",
    "draw_face_image",
    "draw_plate_image",
    "file_sha256",
    "font_file",
    "probe_duration",
    "read_wav",
    "read_wav_chunk",
    "render_avatar_clip",
    "render_gradient_clip",
    "render_text_clip",
    "run_ffmpeg",
    "seed_int",
    "synth_music",
    "synth_sfx",
    "synth_speech",
    "write_wav",
]

TIMINGS_CHUNK = b"ceTM"  # RIFF chunk carrying the mock TTS word timings (read by the mock aligner)


class FFmpegError(RuntimeError):
    pass


def seed_int(*parts: Any) -> int:
    """A stable 31-bit seed from any parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


@lru_cache(maxsize=1)
def font_file() -> str | None:
    """A TrueType font for burned labels (DejaVu Sans when installed)."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    fc = shutil.which("fc-match")
    if fc:
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv
                [fc, "-f", "%{file}", "DejaVu Sans"], capture_output=True, text=True, timeout=10, check=False
            )
            if out.returncode == 0 and out.stdout.strip():
                candidates.insert(0, out.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


def _pil_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = font_file()
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------- processes


async def run_ffmpeg(args: Sequence[str], *, timeout_s: float = 600.0) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise FFmpegError(f"ffmpeg timed out after {timeout_s} s") from None
    except asyncio.CancelledError:  # a cancelled task must not leave the subprocess running
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise FFmpegError(stderr.decode("utf-8", "replace")[-2000:] or f"ffmpeg exited with {process.returncode}")


async def probe_duration(path: Path) -> float:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise FFmpegError("ffprobe is not installed")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise FFmpegError(stderr.decode("utf-8", "replace")[-500:])
    return float(json.loads(stdout or b"{}").get("format", {}).get("duration", 0.0))


# ---------------------------------------------------------------------- stills


def _palette(seed: int) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 120, size=3)
    light = np.clip(base + rng.integers(60, 120, size=3), 0, 255)
    accent = rng.integers(140, 255, size=3)
    return tuple(int(x) for x in base), tuple(int(x) for x in light), tuple(int(x) for x in accent)  # type: ignore[return-value]


def _gradient(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    t = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    a = np.array(top, dtype=np.float32)[None, None, :]
    b = np.array(bottom, dtype=np.float32)[None, None, :]
    rows = (a * (1 - t) + b * t).astype(np.uint8)
    return Image.fromarray(np.repeat(rows, width, axis=1), "RGB")


def _label_block(draw: ImageDraw.ImageDraw, lines: Iterable[str], x: int, y: int, size: int) -> None:
    font = _pil_font(size)
    for line in lines:
        if not line:
            continue
        box = draw.textbbox((x, y), line, font=font)
        draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
        draw.text((x, y), line, fill=(255, 255, 255), font=font)
        y = int(box[3]) + 8


def _figure(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, scale: float, skin: tuple[int, int, int], expression: str
) -> None:
    head_w, head_h = 0.26 * scale, 0.34 * scale
    body_top = cy + head_h * 0.45
    draw.rounded_rectangle(
        (cx - 0.38 * scale, body_top, cx + 0.38 * scale, body_top + 0.7 * scale),
        radius=int(0.12 * scale),
        fill=(90, 90, 96),
    )
    draw.ellipse((cx - head_w / 2, cy - head_h / 2, cx + head_w / 2, cy + head_h / 2), fill=skin)
    eye_y = cy - head_h * 0.08
    for side in (-1, 1):
        ex = cx + side * head_w * 0.2
        draw.ellipse(
            (ex - 0.018 * scale, eye_y - 0.012 * scale, ex + 0.018 * scale, eye_y + 0.012 * scale), fill=(30, 30, 30)
        )
    mouth_y = cy + head_h * 0.22
    width = head_w * (0.36 if expression in ("smile", "amused", "happy") else 0.26)
    if expression in ("smile", "amused", "happy"):
        draw.arc(
            (cx - width / 2, mouth_y - 0.03 * scale, cx + width / 2, mouth_y + 0.03 * scale),
            10,
            170,
            fill=(120, 40, 40),
            width=3,
        )
    else:
        draw.line((cx - width / 2, mouth_y, cx + width / 2, mouth_y), fill=(120, 40, 40), width=3)


def draw_face_image(
    path: Path,
    *,
    width: int,
    height: int,
    seed: int,
    lines: Sequence[str],
    background: Path | None = None,
    expression: str = "neutral",
    figure: bool = True,
) -> None:
    """A face-like figure on a seeded gradient (or on `background`, e.g. a world plate) with labels;
    `figure=False` draws no figure (a faceless take, for honesty tests of real analyzers)."""
    base, light, accent = _palette(seed)
    if background is not None and background.is_file():
        image = Image.open(background).convert("RGB").resize((width, height))
    else:
        image = _gradient(width, height, light, base)
    draw = ImageDraw.Draw(image)
    skin = (int(200 + seed % 40), int(160 + seed % 50), int(130 + seed % 40))
    scale = min(width, height) * 1.25
    if figure:
        _figure(draw, width / 2, height * 0.46, scale, skin, expression)
    draw.rectangle((0, height - 8, width, height), fill=accent)
    # On a composed keyframe the plate's labels stay visible at the top; these go lower.
    _label_block(draw, lines, 12, int(height * 0.74) if background is not None else 12, max(12, height // 40))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=False)


def draw_plate_image(
    path: Path,
    *,
    width: int,
    height: int,
    seed: int,
    lines: Sequence[str],
    elements: Sequence[tuple[str, float, float]] = (),
) -> None:
    """A room-like gradient with element labels at their floor-plan positions (x, depth y in 0..1)."""
    base, light, accent = _palette(seed)
    image = _gradient(width, height, light, base)
    draw = ImageDraw.Draw(image)
    floor_y = int(height * 0.68)
    draw.rectangle((0, floor_y, width, height), fill=tuple(int(c * 0.7) for c in base))
    draw.line((0, floor_y, width, floor_y), fill=accent, width=2)
    font = _pil_font(max(11, height // 50))
    for label, x, y in elements:
        px = int(np.clip(x, 0.02, 0.98) * width)
        py = int(floor_y - (np.clip(y, 0.0, 1.0) * height * 0.45))
        draw.rectangle((px - 18, py - 18, px + 18, py + 18), outline=accent, width=2)
        draw.text((px - 18, py + 22), label, fill=(255, 255, 255), font=font)
    _label_block(draw, lines, 12, int(height * 0.16), max(12, height // 40))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=False)


# ---------------------------------------------------------------------- audio


def write_wav(path: Path, samples: np.ndarray, sample_rate: int, *, extra_chunk: bytes | None = None) -> None:
    """16-bit mono WAV; `extra_chunk` is appended as a `ceTM` RIFF chunk (ignored by players)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    if extra_chunk is not None:
        payload = extra_chunk + (b"\x00" if len(extra_chunk) % 2 else b"")
        with path.open("r+b") as handle:
            handle.seek(0, 2)
            handle.write(TIMINGS_CHUNK + struct.pack("<I", len(extra_chunk)) + payload)
            size = handle.tell() - 8
            handle.seek(4)
            handle.write(struct.pack("<I", size))


def read_wav_chunk(path: Path, chunk_id: bytes = TIMINGS_CHUNK) -> bytes | None:
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if cid == chunk_id:
            return data[pos + 8 : pos + 8 + size]
        pos += 8 + size + (size % 2)
    return None


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("only 16-bit WAV is supported by the mock readers")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def _envelope(n: int, sample_rate: int, attack_s: float, release_s: float) -> np.ndarray:
    env = np.ones(n, dtype=np.float32)
    a = min(n, max(1, int(attack_s * sample_rate)))
    r = min(n - a, max(1, int(release_s * sample_rate))) if n > a else 0
    env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    if r > 0:
        env[n - r :] = np.linspace(1.0, 0.0, r, dtype=np.float32)
    return env


def synth_speech(
    words: Sequence[str],
    *,
    wpm: float,
    seed: int,
    sample_rate: int = 48_000,
    rate: float = 1.0,
    energy: float = 0.6,
    pitch_variation: float = 0.5,
    f0: float | None = None,
    pitch_semitones: float = 0.0,
    pauses_ms: dict[int, int] | None = None,
    emphasis: Iterable[int] = (),
    lead_s: float = 0.12,
    tail_s: float = 0.18,
) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Voiced tone bursts, one per word, with an estimated duration per word (§37 TTS mock).

    Returns the samples and each word's (start_s, end_s). Pauses are inserted silence after the
    given word index; emphasized words are louder and slightly longer.
    """
    rng = np.random.default_rng(seed)
    base_f0 = (f0 if f0 is not None else float(110 + seed % 90)) * 2 ** (pitch_semitones / 12)
    per_word = 60.0 / max(40.0, wpm * max(0.3, rate))
    lengths = np.array([max(1, len(w.strip(".,!?;:…\"'"))) for w in words], dtype=np.float32)
    weights = np.clip(lengths / max(1.0, float(lengths.mean())), 0.6, 1.6)
    weights = weights / float(weights.mean())
    emphasized = set(emphasis)
    pauses = pauses_ms or {}
    chunks: list[np.ndarray] = [np.zeros(int(lead_s * sample_rate), dtype=np.float32)]
    t = lead_s
    timings: list[tuple[float, float]] = []
    gap = 0.045
    for index, weight in enumerate(weights):
        if index in emphasized and index > 0:  # a short breath before the stressed word
            chunks.append(np.zeros(int(0.08 * sample_rate), dtype=np.float32))
            t += 0.08
        duration = per_word * float(weight) * (1.12 if index in emphasized else 1.0)
        voiced = max(0.06, duration - gap)
        n = int(voiced * sample_rate)
        tt = np.arange(n, dtype=np.float32) / sample_rate
        drift = 1.0 + pitch_variation * 0.08 * math.sin(index * 1.7 + seed % 7)
        f = base_f0 * drift * (1.06 if index in emphasized else 1.0)
        wave_ = sum(np.sin(2 * math.pi * f * k * tt) / k for k in (1, 2, 3, 4))
        wave_ = wave_ * (0.6 + 0.4 * np.sin(2 * math.pi * 4.0 * tt))  # syllable-like modulation
        noise = rng.normal(0, 0.05, n).astype(np.float32)
        # emphasis: louder, longer, higher — strong enough to survive the mic compressor and the room
        gain = (0.18 + 0.25 * energy) * (2.0 if index in emphasized else 1.0)
        burst = (wave_ * 0.25 + noise) * gain * _envelope(n, sample_rate, 0.012, 0.03)
        chunks.append(burst.astype(np.float32))
        timings.append((round(t, 4), round(t + voiced, 4)))
        silence = gap + pauses.get(index, 0) / 1000.0
        chunks.append(np.zeros(int(silence * sample_rate), dtype=np.float32))
        t += voiced + silence
    chunks.append(np.zeros(int(tail_s * sample_rate), dtype=np.float32))
    return np.concatenate(chunks), timings


def synth_music(duration_s: float, *, bpm: int, seed: int, sample_rate: int = 48_000) -> np.ndarray:
    """An instrumental bed: soft chord pads changing every bar plus quiet beat clicks."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * sample_rate)
    t = np.arange(n, dtype=np.float32) / sample_rate
    beat = 60.0 / max(40, bpm)
    roots = [220.0, 196.0, 174.6, 246.9]
    order = rng.permutation(len(roots))
    out = np.zeros(n, dtype=np.float32)
    bar = beat * 4
    for i in range(int(duration_s / bar) + 1):
        start, end = int(i * bar * sample_rate), min(n, int((i + 1) * bar * sample_rate))
        if start >= n:
            break
        root = roots[order[i % len(roots)]]
        seg = t[start:end] - t[start]
        chord = sum(np.sin(2 * math.pi * root * r * seg) for r in (1.0, 1.25, 1.5))
        out[start:end] += (0.12 * chord * _envelope(end - start, sample_rate, 0.2, 0.3)).astype(np.float32)
    for k in range(int(duration_s / beat) + 1):
        s = int(k * beat * sample_rate)
        e = min(n, s + int(0.03 * sample_rate))
        if s < n:
            out[s:e] += (0.2 * np.exp(-np.linspace(0, 6, e - s)) * rng.normal(0, 1, e - s)).astype(np.float32)
    fade = _envelope(n, sample_rate, 0.3, 0.6)
    return np.clip(out * fade, -1.0, 1.0)


def synth_sfx(duration_s: float, *, seed: int, sample_rate: int = 48_000, description: str = "") -> np.ndarray:
    """Noise with an envelope: a rising-then-falling sweep for whooshes, a short hit otherwise."""
    rng = np.random.default_rng(seed)
    n = max(1, int(duration_s * sample_rate))
    noise = rng.normal(0, 1, n).astype(np.float32)
    kernel = np.ones(24, dtype=np.float32) / 24  # crude low-pass
    smooth = np.convolve(noise, kernel, mode="same")
    x = np.linspace(0, 1, n, dtype=np.float32)
    if "whoosh" in description.lower() or "swoosh" in description.lower():
        env = np.sin(np.pi * x) ** 2
    else:
        env = np.exp(-6 * x)
    return np.clip(0.6 * smooth * env, -1.0, 1.0)


# ---------------------------------------------------------------------- clips


def _escape_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _drawtext(textfile: Path, *, x: str, y: str, size: int, enable: str | None = None) -> str:
    font = font_file()
    parts = [
        f"textfile='{_escape_path(textfile)}'",
        f"x={x}",
        f"y={y}",
        f"fontsize={size}",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.55",
        "boxborderw=6",
    ]
    if font:
        parts.insert(0, f"fontfile='{_escape_path(Path(font))}'")
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def _even(value: int) -> int:
    return max(2, value - value % 2)


async def render_avatar_clip(
    keyframe: Path,
    audio: Path,
    out: Path,
    *,
    width: int,
    height: int,
    fps: float,
    lines: Sequence[str],
    timed_labels: Sequence[tuple[float, float, str]] = (),
    workdir: Path,
) -> None:
    """Loop the keyframe, overlay an audio-driven waveform at the mouth, burn labels (§37)."""
    width, height = _even(width), _even(height)
    workdir.mkdir(parents=True, exist_ok=True)
    head = workdir / "label_head.txt"
    head.write_text("\n".join(lines), encoding="utf-8")
    wave_w, wave_h = _even(int(width * 0.22)), _even(max(8, int(height * 0.035)))
    mouth_y = int(height * 0.46 + min(width, height) * 1.25 * 0.34 * 0.22 - wave_h / 2)
    size = max(12, height // 42)
    chain = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,format=yuv420p[bg]",
        f"[1:a]aformat=channel_layouts=mono,showwaves=s={wave_w}x{wave_h}:mode=cline:rate={fps:g}:colors=0x8a2a2a,format=yuva420p[wv]",
        f"[bg][wv]overlay=x=(W-w)/2:y={mouth_y}:shortest=1[v0]",
        f"[v0]{_drawtext(head, x='12', y='12', size=size)}[v1]",
    ]
    label = "v1"
    for index, (start, end, text) in enumerate(timed_labels):
        path = workdir / f"label_{index}.txt"
        path.write_text(text, encoding="utf-8")
        enable = f"between(t,{start:.3f},{end:.3f})"
        chain.append(f"[{label}]{_drawtext(path, x='12', y=f'h-{size * 3}', size=size, enable=enable)}[t{index}]")
        label = f"t{index}"
    await run_ffmpeg(
        [
            "-loop", "1", "-framerate", f"{fps:g}", "-i", str(keyframe),
            "-i", str(audio),
            "-filter_complex", ";".join(chain),
            "-map", f"[{label}]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
            "-r", f"{fps:g}", "-threads", "2",
            "-c:a", "aac", "-b:a", "96k", "-shortest",
            str(out),
        ]
    )  # fmt: skip


async def render_gradient_clip(
    out: Path,
    *,
    width: int,
    height: int,
    fps: float,
    duration_s: float,
    seed: int,
    lines: Sequence[str],
    first_frame: Path | None = None,
    workdir: Path,
) -> None:
    """A labelled moving gradient (B-roll mock), or the first frame held when one is given."""
    width, height = _even(width), _even(height)
    workdir.mkdir(parents=True, exist_ok=True)
    text = workdir / "label.txt"
    text.write_text("\n".join(lines), encoding="utf-8")
    base, light, accent = _palette(seed)
    size = max(12, height // 36)
    if first_frame is not None:
        inputs = ["-loop", "1", "-framerate", f"{fps:g}", "-t", f"{duration_s:.3f}", "-i", str(first_frame)]
        head = f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    else:
        c0 = "0x{:02x}{:02x}{:02x}".format(*light)
        c1 = "0x{:02x}{:02x}{:02x}".format(*accent)
        c2 = "0x{:02x}{:02x}{:02x}".format(*base)
        source = (
            f"gradients=s={width}x{height}:r={fps:g}:d={duration_s:.3f}"
            f":c0={c0}:c1={c1}:c2={c2}:nb_colors=3:speed=0.015:seed={seed}"
        )
        inputs = ["-f", "lavfi", "-i", source]
        head = "[0:v]setsar=1"
    chain = f"{head},format=yuv420p,{_drawtext(text, x='(w-text_w)/2', y='(h-text_h)/2', size=size)}[v]"
    await run_ffmpeg(
        [
            *inputs, "-filter_complex", chain, "-map", "[v]", "-t", f"{duration_s:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-r", f"{fps:g}",
            "-threads", "2", "-an", str(out),
        ]
    )  # fmt: skip


async def render_text_clip(
    out: Path, *, width: int, height: int, fps: float, duration_s: float, text: str, color: str, workdir: Path
) -> None:
    """A solid-color card with centred text (effects mocks: titles, disclosures, lower thirds)."""
    width, height = _even(width), _even(height)
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "card.txt"
    path.write_text(text, encoding="utf-8")
    size = max(14, height // 24)
    await run_ffmpeg(
        [
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps:g}:d={duration_s:.3f}",
            "-vf", f"format=yuv420p,{_drawtext(path, x='(w-text_w)/2', y='(h-text_h)/2', size=size)}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-an", str(out),
        ]
    )  # fmt: skip
