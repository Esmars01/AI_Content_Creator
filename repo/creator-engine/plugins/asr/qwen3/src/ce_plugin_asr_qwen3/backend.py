"""The real Qwen3-ASR / Qwen3-ForcedAligner backends (`qwen-asr` 0.0.6: `Qwen3ASRModel`,
`Qwen3ForcedAligner`, transformers backend, as in the Qwen3-ASR README): imported only on an
`asr` worker. **Untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["Qwen3ASRBackend", "Qwen3AlignerBackend"]


def _kwargs(defaults: dict[str, Any]) -> dict[str, Any]:
    import torch

    return {
        "dtype": torch.bfloat16,
        "device_map": "cuda:0",
        "attn_implementation": str(defaults.get("attn_implementation", "sdpa")),
    }


class Qwen3AlignerBackend:
    def __init__(self, *, aligner_dir: Path, defaults: dict[str, Any]) -> None:
        import torch
        from qwen_asr import Qwen3ForcedAligner

        self.torch = torch
        self.aligner = Qwen3ForcedAligner.from_pretrained(str(aligner_dir), **_kwargs(defaults))

    def align(self, wav16k: str, text: str, language: str) -> list[tuple[str, float, float]]:
        (result,) = self.aligner.align(audio=wav16k, text=text, language=language)
        return [(item.text, float(item.start_time), float(item.end_time)) for item in result.items]

    def close(self) -> None:
        self.aligner = None
        self.torch.cuda.empty_cache()


class Qwen3ASRBackend(Qwen3AlignerBackend):
    def __init__(self, *, asr_dir: Path, aligner_dir: Path, defaults: dict[str, Any]) -> None:
        from qwen_asr import Qwen3ASRModel

        super().__init__(aligner_dir=aligner_dir, defaults=defaults)
        self.model = Qwen3ASRModel.from_pretrained(
            str(asr_dir),
            max_inference_batch_size=int(defaults.get("max_batch", 8)),
            max_new_tokens=int(defaults.get("max_new_tokens", 1024)),
            **_kwargs(defaults),
        )

    def transcribe(self, wav16k: str, language: str | None) -> tuple[str, str]:
        (result,) = self.model.transcribe(audio=wav16k, context="", language=language)
        return str(result.language or ""), str(result.text or "")

    def close(self) -> None:
        self.model = None
        super().close()
