"""The emotion2vec+ backend (FunASR `AutoModel`), loaded in the asr family image. Never imported on
CPU-only hosts: the contract tests use `testing.FakeEmotionBackend` (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["Emotion2VecBackend"]


def _label(raw: str) -> str:
    # tokens are bilingual ("生气/angry"); the English half is the label
    return raw.split("/")[-1].strip().lower().replace("<unk>", "unknown")


class Emotion2VecBackend:
    def __init__(self, *, model_dir: Path, scratch: Path | str) -> None:
        from funasr import AutoModel  # type: ignore[import-not-found]

        self.model: Any = AutoModel(model=str(model_dir), disable_update=True)
        self.scratch = scratch

    def classify(self, samples16k: np.ndarray) -> dict[str, float]:
        result = self.model.generate(samples16k.astype(np.float32), granularity="utterance", extract_embedding=False)
        item = result[0] if isinstance(result, list) else result
        return {_label(str(label)): float(score) for label, score in zip(item["labels"], item["scores"], strict=False)}
