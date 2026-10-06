"""The real Z-Image-Turbo backend (diffusers `ZImagePipeline`, as in the model README): imported only
on an `image` worker. Implemented against diffusers 0.40.0; **untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["ZImageBackend"]


class ZImageBackend:
    def __init__(self, model_dir: Path, *, dtype: str = "bf16") -> None:
        import torch
        from ce_plugin_kit.gpu import torch_dtype
        from diffusers import ZImagePipeline

        self.torch = torch
        self.pipe = ZImagePipeline.from_pretrained(
            str(model_dir), torch_dtype=torch_dtype(dtype), local_files_only=True
        )
        self.pipe.to("cuda")

    def generate(self, args: dict[str, Any], out: Path) -> Path:
        seed = int(args.pop("seed"))
        generator = self.torch.Generator("cuda").manual_seed(seed)
        image = self.pipe(generator=generator, **args).images[0]
        image.save(out)
        return out

    def close(self) -> None:
        self.pipe = None
        self.torch.cuda.empty_cache()
