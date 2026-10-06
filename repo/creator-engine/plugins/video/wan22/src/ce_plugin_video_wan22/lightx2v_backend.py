"""The real Wan 2.2 A14B backend through LightX2V (`LightX2VPipeline`, examples/README.md at the
pinned commit): the MoE base model with the high- and low-noise 4-step distillation LoRAs merged at
load, CFG disabled. Imported only on a `wan` worker (variant `lightx2v`). **Untested on a GPU**
(rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_video_wan22.common import WanParams

__all__ = ["LightX2VWanBackend"]


class LightX2VWanBackend:
    def __init__(
        self, *, model_dir: Path, high_lora: Path, low_lora: Path, task: str, defaults: dict[str, Any]
    ) -> None:
        from lightx2v import LightX2VPipeline

        self.pipe = LightX2VPipeline(model_path=str(model_dir), model_cls="wan2.2_moe", task=task)
        if defaults.get("cpu_offload", False):
            self.pipe.enable_offload(cpu_offload=True, offload_granularity="block")
        self.pipe.enable_lora(
            [
                {"name": "high_noise_model", "path": str(high_lora), "strength": 1.0},
                {"name": "low_noise_model", "path": str(low_lora), "strength": 1.0},
            ],
            lora_dynamic_apply=False,
        )
        sizes = dict(defaults.get("sizes") or {})
        first = next(iter(sizes.values()), [480, 832])
        self.pipe.create_generator(
            attn_mode=str(defaults.get("attn_mode", "flash_attn2")),
            infer_steps=int(defaults.get("steps", 4)),
            num_frames=int(defaults.get("max_frames", 81)),
            size=(int(first[1]), int(first[0])),
            guidance_scale=float(defaults.get("guidance_scale", 1.0)),
            sample_shift=float(defaults.get("shift", 5.0)),
            fps=int(defaults.get("fps", 16)),
            boundary_step_index=int(defaults.get("boundary_step_index", 2)),
            denoising_step_list=tuple(defaults.get("denoising_step_list", (1000, 750, 500, 250))),
        )

    def generate(self, params: WanParams, image: Path | None, out: Path) -> Path:
        self.pipe.generate(
            seed=params.seed,
            prompt=params.prompt,
            negative_prompt=params.negative,
            save_result_path=str(out),
            image_path=str(image) if image is not None else None,
            size=(params.height, params.width),
            num_frames=params.num_frames,
        )
        return out

    def close(self) -> None:
        self.pipe = None
