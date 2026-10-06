"""Frame sampling through FFmpeg: RGB frames at `sample_hz`, scaled so the longer side is at most
`max_side` pixels (landmark models run at low resolution anyway)."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["Sampled", "sample_frames"]


@dataclass(frozen=True)
class Sampled:
    frames: list[np.ndarray]
    width: int
    height: int
    duration_s: float
    hz: float


def _probe(path: Path) -> tuple[int, int, float]:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise RuntimeError("ffprobe is not installed")
    out = subprocess.run(  # noqa: S603 - fixed argv over a downloaded artifact
        [
            binary,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    return int(stream.get("width", 0)), int(stream.get("height", 0)), float(data.get("format", {}).get("duration", 0.0))


def sample_frames(path: Path, hz: float, *, max_side: int = 640) -> Sampled:
    width, height, duration = _probe(path)
    if width <= 0 or height <= 0:
        raise ValueError(f"{path.name} has no video stream")
    scale = min(1.0, max_side / max(width, height))
    w, h = max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is not installed")
    raw = subprocess.run(  # noqa: S603 - fixed argv over a downloaded artifact
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"fps={hz:g},scale={w}:{h}:flags=bilinear",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=600,
    ).stdout
    size = w * h * 3
    frames = [
        np.frombuffer(raw[i : i + size], dtype=np.uint8).reshape(h, w, 3) for i in range(0, len(raw) - size + 1, size)
    ]
    return Sampled(frames, w, h, duration, hz)
