"""The real Qwen-Image-Edit-2511 backend (diffusers `QwenImageEditPlusPipeline`, as in the Qwen-Image
README): imported only on an `image` worker. Implemented against diffusers 0.40.0; **untested on a
GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["QwenImageEditBackend"]


class QwenImageEditBackend:
    def __init__(self, model_dir: Path, *, dtype: str = "bf16") -> None:
        import torch
        from ce_plugin_kit.gpu import torch_dtype
        from diffusers import QwenImageEditPlusPipeline

        self.torch = torch
        self.pipe = QwenImageEditPlusPipeline.from_pretrained(
            str(model_dir), torch_dtype=torch_dtype(dtype), local_files_only=True
        )
        self.pipe.to("cuda")
        self.pipe.set_progress_bar_config(disable=True)

    def edit(self, args: dict[str, Any], images: list[Path], out: Path) -> Path:
        from PIL import Image

        seed = int(args.pop("seed"))
        pictures = [Image.open(p).convert("RGB") for p in images]
        with self.torch.inference_mode():
            result = self.pipe(
                image=pictures,
                generator=self.torch.manual_seed(seed),
                num_images_per_prompt=1,
                **args,
            ).images[0]
        result.save(out)
        return out

    def close(self) -> None:
        self.pipe = None
        self.torch.cuda.empty_cache()
