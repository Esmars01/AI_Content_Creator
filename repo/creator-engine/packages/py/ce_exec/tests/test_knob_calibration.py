"""Knob calibration runs (§15.7, Phase 8) on the mock avatar engines: the sweep renders real
media, the mock observer measures the knob's effect from the behavior track, and the fitted curve
says whether the knob may be used."""

from __future__ import annotations

from pathlib import Path

import pytest
from ce_behavior.knobs import fit_knob, sweep
from ce_contracts.plugins import discover
from ce_exec.calibration import default_fixture, knob_directives, pick_face_analyzer, run_local_calibration

REGISTRY = discover(app_env="test", include_mocks=True)


def test_the_calibration_fixture_is_committed() -> None:
    fixture = default_fixture()
    assert fixture.keyframe.is_file() and fixture.audio.is_file()


def test_knob_directives_carry_only_the_knob() -> None:
    directives = knob_directives("motion_energy", 0.75, 4.0)
    (visual,) = directives.visual
    (span,) = visual.sub_spans
    assert (span.start_s, span.end_s, span.knobs, span.item_refs) == (0.0, 4.0, {"motion_energy": 0.75}, [])
    assert directives.realizations == []


def test_fit_knob_verdicts() -> None:
    rising = fit_knob([(x, 1.0 + 2 * x) for x in sweep(5)], knob="motion_energy", param="p", param_range=(1, 2))
    assert (rising["calibrated"], rising["monotonic"], rising["reasons"]) == (True, "true", [])
    flat = fit_knob([(x, 1.0 + 0.01 * x) for x in sweep(5)], knob="motion_energy", param="p", param_range=(1, 2))
    assert flat["calibrated"] is False and any("too small" in r for r in flat["reasons"])
    noisy = fit_knob([(0, 1.0), (0.5, 3.0), (1, 1.5)], knob="motion_energy", param="p", param_range=(1, 2))
    assert noisy["calibrated"] is False and noisy["monotonic"] == "unverified"
    with pytest.raises(ValueError):
        sweep(2)


def test_mock_engines_are_measured_by_the_mock_observer() -> None:
    assert pick_face_analyzer(REGISTRY, prefer_mock=True).manifest.mock


async def test_mock_avatar_motion_energy_calibrates(tmp_path: Path) -> None:
    report = await run_local_calibration(
        REGISTRY, "mock_avatar_global", tmp_path, model_cache_dir=str(tmp_path / "cache"), app_env="test", points=3,
        seeds=(11,),
    )  # fmt: skip
    assert report["adapter_id"] == "mock_avatar_global" and report["analyzer"]
    curve = report["knobs"]["motion_energy"]
    assert len(curve["points"]) == 3 and curve["metric"] == "head_motion_deg_per_s"
    effects = [y for _x, y in curve["points"]]
    assert effects == sorted(effects) and curve["calibrated"] is True, curve


async def test_engines_without_avatar_knobs_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no avatar knobs"):
        await run_local_calibration(REGISTRY, "mock_voice", tmp_path, model_cache_dir=str(tmp_path), app_env="test")
