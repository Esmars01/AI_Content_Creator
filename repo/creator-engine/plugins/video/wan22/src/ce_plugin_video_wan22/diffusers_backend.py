"""The real Wan 2.2 TI2V-5B backend through diffusers (`WanPipeline` / `WanImageToVideoPipeline`
from the official `-Diffusers` checkpoint). Imported only on a `wan` worker. **Untested on a GPU**
(rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_video_wan22.common import WanParams

__all__ = ["DiffusersWanBackend"]


class DiffusersWanBackend:
    def __init__(self, *, model_dir: Path, defaults: dict[str, Any]) -> None:
        import torch
        from diffusers import AutoencoderKLWan, WanImageToVideoPipeline, WanPipeline

        self.torch = torch
        vae = AutoencoderKLWan.from_pretrained(
            str(model_dir), subfolder="vae", torch_dtype=torch.float32, local_files_only=True
        )
        self.t2v = WanPipeline.from_pretrained(
            str(model_dir), vae=vae, torch_dtype=torch.bfloat16, local_files_only=True
        )
        self.i2v = WanImageToVideoPipeline(**self.t2v.components)  # shares weights with the text pipeline
        if defaults.get("cpu_offload", False):
            self.t2v.enable_model_cpu_offload()
        else:
            self.t2v.to("cuda")

    def generate(self, params: WanParams, image: Path | None, out: Path) -> Path:
        from diffusers.utils import export_to_video, load_image

        generator = self.torch.Generator("cuda").manual_seed(params.seed)
        common: dict[str, Any] = {
            "prompt": params.prompt,
            "negative_prompt": params.negative or None,
            "height": params.height,
            "width": params.width,
            "num_frames": params.num_frames,
            "num_inference_steps": params.steps,
            "guidance_scale": params.guidance_scale,
            "generator": generator,
        }
        if params.task == "i2v" and image is not None:
            frames = self.i2v(image=load_image(str(image)), **common).frames[0]
        else:
            frames = self.t2v(**common).frames[0]
        export_to_video(frames, str(out), fps=params.fps)
        return out

    def close(self) -> None:
        self.t2v = None
        self.i2v = None
        self.torch.cuda.empty_cache()
