"""The real MOSS-SoundEffect v2.0 backend (`moss_soundeffect_v2.MossSoundEffectPipeline` from the
OpenMOSS/MOSS-TTS repository at the pinned commit, as in the model card): imported only on an
`audio` worker. **Untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["MossSfxBackend"]


class MossSfxBackend:
    def __init__(self, *, model_dir: Path, defaults: dict[str, Any]) -> None:
        import torch
        from moss_soundeffect_v2 import MossSoundEffectPipeline

        self.torch = torch
        self.pipe = MossSoundEffectPipeline.from_pretrained(str(model_dir), torch_dtype=torch.bfloat16, device="cuda")
        if int(self.pipe.sample_rate) != 48_000:
            raise RuntimeError(f"MOSS-SoundEffect returned {self.pipe.sample_rate} Hz; the adapter expects 48000")

    def generate(self, options: dict[str, Any]) -> np.ndarray:
        audio = self.pipe(
            prompt=str(options["prompt"]), seconds=float(options["seconds"]),
            num_inference_steps=int(options["steps"]), cfg_scale=float(options["cfg_scale"]),
            sigma_shift=float(options["sigma_shift"]), seed=int(options["seed"]),
        )  # fmt: skip
        return audio[0].float().cpu().numpy()  # (channels, samples)

    def close(self) -> None:
        self.pipe = None
        self.torch.cuda.empty_cache()
