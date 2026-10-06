"""ce_camera (§22): seeded procedural motion and subject-aware reframing."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from ce_camera import (
    OneEuro,
    SubjectTrack,
    exposure_expression,
    focus_hunts,
    motion_for,
    piecewise_expression,
    plan_crop,
    track_from_detections,
)
from ce_config.loader import load_config

ROOT = Path(__file__).resolve().parents[4]
PROFILES = load_config(ROOT / "config", "test").camera_profiles


def test_motion_is_deterministic_and_follows_the_profile() -> None:
    handheld = PROFILES["phone_front_selfie"]
    a, b = motion_for(handheld, 7), motion_for(handheld, 7)
    assert a == b and motion_for(handheld, 8) != a
    t = np.linspace(0, 20, 4000)
    rms = float(np.sqrt(np.mean(a.x(t) ** 2)))
    expected = handheld.motion.amplitude_px * (1 - handheld.motion.stabilization)
    assert 0.6 * expected < rms < 1.5 * expected
    assert a.angle.terms and a.peak_px(1920, 10.0) > 0
    assert motion_for(PROFILES["desk_mirrorless"], 7).still or PROFILES["desk_mirrorless"].motion.type not in (
        "static",
        "tripod",
    )
    assert motion_for(None, 1).still
    assert "sin(2*PI*" in a.x.expression("1.0")


def test_focus_hunts_and_exposure_drift() -> None:
    hunts = focus_hunts(6.0, 60.0, 3)
    assert hunts == focus_hunts(6.0, 60.0, 3) and 1 <= len(hunts) <= 15
    assert all(0 <= h.start_s < 60 for h in hunts)
    assert focus_hunts(0.0, 60.0, 3) == []
    expr = exposure_expression(0.03, 5)
    assert expr is not None and "sin" in expr
    assert exposure_expression(0.0, 5) is None


def test_one_euro_smooths_jitter_but_follows_motion() -> None:
    rng = np.random.default_rng(0)
    f = OneEuro()
    still = [f(i / 10, 0.5 + rng.normal(0, 0.02)) for i in range(50)]
    assert np.std(still[10:]) < 0.02
    g = OneEuro()
    moved = [g(i / 10, 0.2 if i < 20 else 0.8) for i in range(60)]
    assert moved[-1] > 0.75


def test_reframe_follows_the_subject_and_reports_crop_loss() -> None:
    frames = [{"t_s": i / 2, "boxes": [[0.62, 0.30, 0.10, 0.18, 0.9]]} for i in range(8)]
    track = track_from_detections(frames)
    assert track.found and len(track.samples) == 8
    plan = plan_crop(track, src_aspect=16 / 9, dst_aspect=9 / 16)
    assert plan.layout == "crop" and plan.loss < 0.05
    x0 = plan.path[-1][1]
    assert x0 < 0.67 < x0 + plan.width  # the subject's centre is inside the 9:16 window
    centred = plan_crop(SubjectTrack(), src_aspect=16 / 9, dst_aspect=9 / 16)
    assert (
        centred.path[0][1] == round((1 - centred.width) / 2, 6)
        or abs(centred.path[0][1] - (1 - centred.width) / 2) < 1e-9
    )
    wide = track_from_detections([{"t_s": 0.0, "boxes": [[0.05, 0.1, 0.9, 0.8, 0.9]]}])
    fallback = plan_crop(wide, src_aspect=16 / 9, dst_aspect=9 / 16, threshold=0.25)
    assert fallback.layout == "blurred_fill" and fallback.loss > 0.25


def test_piecewise_expression() -> None:
    assert piecewise_expression([], default=0.25) == "0.250"
    expr = piecewise_expression([(0.0, 0.1), (1.0, 0.3)], default=0.0)
    assert expr.startswith("if(lt(t\\,0.000)") and "0.2000*(t-0.000)" in expr
