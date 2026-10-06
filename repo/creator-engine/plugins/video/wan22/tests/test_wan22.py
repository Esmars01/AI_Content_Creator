"""Wan 2.2 adapters (Phase 8): frame and bucket planning, and the adapter code on the CPU stand-in.
Nothing here runs a model (`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import load_with_backend, make_media

from ce_plugin_video_wan22.common import frames_for, plan
from ce_plugin_video_wan22.testing import FakeWanBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_frame_and_bucket_planning() -> None:
    a14b = REGISTRY.get("wan22_a14b_t2v").manifest.defaults
    request = m.VideoGenerateRequest(prompt="city at dusk", duration_s=4.0, fps=25, width=405, height=720)
    p = plan(request, a14b, task="t2v", seed=9)
    assert (p.width, p.height, p.fps, p.num_frames) == (480, 832, 16, 65)  # 64 frames → 4n+1 = 65
    assert (p.steps, p.guidance_scale) == (4, 1.0)  # distilled, CFG off
    long = plan(request.model_copy(update={"duration_s": 30.0, "width": 1280, "height": 720}), a14b, task="t2v", seed=9)
    assert (long.width, long.height, long.num_frames) == (1280, 720, 161)  # capped at 10 s
    five = REGISTRY.get("wan22_ti2v_5b").manifest.defaults
    q = plan(request, five, task="i2v", seed=9)
    assert (q.width, q.height, q.fps, q.num_frames) == (704, 1280, 24, 97)
    assert frames_for(5.0, 16) == 81


@pytest.mark.parametrize("adapter_id", ["wan22_a14b_t2v", "wan22_a14b_i2v", "wan22_ti2v_5b"])
def test_the_adapter_conforms_the_clip(adapter_id: str, tmp_path: Path) -> None:
    plugin = REGISTRY.get(adapter_id)
    capability = plugin.manifest.capabilities[-1].id

    async def go() -> tuple[Any, FakeWanBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=2)
        refs = await make_media(ctx, tmp_path, plate="image")
        backend = FakeWanBackend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        request = m.VideoGenerateRequest(
            prompt="desk", duration_s=2.0, fps=25, width=360, height=640,
            first_frame=refs["plate"] if capability == "video.i2v" else None,
        )  # fmt: skip
        return await adapter.run(capability, request, ctx), backend, ctx

    result, backend, ctx = asyncio.run(go())
    info = probe(asyncio.run(ctx.read_artifact(result.video)))
    assert (info.width, info.height) == (360, 640) and abs(info.duration_s - 2.0) < 0.15
    params, image = backend.calls[0]
    assert result.fps == params.fps and (image is not None) == (capability == "video.i2v")


def test_i2v_without_a_first_frame_is_refused(tmp_path: Path) -> None:
    plugin = REGISTRY.get("wan22_a14b_i2v")

    async def go() -> None:
        adapter = await load_with_backend(plugin, FakeWanBackend(), tmp_path)
        request = m.VideoGenerateRequest(prompt="x", duration_s=1, fps=16, width=64, height=64)
        await adapter.run("video.i2v", request, LocalRunContext(tmp_path / "c"))

    with pytest.raises(ValueError, match="first_frame"):
        asyncio.run(go())
