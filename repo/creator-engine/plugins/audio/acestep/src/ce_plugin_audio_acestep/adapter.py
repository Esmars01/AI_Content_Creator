"""ACE-Step 1.5 adapter (`audio.music`, §22 music beds).

Instrumental only (the spec's default; a vocal track would need lyrics the plan never writes):
`instrumental: false` is refused. ACE-Step generates at least 10 s; the adapter asks for the
requested length (rounded up to the engine's minimum), then fits the result to the exact
duration with a short fade-in and a 1.5 s fade-out, mono 48 kHz like the other beds."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.audio import fit_duration
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import read_audio, write_wav

__all__ = ["AceStepAdapter", "caption"]

MIN_S, MAX_S = 10.0, 600.0
SAMPLE_RATE = 48_000


def caption(description: str, limit: int = 512) -> str:
    """ACE-Step's caption: the description, instrumental, capped at the engine's 512 characters."""
    text = " ".join(description.split())
    suffix = ", instrumental, no vocals"
    return (text[: limit - len(suffix)].rstrip(" ,.") + suffix) if text else suffix.lstrip(", ")


class AceStepAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_audio_acestep.backend import AceStepBackend

        assert self.paths is not None
        return AceStepBackend(
            checkpoints=self.paths.model("acestep-v15"), scratch=ctx.scratch_dir, defaults=self.defaults
        )

    async def run_audio_music(self, request: m.MusicRequest, ctx: RunContext) -> m.AudioResult:
        if not request.instrumental:
            raise ValueError("acestep_v15 generates instrumental beds only (no lyrics are planned)")
        if request.duration_s > MAX_S:
            raise ValueError(f"acestep_v15 generates at most {MAX_S:g} s; got {request.duration_s:g}")
        backend = self.require_backend()
        work = self.scratch(ctx, f"music-{request.labels.get('cue', 'bed')}")
        options = {
            "caption": caption(request.description),
            "duration_s": max(MIN_S, round(request.duration_s + 0.5, 1)),
            "bpm": request.bpm,
            "seed": int(ctx.seed) % (2**31),
            "steps": int(self.defaults.get("inference_steps", 8)),
            "shift": float(self.defaults.get("shift", 3.0)),
        }
        raw = await self.call(ctx, backend.generate, options, str(work))
        samples = read_audio(raw, SAMPLE_RATE)
        fitted = fit_duration(samples, SAMPLE_RATE, request.duration_s, fade_in_s=0.05, fade_out_s=1.5)
        out = write_wav(work / "music.wav", fitted, SAMPLE_RATE)
        ref = await ctx.write_artifact(
            out,
            "audio",
            {
                "adapter_id": self.manifest.id,
                "caption": options["caption"],
                "bpm": request.bpm,
                "generated_s": options["duration_s"],
            },
            role="audio",
            mime="audio/wav",
        )
        return m.AudioResult(audio=ref, duration_s=round(len(fitted) / SAMPLE_RATE, 4), sample_rate=SAMPLE_RATE)
