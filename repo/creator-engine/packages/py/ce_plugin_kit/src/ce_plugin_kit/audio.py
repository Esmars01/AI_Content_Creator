"""Audio shaping shared by generative audio engines (music beds, SFX, room tone; §22).

Engines generate in their own length steps (ACE-Step from 10 s, MOSS-SoundEffect up to 30 s per
call); the timeline needs exact durations. These helpers fit generated samples to a duration
with short fades (beds), or loop an ambience with equal-power crossfades when it is longer than
one generation."""

from __future__ import annotations

import numpy as np

__all__ = ["fit_duration", "loop_to", "to_mono"]


def to_mono(samples: np.ndarray) -> np.ndarray:
    """`(n,)`, `(n, c)` or `(c, n)` float samples → mono `(n,)` (channels averaged)."""
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        return data
    if data.ndim == 3:  # (batch, channels, n)
        data = data[0]
    channels_first = data.shape[0] < data.shape[1]
    return (data.mean(axis=0) if channels_first else data.mean(axis=1)).astype(np.float32)


def _fade(samples: np.ndarray, sample_rate: int, fade_in_s: float, fade_out_s: float) -> np.ndarray:
    out = samples.copy()
    n_in, n_out = min(len(out), int(fade_in_s * sample_rate)), min(len(out), int(fade_out_s * sample_rate))
    if n_in:
        out[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if n_out:
        out[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    return out


def fit_duration(
    samples: np.ndarray, sample_rate: int, duration_s: float, *, fade_in_s: float = 0.0, fade_out_s: float = 0.5
) -> np.ndarray:
    """Exactly `duration_s` long: trimmed (with a fade-out at the cut) or padded with silence."""
    data = to_mono(samples)
    target = round(duration_s * sample_rate)
    if len(data) >= target:
        data = data[:target]
    else:
        data = np.concatenate([data, np.zeros(target - len(data), dtype=np.float32)])
    return _fade(data, sample_rate, fade_in_s, fade_out_s)


def loop_to(samples: np.ndarray, sample_rate: int, duration_s: float, *, crossfade_s: float = 1.0) -> np.ndarray:
    """An ambience repeated to `duration_s` with equal-power crossfades at each seam (no fades at
    the ends: room tone sits under the whole scene)."""
    data = to_mono(samples)
    target = round(duration_s * sample_rate)
    if len(data) >= target:
        return data[:target].copy()
    xf = min(int(crossfade_s * sample_rate), len(data) // 2)
    if xf <= 0:
        return np.resize(data, target).astype(np.float32)
    t = np.linspace(0.0, np.pi / 2, xf, dtype=np.float32)
    fade_out, fade_in = np.cos(t), np.sin(t)
    out = data.copy()
    while len(out) < target:
        seam = out[-xf:] * fade_out + data[:xf] * fade_in
        out = np.concatenate([out[:-xf], seam, data[xf:]])
    return out[:target].astype(np.float32)
