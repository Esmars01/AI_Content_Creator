"""CPU stand-ins for the heavy calls of real engines (manifest `test_backend`, ADR 0051).

Test backends produce media of the shape the real engine would produce (resolution, frame count,
duration, sample rate) with NumPy and FFmpeg, deterministically from the seed, so the adapter code
around them — parameter building, chunking, encoding, artifact bookkeeping — runs for real in the
contract suite. Their outputs are labelled synthetic and are never routed: test backends exist only
in tests and in the contract suite, never in a worker (rule 5).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

from ce_plugin_kit.media import ffmpeg, write_wav

__all__ = ["fake_image", "fake_speech", "fake_speech_samples", "fake_tone", "fake_video", "seeded"]


def seeded(*parts: object) -> np.random.Generator:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def fake_image(path: Path | str, width: int, height: int, *, seed: int = 0, label: str = "") -> Path:
    """A seeded colour field with a lighter disc in the upper middle (a stand-in for a face)."""
    rng = seeded("image", seed, label, width, height)
    base = rng.integers(40, 200, size=3)
    yy, xx = np.mgrid[0:height, 0:width]
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[:] = base
    cy, cx, r = height * 0.38, width * 0.5, min(width, height) * 0.18
    disc = (yy - cy) ** 2 + (xx - cx) ** 2 <= r**2
    image[disc] = np.clip(base + 50, 0, 255)
    Image.fromarray(image).save(path)
    return Path(path)


def fake_video(
    path: Path | str, width: int, height: int, fps: float, duration_s: float, *, audio: Path | str | None = None
) -> Path:
    """A test pattern of the requested size, rate and length (optionally with an audio track)."""
    args = ["-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r={fps:g}:d={duration_s:.4f}"]
    if audio is not None:
        args += ["-i", str(audio), "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    ffmpeg(*args)
    return Path(path)


def fake_tone(
    path: Path | str, duration_s: float, sample_rate: int, *, seed: int = 0, frequency: float = 220.0
) -> Path:
    t = np.arange(int(duration_s * sample_rate)) / sample_rate
    rng = seeded("tone", seed, frequency)
    samples = 0.25 * np.sin(2 * np.pi * frequency * t) + 0.01 * rng.standard_normal(t.size)
    return write_wav(path, samples.astype(np.float32), sample_rate)


def fake_speech_samples(words: int, sample_rate: int, *, wpm: float = 150.0, seed: int = 0) -> np.ndarray:
    """Tone bursts, one per word at the given rate, with short gaps: speech-shaped, not speech."""
    rng = seeded("speech", seed, words)
    per_word = 60.0 / max(wpm, 1.0)
    chunks: list[np.ndarray] = []
    for _ in range(max(words, 1)):
        n = int(per_word * 0.8 * sample_rate)
        t = np.arange(n) / sample_rate
        freq = 140.0 + 40.0 * rng.random()
        envelope = np.sin(np.pi * np.linspace(0.0, 1.0, n)) ** 2
        chunks.append((0.3 * envelope * np.sin(2 * np.pi * freq * t)).astype(np.float32))
        chunks.append(np.zeros(int(per_word * 0.2 * sample_rate), dtype=np.float32))
    return np.concatenate(chunks)


def fake_speech(path: Path | str, words: int, sample_rate: int, *, wpm: float = 150.0, seed: int = 0) -> Path:
    return write_wav(path, fake_speech_samples(words, sample_rate, wpm=wpm, seed=seed), sample_rate)
