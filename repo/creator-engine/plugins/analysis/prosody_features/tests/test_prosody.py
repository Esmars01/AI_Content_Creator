"""Prosody features (librosa) on synthetic voiced audio with known pitch, pauses and timing:
always runnable (no downloaded assets)."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover

SR = 16_000


def _voice(f0: float, seconds: float) -> np.ndarray:
    """A buzzy harmonic tone (a crude vowel) at `f0`."""
    t = np.arange(int(SR * seconds)) / SR
    tone = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
    envelope = np.minimum(1.0, np.minimum(t, seconds - t) / 0.03)
    return (0.25 * tone * envelope).astype(np.float32)


def _wav(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _run(tmp: Path, samples: np.ndarray, words: list[m.AlignedWord]) -> m.ProsodyFeaturesResult:
    async def go() -> m.ProsodyFeaturesResult:
        adapter = discover(app_env="test", include_mocks=False).get("prosody_features").adapter()
        await adapter.load(LoadContext(model_cache_dir=str(tmp), scratch_dir=str(tmp / "s"), app_env="test"))
        ctx = LocalRunContext(tmp / "ctx")
        ref = await ctx.put_file(_wav(tmp / "speech.wav", samples), "audio")
        result = await adapter.run("audio.prosody", m.AudioAnalysisRequest(audio=ref, word_timings=words), ctx)
        assert isinstance(result, m.ProsodyFeaturesResult)
        return result

    return asyncio.run(go())


def test_pitch_energy_pauses_and_rate(tmp_path: Path) -> None:
    silence = np.zeros(int(SR * 0.6), np.float32)
    samples = np.concatenate([_voice(140.0, 1.0), silence, _voice(220.0, 1.0)])
    words = [
        m.AlignedWord(index=0, word="one", start_s=0.0, end_s=0.5),
        m.AlignedWord(index=1, word="two", start_s=0.5, end_s=1.0),
        m.AlignedWord(index=2, word="three", start_s=1.6, end_s=2.1),
        m.AlignedWord(index=3, word="four", start_s=2.1, end_s=2.6),
    ]
    result = _run(tmp_path, samples, words)
    hz = result.sample_hz
    pitch = np.array(result.series["pitch_hz"])
    first, second = pitch[int(0.2 * hz) : int(0.8 * hz)], pitch[int(1.8 * hz) : int(2.4 * hz)]
    assert np.median(first[first > 0]) == pytest.approx(140.0, rel=0.05)
    assert np.median(second[second > 0]) == pytest.approx(220.0, rel=0.05)
    energy = np.array(result.series["energy"])
    assert energy[int(1.3 * hz)] < 0.1 * energy[int(0.5 * hz)]  # the pause is quiet
    assert [(p.start_s, p.end_s) for p in result.pauses] == [(1.0, 1.6)]
    assert result.speech_rate_wpm == pytest.approx(4 / 2.6 * 60, rel=0.01)


def test_pauses_without_timings_come_from_silence(tmp_path: Path) -> None:
    samples = np.concatenate([_voice(160.0, 1.0), np.zeros(int(SR * 0.8), np.float32), _voice(160.0, 1.0)])
    result = _run(tmp_path, samples, [])
    assert len(result.pauses) == 1
    pause = result.pauses[0]
    assert pause.start_s == pytest.approx(1.0, abs=0.15) and pause.end_s == pytest.approx(1.8, abs=0.15)
