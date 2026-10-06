"""MOSS-SoundEffect v2.0 adapter (`audio.sfx`, §22 SFX and room tone).

One call generates up to 30 s (the pipeline's `max_inference_seconds`). One-shots (`kind` label
`one_shot`, or anything up to 30 s) are fitted to the exact duration with a short fade-out;
ambiences longer than one generation (room tone under a scene, `kind: ambience`) are generated
once and looped with equal-power crossfades, so a minute of room tone costs one 30 s call."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.audio import fit_duration, loop_to
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import write_wav

__all__ = ["MossSfxAdapter"]

SAMPLE_RATE = 48_000
MAX_CALL_S = 30.0


class MossSfxAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_audio_moss_sfx.backend import MossSfxBackend

        assert self.paths is not None
        return MossSfxBackend(model_dir=self.paths.model("moss-soundeffect-v2"), defaults=self.defaults)

    async def run_audio_sfx(self, request: m.SfxRequest, ctx: RunContext) -> m.AudioResult:
        backend = self.require_backend()
        kind = str(request.labels.get("kind", "one_shot" if request.duration_s <= MAX_CALL_S else "ambience"))
        if kind != "ambience" and request.duration_s > MAX_CALL_S:
            raise ValueError(f"moss_sfx_v2: a one-shot is at most {MAX_CALL_S:g} s; got {request.duration_s:g}")
        seconds = min(MAX_CALL_S, max(1.0, round(request.duration_s + (0.0 if kind == "ambience" else 0.3), 1)))
        options = {
            "prompt": " ".join(request.description.split())[:400],
            "seconds": seconds,
            "seed": int(ctx.seed) % (2**31),
            "steps": int(self.defaults.get("inference_steps", 100)),
            "cfg_scale": float(self.defaults.get("cfg_scale", 4.0)),
            "sigma_shift": float(self.defaults.get("sigma_shift", 5.0)),
        }
        samples = await self.call(ctx, backend.generate, options)
        if kind == "ambience":
            shaped = loop_to(
                samples, SAMPLE_RATE, request.duration_s, crossfade_s=float(self.defaults.get("loop_crossfade_s", 1.5))
            )
        else:
            shaped = fit_duration(
                samples, SAMPLE_RATE, request.duration_s, fade_out_s=min(0.25, request.duration_s / 4)
            )
        work = self.scratch(ctx, f"sfx-{request.labels.get('cue', kind)}")
        out = write_wav(work / "sfx.wav", shaped, SAMPLE_RATE)
        ref = await ctx.write_artifact(
            out,
            "audio",
            {
                "adapter_id": self.manifest.id,
                "kind": kind,
                "generated_s": seconds,
                "looped": kind == "ambience" and request.duration_s > seconds,
            },
            role="audio",
            mime="audio/wav",
        )
        return m.AudioResult(audio=ref, duration_s=round(len(shaped) / SAMPLE_RATE, 4), sample_rate=SAMPLE_RATE)
