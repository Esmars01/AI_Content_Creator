"""Seeded procedural camera motion (§22 post camera, ADR 0009): deterministic given the seed.

Each axis is a sum of `K` sinusoids with seeded frequencies in the profile's `freq_hz` band and
seeded phases (band-limited noise); amplitudes are scaled so the RMS displacement equals
`amplitude_px × (1 − stabilization)` at the reference height. Rotation follows the same recipe with
`rotation_deg`; `breathing_sway` adds a slow vertical sway (≈0.25 Hz). Paths come both as FFmpeg
expressions (evaluated per frame by `crop`/`rotate`) and as numpy functions (tests, blur sizing).

Also here: autofocus hunts (brief blur ramps at seeded times) and auto-exposure drift.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

import numpy as np

__all__ = ["CameraMotion", "FocusHunt", "Wave", "exposure_expression", "focus_hunts", "motion_for"]

K = 4
REFERENCE_HEIGHT = 1920.0
BREATHING_HZ = 0.25


def _rng(seed: int, salt: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(f"{seed}:{salt}".encode()).hexdigest()[:12], 16))


@dataclass(frozen=True)
class Wave:
    """A sum of sinusoids: Σ a_k sin(2π f_k t + φ_k)."""

    terms: tuple[tuple[float, float, float], ...] = ()  # (amplitude, frequency, phase)

    def expression(self, scale: str = "1") -> str:
        if not self.terms:
            return "0"
        parts = [f"{a:.4f}*sin(2*PI*{f:.4f}*t+{p:.4f})" for a, f, p in self.terms if abs(a) > 1e-6]
        return f"({scale})*(" + "+".join(parts) + ")" if parts else "0"

    def __call__(self, t: np.ndarray) -> np.ndarray:
        out = np.zeros_like(t, dtype=float)
        for a, f, p in self.terms:
            out += a * np.sin(2 * np.pi * f * t + p)
        return out

    def velocity(self, t: np.ndarray) -> np.ndarray:
        out = np.zeros_like(t, dtype=float)
        for a, f, p in self.terms:
            out += a * 2 * np.pi * f * np.cos(2 * np.pi * f * t + p)
        return out


def _wave(rng: np.random.Generator, rms: float, band: tuple[float, float]) -> Wave:
    if rms <= 0 or band[1] <= 0:
        return Wave()
    lo, hi = max(0.01, band[0]), max(band[0], band[1], 0.02)
    freqs = rng.uniform(lo, hi, K)
    phases = rng.uniform(0, 2 * np.pi, K)
    weights = rng.uniform(0.5, 1.0, K) / np.sqrt(np.arange(1, K + 1))
    amps = weights * rms * math.sqrt(2.0) / float(np.sqrt(np.sum(weights**2)))  # RMS of Σ a sin = sqrt(Σa²/2)
    return Wave(tuple((float(a), float(f), float(p)) for a, f, p in zip(amps, freqs, phases, strict=True)))


@dataclass(frozen=True)
class CameraMotion:
    x: Wave = field(default_factory=Wave)  # pixels at REFERENCE_HEIGHT
    y: Wave = field(default_factory=Wave)
    angle: Wave = field(default_factory=Wave)  # radians

    @property
    def still(self) -> bool:
        return not (self.x.terms or self.y.terms or self.angle.terms)

    def peak_px(self, height: int, duration_s: float = 10.0) -> float:
        """Largest displacement over the clip, scaled to `height` (crop margin)."""
        t = np.linspace(0, max(duration_s, 0.1), 512)
        scale = height / REFERENCE_HEIGHT
        return float(np.max(np.hypot(self.x(t), self.y(t)))) * scale

    def mean_speed_px_per_frame(self, height: int, fps: float, duration_s: float = 10.0) -> float:
        t = np.linspace(0, max(duration_s, 0.1), 512)
        scale = height / REFERENCE_HEIGHT
        return float(np.mean(np.hypot(self.x.velocity(t), self.y.velocity(t)))) * scale / max(fps, 1.0)


def motion_for(profile: object | None, seed: int, *, intensity: float = 1.0) -> CameraMotion:
    """The motion of a camera profile (its `motion` block), deterministic for `seed`."""
    motion = getattr(profile, "motion", None)
    if motion is None or str(motion.type) in ("static", "tripod"):
        return CameraMotion()
    keep = max(0.0, 1.0 - float(motion.stabilization)) * intensity
    band = (float(motion.freq_hz[0]), float(motion.freq_hz[1]))
    rms = float(motion.amplitude_px) * keep
    y = _wave(_rng(seed, "y"), rms * 0.8, band)
    if motion.breathing_sway:
        y = Wave((*y.terms, (rms * 0.6, BREATHING_HZ, float(_rng(seed, "breath").uniform(0, 2 * np.pi)))))
    return CameraMotion(
        x=_wave(_rng(seed, "x"), rms, band),
        y=y,
        angle=_wave(
            _rng(seed, "angle"), math.radians(float(motion.rotation_deg)) * keep, (band[0] * 0.5, band[1] * 0.5)
        ),
    )


@dataclass(frozen=True)
class FocusHunt:
    start_s: float
    duration_s: float


def focus_hunts(rate_per_min: float, duration_s: float, seed: int) -> list[FocusHunt]:
    """Seeded autofocus hunts: a Poisson process at `rate_per_min`, each a ~0.45 s blur ramp."""
    if rate_per_min <= 0 or duration_s <= 1.0:
        return []
    rng = _rng(seed, "focus")
    out: list[FocusHunt] = []
    t = float(rng.exponential(60.0 / rate_per_min))
    while t < duration_s - 0.5:
        out.append(FocusHunt(round(t, 3), 0.45))
        t += float(rng.exponential(60.0 / rate_per_min)) + 0.5
    return out


def exposure_expression(drift: float, seed: int) -> str | None:
    """`eq` brightness drift (slow, ±drift/2 of the brightness range)."""
    if drift <= 0:
        return None
    rng = _rng(seed, "exposure")
    f1, f2 = rng.uniform(0.05, 0.15), rng.uniform(0.15, 0.4)
    p1, p2 = rng.uniform(0, 2 * np.pi, 2)
    a = drift / 2.0
    return f"{a * 0.7:.4f}*sin(2*PI*{f1:.4f}*t+{p1:.4f})+{a * 0.3:.4f}*sin(2*PI*{f2:.4f}*t+{p2:.4f})"
