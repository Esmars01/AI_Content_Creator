"""Z-Image-Turbo adapter (Phase 8): generation arguments and the adapter on its CPU stand-in.
Nothing here runs the model (`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import load_with_backend

from ce_plugin_image_z_image.adapter import generation_args
from ce_plugin_image_z_image.testing import FakeZImageBackend

PLUGIN = discover(app_env="test", include_mocks=True).get("z_image_turbo")


def test_turbo_settings_and_size_planning() -> None:
    request = m.ImageGenerateRequest(prompt="a desk by a window", width=1080, height=1920)
    args = generation_args(request, PLUGIN.manifest.defaults, seed=2**70 + 9)
    assert (args["num_inference_steps"], args["guidance_scale"]) == (9, 0.0)  # README Turbo settings
    assert args["width"] % 16 == 0 and args["height"] % 16 == 0 and args["width"] * args["height"] <= 1536 * 1536
    assert abs(args["width"] / args["height"] - 1080 / 1920) < 0.02
    assert args["seed"] == (2**70 + 9) % 2**63 and args["negative_prompt"] is None


def test_the_adapter_resizes_to_the_requested_frame(tmp_path: Path) -> None:
    async def go() -> tuple[m.ImageResult, FakeZImageBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=5)
        backend = FakeZImageBackend()
        adapter = await load_with_backend(PLUGIN, backend, tmp_path)
        request = m.ImageGenerateRequest(prompt="portrait", width=720, height=1280, steps=12)
        return await adapter.run("image.generate", request, ctx), backend, ctx  # type: ignore[return-value]

    result, backend, ctx = asyncio.run(go())
    info = probe(asyncio.run(ctx.read_artifact(result.image)))
    assert (info.width, info.height) == (720, 1280) == (result.width, result.height)
    assert backend.calls[0]["num_inference_steps"] == 12  # an explicit request wins over the default
