"""ACE-Step adapter (Phase 8): caption building, instrumental-only, minimum length and exact-duration
fitting on the CPU stand-in. No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import load_with_backend

from ce_plugin_audio_acestep.adapter import caption
from ce_plugin_audio_acestep.testing import FakeAceStepBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_caption_is_instrumental_and_capped() -> None:
    assert caption("warm lo-fi  piano, slow") == "warm lo-fi piano, slow, instrumental, no vocals"
    assert len(caption("x" * 2000)) == 512 and caption("").startswith("instrumental")


def _run(tmp: Path, request: m.MusicRequest) -> tuple[m.AudioResult, FakeAceStepBackend, LocalRunContext]:
    async def go() -> tuple[m.AudioResult, FakeAceStepBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp / "ctx", seed=42)
        backend = FakeAceStepBackend()
        adapter = await load_with_backend(REGISTRY.get("acestep_v15"), backend, tmp)
        return await adapter.run("audio.music", request, ctx), backend, ctx  # type: ignore[return-value]

    return asyncio.run(go())


def test_short_beds_are_generated_at_the_minimum_and_fitted(tmp_path: Path) -> None:
    result, backend, ctx = _run(tmp_path, m.MusicRequest(description="calm ambient pad", duration_s=6.5, bpm=80))
    assert backend.calls[0]["duration_s"] == 10.0 and backend.calls[0]["bpm"] == 80 and backend.calls[0]["seed"] == 42
    info = probe(asyncio.run(ctx.read_artifact(result.audio)))
    assert result.sample_rate == 48_000 and info.sample_rate == 48_000 and info.channels == 1
    assert abs(result.duration_s - 6.5) < 1e-3 and abs(info.duration_s - 6.5) < 0.01


def test_vocals_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="instrumental"):
        _run(tmp_path, m.MusicRequest(description="pop song", duration_s=20, instrumental=False))
