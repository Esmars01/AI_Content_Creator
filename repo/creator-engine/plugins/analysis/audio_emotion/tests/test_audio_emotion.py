"""audio.emotion adapter (Phase 11, sandbox): manifest and license block, windowing, span selection
and class averaging on the CPU stand-in. No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import ffmpeg
from ce_testing.engines import load_with_backend

from ce_plugin_audio_emotion.adapter import LABELS, windows
from ce_plugin_audio_emotion.testing import FakeEmotionBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_manifest_is_sandbox_and_non_commercial() -> None:
    manifest = REGISTRY.get("audio_emotion").manifest
    assert str(manifest.status) == "sandbox" and str(manifest.validation) == "untested_on_gpu"
    license_ = manifest.models[0].license
    assert license_.commercial_use is False and license_.condition("non_commercial") is True
    assert "66d7a4c264a5993a2a63ed00c1f402c296ee521a" in license_.url
    assert manifest.capability("audio.emotion") is not None


def test_windows_cover_the_clip() -> None:
    assert windows(16_000, 2.0, 1.0) == [(0, 16_000)]
    spans = windows(16_000 * 5 + 100, 2.0, 1.0)
    assert spans[0] == (0, 32_000) and spans[-1][1] == 16_000 * 5 + 100


def _classify(tmp: Path, volume: float, **kw: object) -> m.AudioEmotionResult:
    async def go() -> m.AudioEmotionResult:
        ctx = LocalRunContext(tmp / f"ctx{volume}", seed=1)
        path = tmp / f"voice{volume}.wav"
        ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=220:duration=4:sample_rate=48000", "-af", f"volume={volume}", str(path)
        )
        ref = await ctx.put_file(path, "audio")
        adapter = await load_with_backend(REGISTRY.get("audio_emotion"), FakeEmotionBackend(), tmp)
        return await adapter.run("audio.emotion", m.AudioAnalysisRequest(audio=ref, **kw), ctx)  # type: ignore[no-any-return, arg-type]

    return asyncio.run(go())


def test_classes_are_a_distribution_over_the_model_labels(tmp_path: Path) -> None:
    loud, quiet = _classify(tmp_path, 1.0), _classify(tmp_path / "q", 0.02)
    assert set(loud.classes) == set(LABELS) and abs(sum(loud.classes.values()) - 1.0) < 1e-3
    assert loud.classes["happy"] > quiet.classes["happy"] and quiet.classes["neutral"] > loud.classes["neutral"]


def test_a_too_short_span_is_refused(tmp_path: Path) -> None:
    word = m.AlignedWord(index=0, word="hi", start_s=1.0, end_s=1.1)
    with pytest.raises(ValueError, match=r"at least 0\.3 s"):
        _classify(tmp_path, 1.0, word_timings=[word])
