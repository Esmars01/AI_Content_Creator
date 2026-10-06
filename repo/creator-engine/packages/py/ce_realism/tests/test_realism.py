"""ce_realism (§22, §19.7): picture realism filters and the sound chain."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from ce_config.loader import load_config
from ce_realism import ambient_bed, biquad, deess, mic_chain, process_dialogue, realism_plan, room
from scipy import signal

ROOT = Path(__file__).resolve().parents[4]
BUNDLE = load_config(ROOT / "config", "test")
SR = 48_000


def test_realism_plan_from_profile() -> None:
    plan = realism_plan(BUNDLE.camera_profiles["phone_front_selfie"], ROOT / "config", 11)
    joined = ",".join(plan.filters)
    assert "lut3d=file=" in joined and "phone_natural.cube" in joined
    assert "noise=c0s=" in joined and "vignette=angle=" in joined and "lenscorrection" in joined
    assert plan.bitrate_kbps == 8000
    assert plan == realism_plan(BUNDLE.camera_profiles["phone_front_selfie"], ROOT / "config", 11)
    assert realism_plan(None, ROOT / "config", 1).filters == []


def _tone(freq: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _gain_db(sos: np.ndarray, freq: float) -> float:
    _w, h = signal.sosfreqz(sos, worN=[freq], fs=SR)
    return float(20 * np.log10(abs(h[0])))


def test_biquads_shape_the_spectrum() -> None:
    assert _gain_db(biquad("highpass", 200, SR), 50) < -15
    assert abs(_gain_db(biquad("highpass", 200, SR), 2000)) < 0.5
    assert 4.5 < _gain_db(biquad("peak", 3000, SR, gain_db=5, q=1.0), 3000) < 5.5
    assert _gain_db(biquad("lowpass", 8000, SR), 16000) < -10


def test_mic_room_deess_and_beds() -> None:
    mic = BUNDLE.mic_profiles["laptop_mic"]
    low = mic_chain(_tone(100), SR, mic)
    assert np.sqrt(np.mean(low**2)) < 0.3 * np.sqrt(np.mean(_tone(100) ** 2))  # 250 Hz high-pass
    ir = np.zeros(4800, dtype=np.float32)
    ir[0], ir[2400] = 1.0, 0.5
    wet = room(_tone(440), ir, 0.3)
    assert wet.shape == _tone(440).shape and not np.allclose(wet, _tone(440))
    assert np.allclose(room(_tone(440), None, 0.3), _tone(440))
    hiss = _tone(7000)
    assert np.sqrt(np.mean(deess(hiss, SR) ** 2)) < np.sqrt(np.mean(hiss**2))
    processed = process_dialogue(_tone(300), SR, mic=mic, ir=ir, wet_mix=0.2)
    assert np.isfinite(processed).all()
    bed = ambient_bed(["room_tone_light_hvac", "cafe_murmur"], 2.0, SR, noise_floor_db=-60, seed=4)
    assert bed.shape == (2 * SR,)
    level = 20 * np.log10(float(np.sqrt(np.mean(bed**2))))
    assert -62 < level < -45
    assert np.array_equal(
        bed, ambient_bed(["room_tone_light_hvac", "cafe_murmur"], 2.0, SR, noise_floor_db=-60, seed=4)
    )
