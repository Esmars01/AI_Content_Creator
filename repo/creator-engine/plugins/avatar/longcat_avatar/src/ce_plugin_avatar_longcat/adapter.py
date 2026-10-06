"""LongCat-Video-Avatar 1.5 adapter: `avatar.a2v`. Prepares the start image (keyframe or the previous
chunk's last frame), plans the windows, runs the backend, and conforms the clip to the request with
the request's own audio muxed in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit import EngineAdapter
from ce_plugin_kit.media import conform_clip, probe

from ce_plugin_avatar_longcat.params import build_params

__all__ = ["LongCatAvatarAdapter"]


class LongCatAvatarAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_avatar_longcat.backend import LongCatAvatarBackend  # heavy imports live there

        assert self.paths is not None
        return LongCatAvatarBackend(
            checkpoint_dir=self.paths.model(), base_dir=self.paths.dependency("base_weights"), defaults=self.defaults
        )

    async def run_avatar_a2v(self, request: m.AvatarRequest, ctx: RunContext) -> m.AvatarResult:
        backend = self.require_backend()
        work = self.scratch(ctx, f"a2v-{request.chunk_index}")
        audio = await ctx.read_artifact(request.audio)
        image = await self.start_image(ctx, request, work)
        audio_s = probe(audio).duration_s
        engine = dict(request.engine or {})
        cue = str(self.defaults.get("verbal_cue", ""))
        prompt = str(engine.get("prompt") or " ".join(p for p in (cue, request.prompt) if p))
        params = build_params(
            width=request.width, height=request.height, audio_seconds=audio_s, seed=ctx.seed, prompt=prompt,
            defaults=self.defaults,
        )  # fmt: skip
        await ctx.progress(0.05, f"generating {params.segments} window(s) at {params.resolution}")
        raw = await self.call(ctx, backend.generate, params, image, audio, work)
        out = conform_clip(
            Path(raw), work / "take.mp4", width=request.width, height=request.height, duration_s=audio_s, audio=audio
        )
        info = probe(out)
        ref = await ctx.write_artifact(
            out,
            "video",
            {"engine": "longcat_avatar", "params": params.as_dict(), "duration_s": info.duration_s},
            role="video",
            mime="video/mp4",
        )
        await ctx.progress(1.0, "done")
        return m.AvatarResult(
            video=ref, duration_s=info.duration_s, fps=float(params.fps), width=info.width, height=info.height
        )
