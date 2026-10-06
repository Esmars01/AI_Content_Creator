"""MOSS-SoundEffect adapter (Phase 8): one-shots fitted, ambiences looped past one generation, the
30 s one-shot limit, on the CPU stand-in. No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import load_with_backend

from ce_plugin_audio_moss_sfx.testing import FakeMossSfxBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def _run(tmp: Path, request: m.SfxRequest) -> tuple[m.AudioResult, FakeMossSfxBackend, LocalRunContext]:
    async def go() -> tuple[m.AudioResult, FakeMossSfxBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp / "ctx", seed=5)
        backend = FakeMossSfxBackend()
        adapter = await load_with_backend(REGISTRY.get("moss_sfx_v2"), backend, tmp)
        return await adapter.run("audio.sfx", request, ctx), backend, ctx  # type: ignore[return-value]

    return asyncio.run(go())


def test_one_shot_is_fitted(tmp_path: Path) -> None:
    result, backend, ctx = _run(tmp_path, m.SfxRequest(description="a door slams", duration_s=1.2))
    assert backend.calls[0]["seconds"] == 1.5 and result.audio.meta["kind"] == "one_shot"
    assert abs(probe(asyncio.run(ctx.read_artifact(result.audio))).duration_s - 1.2) < 0.01


def test_long_room_tone_is_one_call_looped(tmp_path: Path) -> None:
    request = m.SfxRequest(
        description="quiet office room tone, air conditioning hum", duration_s=75.0, labels={"kind": "ambience"}
    )
    result, backend, _ = _run(tmp_path, request)
    assert len(backend.calls) == 1 and backend.calls[0]["seconds"] == 30.0
    assert result.audio.meta["looped"] is True and abs(result.duration_s - 75.0) < 1e-3


def test_long_one_shot_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one-shot"):
        _run(tmp_path, m.SfxRequest(description="explosion", duration_s=45.0, labels={"kind": "one_shot"}))
