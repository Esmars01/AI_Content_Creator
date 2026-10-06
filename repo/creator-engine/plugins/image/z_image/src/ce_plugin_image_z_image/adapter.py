"""Z-Image-Turbo adapter: `image.generate`. Plans the generation size (requested aspect, within the
manifest's pixel budget, multiples of 16), seeds the generator with the attempt seed, and resizes
the result to the requested frame."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit import EngineAdapter
from ce_plugin_kit.media import plan_size, resize_image

__all__ = ["ZImageAdapter", "generation_args"]


def generation_args(request: m.ImageGenerateRequest, defaults: dict[str, Any], seed: int) -> dict[str, Any]:
    width, height = plan_size(
        request.width,
        request.height,
        multiple=int(defaults.get("multiple", 16)),
        max_pixels=int(defaults.get("max_pixels", 1024 * 1024)),
    )
    return {
        "prompt": request.prompt,
        "negative_prompt": request.negative or None,
        "width": width,
        "height": height,
        "num_inference_steps": int(request.steps or defaults.get("steps", 9)),
        "guidance_scale": float(defaults.get("guidance_scale", 0.0)),
        "seed": int(seed) % (2**63),
    }


class ZImageAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_image_z_image.backend import ZImageBackend

        assert self.paths is not None
        return ZImageBackend(self.paths.model(), dtype=str(self.defaults.get("dtype", "bf16")))

    async def run_image_generate(self, request: m.ImageGenerateRequest, ctx: RunContext) -> m.ImageResult:
        backend = self.require_backend()
        work = self.scratch(ctx, "generate")
        args = generation_args(request, self.defaults, ctx.seed)
        raw = await self.call(ctx, backend.generate, args, work / "raw.png")
        out = resize_image(raw, work / "image.png", request.width, request.height)
        ref = await ctx.write_artifact(
            out, "image", {"engine": "z_image_turbo", "args": args}, role="image", mime="image/png"
        )
        return m.ImageResult(image=ref, width=request.width, height=request.height)
