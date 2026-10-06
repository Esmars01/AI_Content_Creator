"""The ONNX text encoder adapter on a stand-in backend: batching with padding masks, CLS and mean
pooling, L2 normalization, the dimension check, the prefix, and the missing-model error."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.models import TextEmbedRequest
from ce_plugin_text_embed.adapter import TextEmbed, pool
from ce_plugin_text_embed.testing import StandInTokenizer, standin_encoder

MANIFEST = SimpleNamespace(
    id="text_embed",
    defaults={"model_file": "m/model.onnx", "tokenizer_file": "m/tokenizer.json", "batch_size": 2},
    capabilities=[SimpleNamespace(id="embed.text")],
)


def test_pooling_masks_padding_and_normalizes() -> None:
    hidden = np.array([[[3.0, 4.0], [1.0, 0.0], [100.0, 100.0]]])
    mask = np.array([[1, 1, 0]])
    assert np.allclose(pool(hidden, mask, "cls"), [[0.6, 0.8]])
    mean = pool(hidden, mask, "mean")  # the padded token does not count
    assert np.allclose(mean, np.array([[2.0, 2.0]]) / np.sqrt(8))
    with pytest.raises(ValueError):
        pool(hidden, mask, "max")


async def _embed(tmp_path: Path, texts: list[str], **config: object) -> list[list[float]]:
    adapter = TextEmbed(MANIFEST)  # type: ignore[arg-type]
    adapter.use_backend(standin_encoder(16), StandInTokenizer())  # type: ignore[arg-type]
    await adapter.load(
        LoadContext(model_cache_dir=str(tmp_path), scratch_dir=str(tmp_path), app_env="test", config=config)
    )
    result = await adapter.run("embed.text", TextEmbedRequest(texts=texts), LocalRunContext(tmp_path))
    return result.vectors  # type: ignore[attr-defined,no-any-return]


async def test_vectors_are_unit_length_and_batches_agree(tmp_path: Path) -> None:
    texts = ["Remote work is overrated.", "remote work is overrated", "I bake bread on Sundays.", "x"]
    vectors = np.array(await _embed(tmp_path, texts, dim=16))
    assert vectors.shape == (4, 16) and np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    alone = np.array(await _embed(tmp_path, texts[2:3], dim=16))
    assert np.allclose(alone[0], vectors[2], atol=1e-5)  # padding in a batch changes nothing
    near, far = float(vectors[0] @ vectors[1]), float(vectors[0] @ vectors[2])
    assert near > far
    prefixed = np.array(await _embed(tmp_path, texts[:1], prefix="passage: "))
    assert not np.allclose(prefixed[0], vectors[0])


async def test_a_wrong_dimension_or_a_missing_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="EMBEDDING_DIM is 1024"):
        await _embed(tmp_path, ["a"], dim=1024)
    adapter = TextEmbed(MANIFEST)  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError, match="not fetched"):
        await adapter.load(LoadContext(model_cache_dir=str(tmp_path), scratch_dir=str(tmp_path), app_env="test"))
