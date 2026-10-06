"""Qwen-Image-Edit-2511 adapter: `image.edit`. The source image first, then up to
`max_references` reference images (canonical face, wardrobe or world references, §17.2); the
generation size keeps the requested aspect near the pipeline's 1 MP VAE size; the result is resized
to the requested frame. A mask is not part of this engine (inpainting would need another engine)."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit import EngineAdapter
from ce_plugin_kit.media import plan_size, resize_image

__all__ = ["QwenImageEditAdapter", "edit_args"]


def edit_args(request: m.ImageEditRequest, defaults: dict[str, Any], seed: int) -> dict[str, Any]:
    width, height = plan_size(
        request.width,
        request.height,
        multiple=int(defaults.get("multiple", 16)),
        max_pixels=int(defaults.get("max_pixels", 1024 * 1024)),
    )
    return {
        "prompt": request.prompt,
        "negative_prompt": request.negative or str(defaults.get("negative", " ")),
        "width": width,
        "height": height,
        "num_inference_steps": int(defaults.get("steps", 40)),
        "true_cfg_scale": float(defaults.get("true_cfg_scale", 4.0)),
        "guidance_scale": float(defaults.get("guidance_scale", 1.0)),
        "seed": int(seed) % (2**63),
    }


class QwenImageEditAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_image_qwen_edit.backend import QwenImageEditBackend

        assert self.paths is not None
        return QwenImageEditBackend(self.paths.model(), dtype=str(self.defaults.get("dtype", "bf16")))

    async def run_image_edit(self, request: m.ImageEditRequest, ctx: RunContext) -> m.ImageResult:
        backend = self.require_backend()
        if request.mask is not None:
            raise ValueError(
                "qwen_image_edit edits by instruction; masks are not supported (route `inpaint` elsewhere)"
            )
        work = self.scratch(ctx, "edit")
        limit = int(self.defaults.get("max_references", 3))
        images = [await ctx.read_artifact(request.image)]
        images += [await ctx.read_artifact(ref) for ref in request.references[:limit]]
        args = edit_args(request, self.defaults, ctx.seed)
        raw = await self.call(ctx, backend.edit, args, images, work / "raw.png")
        out = resize_image(raw, work / "image.png", request.width, request.height)
        meta = {"engine": "qwen_image_edit", "args": args, "references": len(images) - 1}
        ref = await ctx.write_artifact(out, "image", meta, role="image", mime="image/png")
        return m.ImageResult(image=ref, width=request.width, height=request.height)
