"""ONNX text encoder for `embed.text`: tokenize (the model's `tokenizer.json`), run the encoder in
batches, pool (CLS or attention-masked mean), L2-normalize, check the dimension.

Configuration (manifest `defaults`, then the load context): `model_file`, `tokenizer_file`,
`max_length`, `batch_size`, `pooling` (`cls` | `mean`), `prefix` (prepended to every text, e.g. a
model's passage prefix), `dim` (the expected vector size, `EMBEDDING_DIM`).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import EmbeddingEngine
from ce_contracts.models import EmbeddingResult, TextEmbedRequest

__all__ = ["Backend", "TextEmbed", "pool"]

# (input_ids, attention_mask) → last hidden states [batch, tokens, dim]
Backend = Callable[[np.ndarray, np.ndarray], np.ndarray]


def pool(hidden: np.ndarray, mask: np.ndarray, how: str) -> np.ndarray:
    """[batch, dim] L2-normalized sentence vectors."""
    if how == "cls":
        vectors = hidden[:, 0, :]
    elif how == "mean":
        weights = mask[..., None].astype(np.float32)
        vectors = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
    else:
        raise ValueError(f"unknown pooling {how!r}")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


class TextEmbed(EmbeddingEngine):
    seconds_per_unit = 0.05

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = {
            "max_length": 512,
            "batch_size": 16,
            "pooling": "cls",
            "prefix": "",
            **dict(getattr(manifest, "defaults", {}) or {}),
        }
        self._backend: Backend | None = None
        self._tokenizer: Any = None

    def use_backend(self, backend: Backend, tokenizer: Any) -> None:
        """A stand-in encoder and tokenizer (tests)."""
        self._backend, self._tokenizer = backend, tokenizer

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        if self._backend is not None:
            return
        root = Path(ctx.model_cache_dir)
        model, vocab = root / str(self.config["model_file"]), root / str(self.config["tokenizer_file"])
        for path in (model, vocab):
            if not path.is_file():
                raise FileNotFoundError(f"{path} is missing: the text embedding model is not fetched")

        def open_model() -> tuple[Backend, Any]:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
            names = {i.name for i in session.get_inputs()}

            def run(ids: np.ndarray, mask: np.ndarray) -> np.ndarray:
                feeds = {"input_ids": ids, "attention_mask": mask}
                if "token_type_ids" in names:
                    feeds["token_type_ids"] = np.zeros_like(ids)
                return np.asarray(session.run(None, feeds)[0])

            return run, Tokenizer.from_file(str(vocab))

        self._backend, self._tokenizer = await asyncio.to_thread(open_model)

    def _encode(self, texts: list[str]) -> np.ndarray:
        assert self._backend is not None and self._tokenizer is not None
        size, limit = int(self.config["batch_size"]), int(self.config["max_length"])
        prefix = str(self.config.get("prefix") or "")
        out: list[np.ndarray] = []
        for start in range(0, len(texts), size):
            batch = [prefix + t for t in texts[start : start + size]]
            encoded = self._tokenizer.encode_batch(batch)
            longest = min(limit, max(len(e.ids) for e in encoded))
            ids = np.zeros((len(batch), longest), dtype=np.int64)
            mask = np.zeros((len(batch), longest), dtype=np.int64)
            for row, e in enumerate(encoded):
                n = min(longest, len(e.ids))
                ids[row, :n] = e.ids[:n]
                mask[row, :n] = 1
            out.append(pool(self._backend(ids, mask), mask, str(self.config["pooling"])))
        return np.concatenate(out, axis=0)

    async def run_embed_text(self, request: TextEmbedRequest, ctx: RunContext) -> EmbeddingResult:
        if self._backend is None:
            raise RuntimeError("text_embed is not loaded")
        vectors = await asyncio.to_thread(self._encode, list(request.texts))
        dim = int(vectors.shape[1])
        expected = self.config.get("dim")
        if expected is not None and dim != int(expected):
            raise ValueError(f"the model gives {dim}-dimensional vectors; EMBEDDING_DIM is {expected}")
        return EmbeddingResult(vectors=[[round(float(x), 6) for x in v] for v in vectors], dim=dim)
