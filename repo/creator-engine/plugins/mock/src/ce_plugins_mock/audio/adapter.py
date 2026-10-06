"""Mock music and SFX (§37): generated tones or noise with envelopes."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import MusicEngine, SFXEngine
from ce_contracts.models import AudioResult, MusicRequest, SfxRequest

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import seed_int, synth_music, synth_sfx, write_wav

__all__ = ["MockAudio"]


class MockAudio(MockAdapter, MusicEngine, SFXEngine):
    seconds_per_unit = 0.05

    async def run_audio_music(self, request: MusicRequest, ctx: RunContext) -> AudioResult:
        samples = synth_music(request.duration_s, bpm=request.bpm or 96, seed=seed_int(ctx.seed, request.description))
        out = self.workdir(ctx, "music") / "music.wav"
        write_wav(out, samples, 48_000)
        ref = await self.write(ctx, out, "audio", role="audio", mime="audio/wav", bpm=request.bpm or 96)
        return AudioResult(audio=ref, duration_s=round(len(samples) / 48_000, 4), sample_rate=48_000)

    async def run_audio_sfx(self, request: SfxRequest, ctx: RunContext) -> AudioResult:
        samples = synth_sfx(
            request.duration_s, seed=seed_int(ctx.seed, request.description), description=request.description
        )
        out = self.workdir(ctx, "sfx") / "sfx.wav"
        write_wav(out, samples, 48_000)
        ref = await self.write(ctx, out, "audio", role="audio", mime="audio/wav")
        return AudioResult(audio=ref, duration_s=round(len(samples) / 48_000, 4), sample_rate=48_000)
