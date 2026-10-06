"""DINOv2 image embeddings (world identity QC, Phase 7): the same picture is identical, a nearby
crop is closer than an unrelated picture, and masked regions are ignored. Needs the ONNX export
(`make fetch-cpu-assets`); skips with that reason otherwise."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.manifest import missing_assets
from ce_contracts.plugins import discover
from PIL import Image

ROOT = Path(__file__).resolve().parents[4]
CACHE = Path(os.environ.get("CE_TEST_MODEL_CACHE", ROOT / ".cache" / "models"))


def _cos(a: list[float], b: list[float]) -> float:
    x, y = np.asarray(a), np.asarray(b)
    return float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))


def test_embeddings_rank_similar_pictures(tmp_path: Path) -> None:
    registry = discover(app_env="test", include_mocks=True)
    missing = missing_assets(registry.get("dinov2_embed").manifest, CACHE)
    if missing:
        pytest.skip(f"dinov2_embed: assets missing under {CACHE} ({missing[0]}); run `make fetch-cpu-assets`")
    from ce_plugins_mock._media import draw_face_image

    room = tmp_path / "room.png"
    draw_face_image(room, width=640, height=640, seed=11, lines=["desk"], figure=False)
    shifted = tmp_path / "shifted.png"
    Image.open(room).crop((24, 24, 640, 640)).resize((640, 640)).save(shifted)
    noise = tmp_path / "noise.png"
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (640, 640, 3), dtype=np.uint8)).save(noise)

    async def run() -> list[list[float]]:
        adapter = registry.get("dinov2_embed").adapter()
        await adapter.load(LoadContext(model_cache_dir=str(CACHE), scratch_dir=str(tmp_path / "s"), app_env="test"))
        ctx = LocalRunContext(tmp_path / "ctx")
        refs = [await ctx.put_file(p, "image") for p in (room, room, shifted, noise)]
        result = await adapter.run("image.embed", m.ImageEmbedRequest(images=refs), ctx)
        return result.vectors

    same_a, same_b, near, far = asyncio.run(run())
    assert _cos(same_a, same_b) == pytest.approx(1.0, abs=1e-5)
    assert _cos(same_a, near) > _cos(same_a, far) + 0.1
