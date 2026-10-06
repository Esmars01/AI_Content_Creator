"""DINOv2 embeddings: resize the shortest edge (bicubic), centre-crop, normalize, CLS token."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import ImageEmbedder
from ce_contracts.models import EmbeddingResult, ImageEmbedRequest
from PIL import Image

__all__ = ["DinoV2Embed", "preprocess"]


def preprocess(image: Image.Image, config: dict[str, Any], mask: Image.Image | None = None) -> np.ndarray:
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    if mask is not None:  # white = exclude: fill with the mean colour of what remains
        m = np.asarray(mask.convert("L").resize(image.size), dtype=np.float32) > 127
        keep = rgb[~m]
        rgb[m] = keep.mean(axis=0) if keep.size else 127.0
        image = Image.fromarray(rgb.astype(np.uint8))
    short = int(config.get("size", {}).get("shortest_edge", 256))
    crop = config.get("crop_size", {"height": 224, "width": 224})
    w, h = image.size
    scale = short / min(w, h)
    resized = image.convert("RGB").resize(
        (max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.BICUBIC
    )
    cw, ch = int(crop["width"]), int(crop["height"])
    left, top = (resized.width - cw) // 2, (resized.height - ch) // 2
    arr = np.asarray(resized.crop((left, top, left + cw, top + ch)), dtype=np.float32) * float(
        config.get("rescale_factor", 1 / 255)
    )
    mean = np.asarray(config.get("image_mean", [0.485, 0.456, 0.406]), dtype=np.float32)
    std = np.asarray(config.get("image_std", [0.229, 0.224, 0.225]), dtype=np.float32)
    return ((arr - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)


class DinoV2Embed(ImageEmbedder):
    seconds_per_unit = 0.2

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self._session: Any = None
        self._pre: dict[str, Any] = {}

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        root = Path(ctx.model_cache_dir)
        model = root / str(self.config["model_file"])
        if not model.is_file():
            raise FileNotFoundError(f"{model} is missing: run `make fetch-cpu-assets`")
        self._pre = json.loads((root / str(self.config["preprocessor_file"])).read_text(encoding="utf-8"))

        def open_session() -> Any:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            return ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])

        self._session = await asyncio.to_thread(open_session)

    def embed_image(self, image: Image.Image, mask: Image.Image | None = None) -> list[float]:
        (hidden,) = self._session.run(None, {"pixel_values": preprocess(image, self._pre, mask)})
        cls = np.asarray(hidden)[0, 0]
        cls = cls / (np.linalg.norm(cls) or 1.0)
        return [round(float(x), 6) for x in cls]

    async def run_image_embed(self, request: ImageEmbedRequest, ctx: RunContext) -> EmbeddingResult:
        if self._session is None:
            raise RuntimeError("dinov2_embed is not loaded")
        vectors = []
        for index, ref in enumerate(request.images):
            image = Image.open(await ctx.read_artifact(ref))
            mask = Image.open(await ctx.read_artifact(request.masks[index])) if index < len(request.masks) else None
            vectors.append(await asyncio.to_thread(self.embed_image, image, mask))
        return EmbeddingResult(vectors=vectors, dim=len(vectors[0]) if vectors else 0)
