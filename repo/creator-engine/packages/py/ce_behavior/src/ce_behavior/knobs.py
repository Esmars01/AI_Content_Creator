"""Knob calibration (§15.7): abstract knob value → observed effect curves.

`CalibrationWorkflow` renders the calibration fixtures at evenly spaced knob values (`sweep`),
measures each clip with the knob's effect metric (`KNOB_EFFECTS`, from analyzer outputs) and fits
the curve here. A knob is **calibrated** — and only then used by the compiler — when the effect
rises with the knob (Spearman ρ ≥ `min_rho`) by a usable margin (`min_effect_span`, relative to
the effect at the lowest setting). Anything else is stored too (`calibrated: false`, with the
reason) so the evidence is visible."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["KNOB_EFFECTS", "KnobEffect", "facial_expressivity", "fit_knob", "head_motion_energy", "sweep"]


def sweep(points: int = 5) -> list[float]:
    """Evenly spaced knob values over [0, 1]."""
    if points < 3:
        raise ValueError("a calibration sweep needs at least 3 points")
    return [round(i / (points - 1), 4) for i in range(points)]


def head_motion_energy(series: Mapping[str, Sequence[float]], sample_hz: float) -> float:
    """Mean absolute head rotation speed (deg/s) from `face.landmarks` yaw and pitch series."""
    yaw = np.asarray(series.get("head_yaw_deg", []), dtype=np.float64)
    pitch = np.asarray(series.get("head_pitch_deg", []), dtype=np.float64)
    if len(yaw) < 2 or len(pitch) < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(yaw)) + np.abs(np.diff(pitch))) * sample_hz)


def facial_expressivity(series: Mapping[str, Sequence[float]], sample_hz: float) -> float:
    """Spread of expression blendshapes (smile, brow raise, jaw open): mean standard deviation."""
    names = ("smile", "brow_raise", "jaw_open")
    values = [np.std(np.asarray(series[n], dtype=np.float64)) for n in names if len(series.get(n, [])) > 1]
    return float(np.mean(values)) if values else 0.0


@dataclass(frozen=True)
class KnobEffect:
    analyzer_capability: str
    metric: str
    measure: Any  # (series, sample_hz) -> float


KNOB_EFFECTS: dict[str, KnobEffect] = {
    "motion_energy": KnobEffect("face.landmarks", "head_motion_deg_per_s", head_motion_energy),
    "head_motion": KnobEffect("face.landmarks", "head_motion_deg_per_s", head_motion_energy),
    "expressivity": KnobEffect("face.landmarks", "expression_spread", facial_expressivity),
}


def _ranks(values: Sequence[float]) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=np.float64)
    ranks[order] = np.arange(len(values), dtype=np.float64)
    data = np.asarray(values, dtype=np.float64)
    for value in np.unique(data):  # ties share their mean rank
        mask = data == value
        ranks[mask] = ranks[mask].mean()
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    rx, ry = _ranks(x), _ranks(y)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def fit_knob(
    points: Sequence[tuple[float, float]],
    *,
    knob: str,
    param: str,
    param_range: tuple[float, float],
    min_rho: float = 0.8,
    min_effect_span: float = 0.15,
) -> dict[str, Any]:
    """The stored curve: points (knob value, mean effect), Spearman ρ, relative span, the verdict."""
    if len(points) < 3:
        raise ValueError("at least 3 calibration points")
    ordered = sorted(points)
    xs, ys = [p[0] for p in ordered], [p[1] for p in ordered]
    rho = spearman(xs, ys)
    base = abs(ys[0]) if abs(ys[0]) > 1e-9 else max(abs(v) for v in ys) or 1.0
    span = (max(ys) - min(ys)) / base
    reasons = []
    if rho < min_rho:
        reasons.append(f"not monotonic (rho {rho:.2f} < {min_rho})")
    if span < min_effect_span:
        reasons.append(f"effect too small (span {span:.2f} < {min_effect_span})")
    effect = KNOB_EFFECTS.get(knob)
    return {
        "knob": knob,
        "param": param,
        "param_range": list(param_range),
        "metric": effect.metric if effect else "unknown",
        "points": [[round(x, 4), round(y, 6)] for x, y in ordered],
        "rho": round(rho, 4),
        "span": round(float(span), 4),
        "monotonic": "true" if rho >= min_rho else ("false" if rho <= -min_rho else "unverified"),
        "calibrated": not reasons,
        "reasons": reasons,
    }
