"""The three Wan 2.2 adapters: A14B (t2v and i2v, LightX2V) and TI2V-5B (diffusers)."""

from __future__ import annotations

from typing import Any

from ce_contracts.common import LoadContext

from ce_plugin_video_wan22.common import WanVideoAdapter

__all__ = ["Wan22A14BI2V", "Wan22A14BT2V", "Wan22TI2V5B"]


class _LightX2V(WanVideoAdapter):
    task = "t2v"

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_video_wan22.lightx2v_backend import LightX2VWanBackend

        assert self.paths is not None
        return LightX2VWanBackend(
            model_dir=self.paths.model(),
            high_lora=self.paths.dependency("distill_lora") / str(self.defaults["lora_high"]),
            low_lora=self.paths.dependency("distill_lora") / str(self.defaults["lora_low"]),
            task=self.task,
            defaults=self.defaults,
        )


class Wan22A14BT2V(_LightX2V):
    engine_name = "wan22_a14b_t2v"
    task = "t2v"


class Wan22A14BI2V(_LightX2V):
    engine_name = "wan22_a14b_i2v"
    task = "i2v"


class Wan22TI2V5B(WanVideoAdapter):
    engine_name = "wan22_ti2v_5b"

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_video_wan22.diffusers_backend import DiffusersWanBackend

        assert self.paths is not None
        return DiffusersWanBackend(model_dir=self.paths.model(), defaults=self.defaults)
