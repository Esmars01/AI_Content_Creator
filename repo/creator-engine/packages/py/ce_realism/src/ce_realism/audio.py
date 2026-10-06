"""Sound realism (§19.7, §22, §27 mix step 1): per-scene world acoustics and the mic chain.

For each dialogue segment: mic EQ (RBJ biquads from `config/mic_profiles`) → gentle compression →
room (convolution with the bound world's impulse response at the room's wet mix) → gentle
de-essing (a 5–9 kHz split-band compressor, at most 4 dB). Breaths are kept: nothing gates the
signal. Per scene: an ambient bed (the world's ambient profiles plus scene additions) at the world's
noise floor — the room tone the mix places under the whole scene. Everything is numpy/scipy on
float32 PCM, deterministic given the seed.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy import signal

__all__ = ["AMBIENT_OFFSETS_DB", "ambient_bed", "biquad", "deess", "mic_chain", "process_dialogue", "room"]

AMBIENT_OFFSETS_DB = {  # level above the world's noise floor (room tone sits at the floor)
    "room_tone_quiet": 0.0,
    "room_tone_light_hvac": 2.0,
    "street_distant": 4.0,
    "cafe_murmur": 10.0,
    "birds_outside": 4.0,
    "rain_on_window": 6.0,
    "car_cabin": 8.0,
    "kitchen_appliances": 5.0,
}


def _rng(seed: int, salt: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(f"{seed}:{salt}".encode()).hexdigest()[:12], 16))


def biquad(kind: str, freq: float, sr: int, *, gain_db: float = 0.0, q: float = 0.707) -> np.ndarray:
    """One RBJ-cookbook biquad as second-order sections."""
    freq = min(freq, sr * 0.45)
    w0 = 2 * math.pi * freq / sr
    cos, sin = math.cos(w0), math.sin(w0)
    alpha = sin / (2 * q)
    a = 10 ** (gain_db / 40)
    if kind == "highpass":
        b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
        den = [1 + alpha, -2 * cos, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
        den = [1 + alpha, -2 * cos, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * a, -2 * cos, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cos, 1 - alpha / a]
    elif kind in ("shelf_low", "shelf_high"):
        sq = 2 * math.sqrt(a) * alpha
        if kind == "shelf_low":
            b = [
                a * ((a + 1) - (a - 1) * cos + sq),
                2 * a * ((a - 1) - (a + 1) * cos),
                a * ((a + 1) - (a - 1) * cos - sq),
            ]
            den = [(a + 1) + (a - 1) * cos + sq, -2 * ((a - 1) + (a + 1) * cos), (a + 1) + (a - 1) * cos - sq]
        else:
            b = [
                a * ((a + 1) + (a - 1) * cos + sq),
                -2 * a * ((a - 1) + (a + 1) * cos),
                a * ((a + 1) + (a - 1) * cos - sq),
            ]
            den = [(a + 1) - (a - 1) * cos + sq, 2 * ((a - 1) - (a + 1) * cos), (a + 1) - (a - 1) * cos - sq]
    else:
        raise ValueError(f"unknown filter {kind!r}")
    return np.array([[b[0] / den[0], b[1] / den[0], b[2] / den[0], 1.0, den[1] / den[0], den[2] / den[0]]])


def _envelope(x: np.ndarray, sr: int, attack_s: float, release_s: float) -> np.ndarray:
    """RMS-ish envelope with separate attack and release (one-pole smoothing of |x|²)."""
    power = x.astype(np.float64) ** 2
    att, rel = math.exp(-1.0 / (sr * attack_s)), math.exp(-1.0 / (sr * release_s))
    # vectorized approximation: smooth with the release pole, then take the max with an attack-smoothed copy
    slow = signal.lfilter([1 - rel], [1, -rel], power)
    fast = signal.lfilter([1 - att], [1, -att], power)
    return np.sqrt(np.maximum(slow, fast) + 1e-12)


def _compress(x: np.ndarray, sr: int, threshold_db: float, ratio: float) -> np.ndarray:
    env_db = 20 * np.log10(_envelope(x, sr, 0.005, 0.08))
    over = np.maximum(0.0, env_db - threshold_db)
    gain_db = -over * (1 - 1 / max(ratio, 1.0))
    return (x * 10 ** (gain_db / 20)).astype(np.float32)


def mic_chain(x: np.ndarray, sr: int, mic: Any | None) -> np.ndarray:
    if mic is None:
        return x
    y = x.astype(np.float64)
    for band in mic.eq:
        kind = str(band.type)
        q = float(band.q) if band.q else 0.707
        y = signal.sosfilt(biquad(kind, float(band.freq_hz), sr, gain_db=float(band.gain_db or 0.0), q=q), y)
    return _compress(y.astype(np.float32), sr, float(mic.compression.threshold_db), float(mic.compression.ratio))


def room(x: np.ndarray, ir: np.ndarray | None, wet_mix: float) -> np.ndarray:
    if ir is None or ir.size == 0 or wet_mix <= 0:
        return x
    kernel = ir.astype(np.float64) / (float(np.sqrt(np.sum(ir.astype(np.float64) ** 2))) or 1.0)
    wet = signal.fftconvolve(x.astype(np.float64), kernel)[: x.size]
    dry_rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) or 1.0
    wet_rms = float(np.sqrt(np.mean(wet**2))) or 1.0
    wet *= dry_rms / wet_rms
    return ((1 - wet_mix) * x + wet_mix * wet).astype(np.float32)


def deess(x: np.ndarray, sr: int, *, threshold_db: float = -30.0, max_reduction_db: float = 4.0) -> np.ndarray:
    if sr < 20_000:
        return x
    band = signal.sosfilt(signal.butter(4, [5000, 9000], btype="bandpass", fs=sr, output="sos"), x.astype(np.float64))
    env_db = 20 * np.log10(_envelope(band, sr, 0.002, 0.05))
    reduction = np.clip((env_db - threshold_db) * 0.5, 0.0, max_reduction_db)
    gain = 10 ** (-reduction / 20)
    return (x - band + band * gain).astype(np.float32)


def process_dialogue(x: np.ndarray, sr: int, *, mic: Any | None, ir: np.ndarray | None, wet_mix: float) -> np.ndarray:
    """One segment through the mic and the room (§27 mix step 1)."""
    return deess(room(mic_chain(x, sr, mic), ir, wet_mix), sr)


def _colored(rng: np.random.Generator, n: int, exponent: float) -> np.ndarray:
    """Noise with a 1/f^exponent power spectrum, unit RMS."""
    spectrum = rng.normal(size=n // 2 + 1) + 1j * rng.normal(size=n // 2 + 1)
    f = np.fft.rfftfreq(n)
    f[0] = f[1] if n > 1 else 1.0
    spectrum *= f ** (-exponent / 2)
    out = np.fft.irfft(spectrum, n)
    return out / (float(np.sqrt(np.mean(out**2))) or 1.0)


def _bed(kind: str, n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / sr
    if kind == "room_tone_light_hvac":
        base = _colored(rng, n, 2.0) * 0.8 + 0.25 * np.sin(2 * np.pi * 120 * t)
    elif kind == "street_distant":
        swell = 0.6 + 0.4 * np.sin(2 * np.pi * rng.uniform(0.03, 0.08) * t + rng.uniform(0, 6.28))
        base = signal.sosfilt(biquad("lowpass", 900, sr), _colored(rng, n, 2.0)) * swell
    elif kind == "cafe_murmur":
        mod = 0.7 + 0.3 * np.abs(signal.sosfilt(biquad("lowpass", 4, sr), rng.normal(size=n)) * 8)
        base = (
            signal.sosfilt(signal.butter(2, [300, 3000], btype="bandpass", fs=sr, output="sos"), rng.normal(size=n))
            * mod
        )
    elif kind == "birds_outside":
        base = _colored(rng, n, 1.0) * 0.5
        for start in rng.uniform(0, max(t[-1] - 0.3, 0.01), max(1, int(t[-1] / 2))):
            idx = (t >= start) & (t < start + 0.12)
            base[idx] += 1.5 * np.sin(2 * np.pi * (3500 + 2000 * (t[idx] - start) / 0.12) * t[idx])
    elif kind == "rain_on_window":
        base = signal.sosfilt(biquad("highpass", 1500, sr), rng.normal(size=n)) + 0.3 * (rng.random(n) > 0.999)
    elif kind == "car_cabin":
        base = signal.sosfilt(biquad("lowpass", 300, sr), _colored(rng, n, 2.0))
    elif kind == "kitchen_appliances":
        base = _colored(rng, n, 1.0) * 0.6 + 0.2 * np.sin(2 * np.pi * 60 * t) + 0.1 * np.sin(2 * np.pi * 180 * t)
    else:  # room_tone_quiet and anything unknown: soft pink noise
        base = signal.sosfilt(biquad("lowpass", 4000, sr), _colored(rng, n, 1.0))
    return base / (float(np.sqrt(np.mean(base**2))) or 1.0)


def ambient_bed(kinds: Sequence[str], duration_s: float, sr: int, *, noise_floor_db: float, seed: int) -> np.ndarray:
    """The scene's room tone and ambient beds at the world's noise floor (dBFS RMS)."""
    n = max(1, round(duration_s * sr))
    out = np.zeros(n, dtype=np.float64)
    for kind in dict.fromkeys(kinds or ["room_tone_quiet"]):
        level = 10 ** ((noise_floor_db + AMBIENT_OFFSETS_DB.get(kind, 0.0)) / 20)
        out += _bed(kind, n, sr, _rng(seed, kind)) * level
    fade = min(n // 2, int(0.05 * sr))
    if fade > 0:
        ramp = np.linspace(0, 1, fade)
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    return out.astype(np.float32)
