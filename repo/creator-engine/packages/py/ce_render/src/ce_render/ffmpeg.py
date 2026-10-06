"""FFmpeg and ffprobe processes (ADR 0017): argv lists only (never a shell), bounded time, errors
carry the tail of stderr. Fonts come from the bundled fallback chain (`font_file`)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "FFmpegError",
    "Loudness",
    "MediaInfo",
    "escape_filter_path",
    "escape_text",
    "font_file",
    "measure_loudness",
    "probe",
    "run_ffmpeg",
]


class FFmpegError(RuntimeError):
    """FFmpeg failed or is missing."""


@lru_cache(maxsize=1)
def font_file() -> str:
    """The bundled Noto Sans (assets/fonts), else DejaVu Sans (installed in every image) or the
    first fontconfig match."""
    from ce_render.fonts import fonts_dir

    bundled = fonts_dir() / "NotoSans-Regular.ttf"
    if bundled.is_file():
        return str(bundled)
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
                candidates.append(out.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    for path in candidates:
        if Path(path).is_file():
            return path
    raise FFmpegError("no TrueType font found (install fonts-dejavu)")


def escape_filter_path(path: Path | str) -> str:
    """A path as a filter option value (`ass=…`, `fontfile=…`)."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def escape_text(text: str) -> str:
    """Text for `drawtext=text='…'` (quoted). Text is data: nothing in it can add filters."""
    return (
        text.replace("\\", "\\\\\\\\")
        .replace("'", "\u2019")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


async def run_ffmpeg(args: Sequence[str], *, timeout_s: float = 900.0, input_bytes: bytes | None = None) -> bytes:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    head = ["-hide_banner", "-loglevel", "error", "-y", *(["-nostdin"] if input_bytes is None else [])]
    process = await asyncio.create_subprocess_exec(
        binary,
        *head,
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(input_bytes), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise FFmpegError(f"ffmpeg timed out after {timeout_s:.0f}s") from None
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-12:]
        raise FFmpegError("ffmpeg failed: " + " | ".join(tail))
    return out


async def _run(binary_name: str, args: Sequence[str], timeout_s: float = 120.0) -> tuple[bytes, bytes]:
    binary = shutil.which(binary_name)
    if binary is None:
        raise FFmpegError(f"{binary_name} is not installed")
    process = await asyncio.create_subprocess_exec(
        binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise FFmpegError(f"{binary_name} timed out") from None
    except asyncio.CancelledError:  # a cancelled task must not leave the subprocess running
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise FFmpegError(f"{binary_name} failed: {err.decode('utf-8', 'replace')[-500:]}")
    return out, err


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    pix_fmt: str | None = None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "0"):
        return None
    num, _, den = value.partition("/")
    return round(float(num) / float(den or 1), 3)


async def probe(path: Path) -> MediaInfo:
    out, _ = await _run("ffprobe", ["-v", "error", "-show_entries", "format=duration:stream", "-of", "json", str(path)])
    data = json.loads(out or b"{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (video or audio or {}).get("duration") or 0.0)
    return MediaInfo(
        duration_s=round(duration, 4),
        width=int(video["width"]) if video else None,
        height=int(video["height"]) if video else None,
        fps=_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        pix_fmt=video.get("pix_fmt") if video else None,
    )


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float
    true_peak_dbtp: float
    lra: float


_EBU = re.compile(r"I:\s+(-?[\d.]+|-inf) LUFS.*?LRA:\s+(-?[\d.]+) LU.*?Peak:\s+(-?[\d.]+|-inf) dBFS", re.S)


async def measure_loudness(path: Path) -> Loudness:
    """Integrated loudness and true peak with `ebur128=peak=true` (§27 verification)."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-i",
        str(path),
        "-filter_complex",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(process.communicate(), 300)
    text = err.decode("utf-8", "replace")
    summary = text[text.rfind("Summary:") :] if "Summary:" in text else text
    match = _EBU.search(summary)
    if match is None:
        raise FFmpegError("could not read the ebur128 summary")

    def num(value: str) -> float:
        return -120.0 if value == "-inf" else float(value)

    return Loudness(num(match.group(1)), num(match.group(3)), float(match.group(2)))
