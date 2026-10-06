"""Mock image engine: face-like figures (keyframes, identity candidates) and labelled room plates."""

from __future__ import annotations

import json
from pathlib import Path

from ce_contracts.common import RunContext
from ce_contracts.interfaces import ImageGenerator
from ce_contracts.models import ImageEditRequest, ImageGenerateRequest, ImageResult

from ce_plugins_mock._base import MockAdapter, env_float
from ce_plugins_mock._media import draw_face_image, draw_plate_image, seed_int

__all__ = ["MockImage"]


def _lines(labels: dict[str, str], head: str) -> list[str]:
    keys = ("creator", "wardrobe", "world", "camera_position", "time_of_day", "shot", "state")
    return [head] + [f"{k.replace('_', ' ')}: {labels[k]}" for k in keys if labels.get(k)]


def _elements(labels: dict[str, str]) -> list[tuple[str, float, float]]:
    raw = labels.get("elements_json")
    if not raw:
        return []
    return [(str(e["label"]), float(e["x"]), float(e["y"])) for e in json.loads(raw)]


class MockImage(MockAdapter, ImageGenerator):
    async def run_image_generate(self, request: ImageGenerateRequest, ctx: RunContext) -> ImageResult:
        out = self.workdir(ctx, "generate") / "image.png"
        seed = seed_int(ctx.seed, request.prompt)
        if request.labels.get("kind") == "plate":
            draw_plate_image(
                out,
                width=request.width,
                height=request.height,
                seed=seed,
                lines=_lines(request.labels, "MOCK WORLD PLATE"),
                elements=_elements(request.labels),
            )
        else:
            draw_face_image(
                out, width=request.width, height=request.height, seed=seed, lines=_lines(request.labels, "MOCK IMAGE")
            )
        ref = await self.write(
            ctx, out, "image", role="image", mime="image/png", width=request.width, height=request.height
        )
        return ImageResult(image=ref, width=request.width, height=request.height)

    async def run_image_edit(self, request: ImageEditRequest, ctx: RunContext) -> ImageResult:
        """Composition mock: the figure (from the references) drawn over the input image (a world plate)."""
        base: Path = await ctx.read_artifact(request.image)
        out = self.workdir(ctx, "edit") / "image.png"
        seed = seed_int(ctx.seed, request.prompt, *(r.sha256 for r in request.references))
        # Failure injection (`MOCK_IMAGE_FACELESS_RATE`): keyframes without a figure, so real face
        # analyzers on the mock video find no face (§40 Phase 7 honesty check).
        faceless = request.labels.get("kind") == "keyframe" and (seed % 10_000) / 10_000 < env_float(
            "MOCK_IMAGE_FACELESS_RATE", 0.0
        )
        draw_face_image(
            out,
            width=request.width,
            height=request.height,
            seed=seed,
            lines=_lines(request.labels, "MOCK KEYFRAME (FACELESS)" if faceless else "MOCK KEYFRAME"),
            background=base,
            expression=request.labels.get("expression", "neutral"),
            figure=not faceless,
        )
        ref = await self.write(
            ctx, out, "image", role="image", mime="image/png", width=request.width, height=request.height
        )
        return ImageResult(image=ref, width=request.width, height=request.height)
