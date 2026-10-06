"""Mock text embedding: hashed bag-of-words vectors (deterministic, not semantic)."""

from __future__ import annotations

import hashlib
import math

from ce_contracts.common import RunContext
from ce_contracts.interfaces import EmbeddingEngine
from ce_contracts.models import EmbeddingResult, TextEmbedRequest

from ce_plugins_mock._base import MockAdapter

__all__ = ["MockEmbed", "hashed_vector"]


def hashed_vector(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for token in text.lower().split():
        h = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16)
        vector[h % dim] += 1.0 if (h >> 8) % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]


class MockEmbed(MockAdapter, EmbeddingEngine):
    async def run_embed_text(self, request: TextEmbedRequest, ctx: RunContext) -> EmbeddingResult:
        dim = int(request.labels.get("dim", self.config.get("dim", 64)))
        return EmbeddingResult(vectors=[hashed_vector(t, dim) for t in request.texts], dim=dim)
