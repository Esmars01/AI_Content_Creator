"""Qwen-Image-Edit-2511 adapter (Phase 8): edit arguments, references and the adapter on its CPU
stand-in. Nothing here runs the model (`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import PLACEHOLDER, load_with_backend, make_media

from ce_plugin_image_qwen_edit.adapter import edit_args
from ce_plugin_image_qwen_edit.testing import FakeQwenEditBackend

PLUGIN = discover(app_env="test", include_mocks=True).get("qwen_image_edit")


def test_readme_settings_and_one_megapixel_planning() -> None:
    request = m.ImageEditRequest(image=PLACEHOLDER, prompt="same person, grey hoodie", width=1080, height=1920)
    args = edit_args(request, PLUGIN.manifest.defaults, seed=3)
    assert (args["num_inference_steps"], args["true_cfg_scale"], args["guidance_scale"]) == (40, 4.0, 1.0)
    assert args["negative_prompt"] == " " and args["width"] * args["height"] <= 1024 * 1024


def test_references_are_capped_and_the_source_comes_first(tmp_path: Path) -> None:
    async def go() -> tuple[m.ImageResult, FakeQwenEditBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=1)
        refs = await make_media(ctx, tmp_path, src="image", a="image", b="image", c="image", d="image")
        backend = FakeQwenEditBackend()
        adapter = await load_with_backend(PLUGIN, backend, tmp_path)
        request = m.ImageEditRequest(
            image=refs["src"],
            references=[refs["a"], refs["b"], refs["c"], refs["d"]],
            prompt="x",
            width=512,
            height=512,
        )
        return await adapter.run("image.edit", request, ctx), backend, ctx  # type: ignore[return-value]

    result, backend, ctx = asyncio.run(go())
    assert backend.calls[0][1] == 1 + PLUGIN.manifest.defaults["max_references"]
    assert result.image.meta["references"] == 3
    assert (probe(asyncio.run(ctx.read_artifact(result.image))).width, result.height) == (512, 512)


def test_masks_are_refused_not_ignored(tmp_path: Path) -> None:
    async def go() -> None:
        ctx = LocalRunContext(tmp_path / "ctx")
        adapter = await load_with_backend(PLUGIN, FakeQwenEditBackend(), tmp_path)
        request = m.ImageEditRequest(image=PLACEHOLDER, mask=PLACEHOLDER, prompt="x", width=64, height=64)
        await adapter.run("image.edit", request, ctx)

    with pytest.raises(ValueError, match="masks are not supported"):
        asyncio.run(go())
