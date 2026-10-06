"""Picture realism (§22 `ce_realism`): LUT, saturation, white-balance drift, grain matched to the
sensor, vignette, lens distortion and platform-like compression — FFmpeg filters built from a
camera profile, deterministic given the seed. Face restoration, beauty smoothing and cinematic
grading are never applied on UGC modes (§22: disabled by default)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["RealismPlan", "realism_plan"]

GRAIN_SCALE = 350.0  # sensor noise (fraction of full scale) → FFmpeg `noise` strength


def _rng(seed: int, salt: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(f"{seed}:{salt}".encode()).hexdigest()[:12], 16))


def _escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


@dataclass(frozen=True)
class RealismPlan:
    filters: list[str] = field(default_factory=list)
    bitrate_kbps: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def realism_plan(profile: Any, config_root: Path, seed: int) -> RealismPlan:
    """Filters for one shot's mezzanine (applied after camera post)."""
    if profile is None:
        return RealismPlan(summary={"profile": None})
    filters: list[str] = []
    summary: dict[str, Any] = {"profile": profile.id}
    lut = config_root / str(profile.color.lut)
    if lut.is_file():
        filters.append(f"lut3d=file='{_escape(lut)}':interp=tetrahedral")
        summary["lut"] = lut.name
    rng = _rng(seed, "white_balance")
    warm = float(rng.uniform(-0.02, 0.02))  # per-shot white-balance drift (cameras re-meter between takes)
    saturation = float(profile.color.saturation)
    # Saturation and the drift as 1-D chroma tables (YUV, one table lookup per sample): a warm drift
    # raises Cr and lowers Cb by the amounts that move R up and B down by `warm` of full scale
    # (R − Y = 1.402·(Cr − 128), B − Y = 1.772·(Cb − 128)). An RGB `colorbalance` pass cost as much
    # as the 3-D LUT on 2 vCPUs.
    dv, du = warm * 255 / 1.402, -warm * 255 / 1.772
    filters.append(
        f"lutyuv=u='clip(128+(val-128)*{saturation:.3f}+{du:.3f}\\,0\\,255)'"
        f":v='clip(128+(val-128)*{saturation:.3f}+{dv:.3f}\\,0\\,255)'"
    )
    summary.update({"saturation": saturation, "white_balance_drift": round(warm, 4)})
    lens = profile.lens
    if abs(float(lens.distortion_k1)) > 1e-4:
        filters.append(f"lenscorrection=k1={-float(lens.distortion_k1) * 0.5:.4f}:k2=0")
        summary["distortion_k1"] = float(lens.distortion_k1)
    if float(lens.vignette) > 0:
        angle = min(1.2, 0.25 + float(lens.vignette) * 1.6)
        filters.append(f"vignette=angle={angle:.3f}")
        summary["vignette_angle"] = round(angle, 3)
    luma = round(float(profile.sensor.noise_luma) * GRAIN_SCALE, 2)
    chroma = round(float(profile.sensor.noise_chroma) * GRAIN_SCALE, 2)
    if luma > 0 or chroma > 0:
        filters.append(f"noise=c0s={luma}:c0f=t+u:c1s={chroma}:c1f=t+u:c2s={chroma}:c2f=t+u")
        summary["grain"] = {"luma": luma, "chroma": chroma}
    filters.append("format=yuv420p")
    return RealismPlan(filters=filters, bitrate_kbps=int(profile.compression.bitrate_kbps), summary=summary)
