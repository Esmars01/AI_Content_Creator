"""Media helpers shared by real engines: FFmpeg probing, WAV I/O, frames → MP4, muxing, frame
extraction. Everything goes through the `ffmpeg`/`ffprobe` binaries (present in every worker image)
and NumPy; nothing here imports torch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "MediaInfo",
    "concat_videos",
    "conform_clip",
    "extract_frame",
    "extract_frames",
    "extract_last_frames",
    "ffmpeg",
    "frames_dir_to_mp4",
    "frames_to_mp4",
    "iter_video_frames",
    "plan_size",
    "probe",
    "read_audio",
    "resample_wav",
    "resize_image",
    "snap",
    "still_to_video",
    "trim_or_pad_audio",
    "video_with_audio",
    "write_wav",
]


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"{name} is required by the media helpers")
    return found


def ffmpeg(*args: str, timeout: float = 600.0, stdin: bytes | None = None) -> None:
    subprocess.run(  # noqa: S603 - fixed binary, argv list
        [_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        timeout=timeout,
        input=stdin,
    )


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    has_video: bool = False
    has_audio: bool = False
    sample_rate: int = 0
    channels: int = 0


def _rate(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    num, _, den = value.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: Path | str) -> MediaInfo:
    out = subprocess.run(  # noqa: S603 - fixed binary, argv list
        [_binary("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    data: dict[str, Any] = json.loads(out)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0.0)
    if video is not None and not duration:
        duration = float(video.get("duration") or 0.0)
    return MediaInfo(
        duration_s=duration,
        width=int(video.get("width", 0)) if video else 0,
        height=int(video.get("height", 0)) if video else 0,
        fps=_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else 0.0,
        frames=int(video.get("nb_frames") or 0) if video else 0,
        has_video=video is not None,
        has_audio=audio is not None,
        sample_rate=int(audio.get("sample_rate", 0)) if audio else 0,
        channels=int(audio.get("channels", 0)) if audio else 0,
    )


def snap(value: float, multiple: int, *, minimum: int | None = None) -> int:
    """Rounds to the nearest multiple (models need dimensions divisible by 8, 16, 32, …)."""
    snapped = round(value / multiple) * multiple
    floor = minimum if minimum is not None else multiple
    return max(floor, snapped)


# ---------------------------------------------------------------------- audio


def write_wav(path: Path | str, samples: np.ndarray, sample_rate: int) -> Path:
    """Float samples in [-1, 1] (mono `(n,)` or `(n, channels)`) → 16-bit PCM WAV."""
    path = Path(path)
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(pcm.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return path


def read_audio(path: Path | str, sample_rate: int, *, channels: int = 1) -> np.ndarray:
    """Any audio (or a video's audio) → float32 samples at `sample_rate` (mono by default)."""
    proc = subprocess.run(  # noqa: S603 - fixed binary, argv list
        [
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-f", "f32le", "-acodec", "pcm_f32le", "-ac", str(channels), "-ar", str(sample_rate), "-",
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )  # fmt: skip
    samples = np.frombuffer(proc.stdout, dtype="<f4").copy()
    return samples.reshape(-1, channels) if channels > 1 else samples


def resample_wav(src: Path | str, dst: Path | str, sample_rate: int, *, channels: int = 1) -> Path:
    ffmpeg("-i", str(src), "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst))
    return Path(dst)


def trim_or_pad_audio(src: Path | str, dst: Path | str, duration_s: float, sample_rate: int) -> Path:
    """Exactly `duration_s` long: trimmed, or padded with silence (engines that need fixed lengths)."""
    ffmpeg(
        "-i", str(src), "-af", f"apad=whole_dur={duration_s:.4f}", "-t", f"{duration_s:.4f}",
        "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst),
    )  # fmt: skip
    return Path(dst)


# ---------------------------------------------------------------------- video


def frames_to_mp4(
    frames: Iterable[np.ndarray] | np.ndarray,
    path: Path | str,
    fps: float,
    *,
    audio: Path | str | None = None,
    crf: int = 16,
) -> Path:
    """uint8 RGB frames (H, W, 3) → H.264 MP4 (yuv420p), optionally muxed with an audio file."""
    path = Path(path)
    iterator = iter(frames)
    first = next(iterator, None)
    if first is None:
        raise ValueError("no frames to encode")
    height, width = int(first.shape[0]), int(first.shape[1])
    args = [
        _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{fps:g}", "-i", "-",
    ]  # fmt: skip
    if audio is not None:
        args += ["-i", str(audio), "-c:a", "aac", "-b:a", "192k", "-shortest"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE)  # noqa: S603 - fixed binary, argv list
    assert proc.stdin is not None
    try:
        for frame in (first, *iterator):
            proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
    finally:
        proc.stdin.close()
    if proc.wait(timeout=3600) != 0:
        raise RuntimeError(f"ffmpeg failed to encode {path.name}")
    return path


def video_with_audio(video: Path | str, audio: Path | str, dst: Path | str) -> Path:
    """Replaces a video's audio with `audio` (video stream copied, cut to the shorter of the two)."""
    ffmpeg(
        "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dst),
    )  # fmt: skip
    return Path(dst)


def still_to_video(
    image: Path | str, dst: Path | str, *, duration_s: float, fps: float, width: int, height: int
) -> Path:
    ffmpeg(
        "-loop", "1", "-i", str(image), "-t", f"{duration_s:.4f}", "-r", f"{fps:g}",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(dst),
    )  # fmt: skip
    return Path(dst)


def extract_frame(video: Path | str, dst: Path | str, *, at_s: float = 0.0) -> Path:
    ffmpeg("-ss", f"{max(at_s, 0.0):.4f}", "-i", str(video), "-frames:v", "1", str(dst))
    return Path(dst)


def iter_video_frames(path: Path | str, *, chunk: int = 64) -> Iterator[np.ndarray]:
    """Decoded RGB frames in chunks of up to `chunk` (`(n, h, w, 3)` uint8), streamed from FFmpeg
    so long clips never sit in memory or on disk as images."""
    info = probe(path)
    if not info.has_video or info.width <= 0 or info.height <= 0:
        raise ValueError(f"{path} has no video stream")
    frame_bytes = info.width * info.height * 3
    proc = subprocess.Popen(  # noqa: S603 - fixed binary, argv list
        [
            _binary("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(frame_bytes * chunk)
            if not data:
                break
            n = len(data) // frame_bytes
            if n:
                yield np.frombuffer(data[: n * frame_bytes], dtype=np.uint8).reshape(n, info.height, info.width, 3)
            if len(data) < frame_bytes * chunk:
                break
    finally:
        proc.stdout.close()
        proc.wait(timeout=60)


def extract_frames(video: Path | str, out_dir: Path | str, *, fps: float | None = None) -> list[Path]:
    """Every frame as an RGB PNG (`%08d.png`), optionally resampled to `fps` first."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(video)]
    if fps:
        args += ["-vf", f"fps={fps:g}"]
    ffmpeg(*args, "-vsync", "0", "-pix_fmt", "rgb24", "-start_number", "0", str(out / "%08d.png"))
    return sorted(out.glob("*.png"))


def frames_dir_to_mp4(frames_dir: Path | str, dst: Path | str, fps: float, *, audio: Path | str | None = None) -> Path:
    """`%08d.png` frames → H.264 MP4 (yuv420p, CRF 16), optionally muxed with `audio` (cut to the shorter)."""
    args = ["-framerate", f"{fps:g}", "-start_number", "0", "-i", str(Path(frames_dir) / "%08d.png")]
    if audio is not None:
        args += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", str(dst)]
    ffmpeg(*args)
    return Path(dst)


def extract_last_frames(video: Path | str, out_dir: Path | str, count: int) -> list[Path]:
    """The last `count` frames as PNGs (chunk continuation: the next chunk starts from them)."""
    info = probe(video)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fps = info.fps or 25.0
    start = max(0.0, info.duration_s - (count + 1) / fps)
    ffmpeg("-ss", f"{start:.4f}", "-i", str(video), "-vsync", "0", str(out / "f_%04d.png"))
    frames = sorted(out.glob("f_*.png"))
    return frames[-count:]


def concat_videos(parts: Sequence[Path | str], dst: Path | str) -> Path:
    """Concatenates clips with identical codecs (chunks of one shot) without re-encoding."""
    dst = Path(dst)
    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in parts), encoding="utf-8")
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dst))
    return dst


def conform_clip(
    src: Path | str,
    dst: Path | str,
    *,
    width: int,
    height: int,
    duration_s: float | None = None,
    audio: Path | str | None = None,
    fps: float | None = None,
) -> Path:
    """An engine's raw clip → the requested frame: scaled to cover `width`×`height` and
    center-cropped (engines generate in their own size buckets), optionally trimmed, resampled to
    `fps` and muxed with `audio` (talking shots carry the dialogue bit-exact)."""
    filters = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1"
    )
    if fps:
        filters += f",fps={fps:g}"
    args = ["-i", str(src)]
    if audio is not None:
        args += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
    args += ["-vf", filters]
    if duration_s is not None:
        args += ["-t", f"{duration_s:.4f}"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "192k"] if audio is not None else ["-an"]
    ffmpeg(*args, str(dst))
    return Path(dst)


def plan_size(width: int, height: int, *, multiple: int, max_pixels: int) -> tuple[int, int]:
    """The generation size for a requested frame: the same aspect, at most `max_pixels`, both sides
    multiples of `multiple` (the result is resized to the request afterwards)."""
    scale = min(1.0, (max_pixels / float(width * height)) ** 0.5)
    w = max(multiple, int(width * scale) // multiple * multiple)
    h = max(multiple, int(height * scale) // multiple * multiple)
    return w, h


def resize_image(src: Path | str, dst: Path | str, width: int, height: int) -> Path:
    """Scales to cover `width`×`height` and center-crops (Lanczos)."""
    from PIL import Image

    with Image.open(src) as opened:
        image = opened.convert("RGB")
        scale = max(width / image.width, height / image.height)
        size = (max(width, round(image.width * scale)), max(height, round(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        left, top = (image.width - width) // 2, (image.height - height) // 2
        image.crop((left, top, left + width, top + height)).save(dst)
    return Path(dst)
