"""InfiniteTalk adapter: `avatar.a2v` (keyframe + speech → talking video).

The adapter prepares the conditioning image (the keyframe, or the last frame of the previous chunk
for a continuation), measures the audio, builds the generation parameters (`params.py`), asks the
backend for the clip, then conforms the result: trimmed to the audio, scaled and center-cropped to
the requested size, with the request's own audio muxed in so the dialogue stays bit-exact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit import EngineAdapter
from ce_plugin_kit.media import conform_clip, probe

from ce_plugin_avatar_infinitetalk.params import build_params

__all__ = ["InfiniteTalkAdapter"]


class InfiniteTalkAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_avatar_infinitetalk.backend import InfiniteTalkBackend  # heavy imports live there

        assert self.paths is not None
        return InfiniteTalkBackend(
            base_dir=self.paths.dependency("base_weights"),
            wav2vec_dir=self.paths.dependency("audio_encoder"),
            infinitetalk_file=self.paths.model() / "single" / "infinitetalk.safetensors",
            quant_file=self.paths.model() / "quant_models" / "infinitetalk_single_fp8.safetensors",
            lora_file=self.paths.dependency("distill_lora")
            / "loras"
            / "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
            defaults=self.defaults,
        )

    async def run_avatar_a2v(self, request: m.AvatarRequest, ctx: RunContext) -> m.AvatarResult:
        backend = self.require_backend()
        work = self.scratch(ctx, f"a2v-{request.chunk_index}")
        audio = await ctx.read_artifact(request.audio)
        image = await self.start_image(ctx, request, work)
        audio_s = probe(audio).duration_s
        engine = dict(request.engine or {})
        prompt = str(
            engine.get("prompt") or " ".join(p for p in (self.defaults.get("verbal_cue", ""), request.prompt) if p)
        )
        params = build_params(
            width=request.width,
            height=request.height,
            audio_seconds=audio_s,
            seed=ctx.seed,
            prompt=prompt,
            defaults=self.defaults,
            engine=engine,
            # an OOM retry without a larger GPU class runs with the low-memory settings (§25); they
            # offload weights but do not change the output, so the cache key stays the same
            low_memory=bool(getattr(ctx, "options", {}).get("low_memory")),
        )
        await ctx.progress(0.05, f"generating {params.max_frames} frames ({params.mode})")
        raw = await self.call(ctx, backend.generate, params, image, audio, work)
        out = conform_clip(
            Path(raw), work / "take.mp4", width=request.width, height=request.height, duration_s=audio_s, audio=audio
        )
        info = probe(out)
        ref = await ctx.write_artifact(
            out,
            "video",
            {"engine": "infinitetalk", "params": params.as_dict(), "duration_s": info.duration_s},
            role="video",
            mime="video/mp4",
        )
        await ctx.progress(1.0, "done")
        return m.AvatarResult(
            video=ref, duration_s=info.duration_s, fps=float(params.fps), width=info.width, height=info.height
        )
