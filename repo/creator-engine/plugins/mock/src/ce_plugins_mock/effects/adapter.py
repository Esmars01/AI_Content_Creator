"""Mock effects: solid cards with centred text (titles, overlays, transitions, disclosures)."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import EffectsEngine
from ce_contracts.models import EffectRequest, VideoResult

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import render_text_clip

__all__ = ["MockEffects"]


class MockEffects(MockAdapter, EffectsEngine):
    async def _card(self, name: str, request: EffectRequest, ctx: RunContext, color: str) -> VideoResult:
        work = self.workdir(ctx, name)
        out = work / "card.mp4"
        text = request.text or str(request.effect.get("text", name))
        await render_text_clip(
            out,
            width=request.width,
            height=request.height,
            fps=request.fps,
            duration_s=request.duration_s,
            text=text,
            color=color,
            workdir=work,
        )
        ref = await self.write(ctx, out, "video", role="video", mime="video/mp4", effect=name)
        return VideoResult(
            video=ref, duration_s=request.duration_s, fps=request.fps, width=request.width, height=request.height
        )

    async def run_effects_title(self, request: EffectRequest, ctx: RunContext) -> VideoResult:
        return await self._card("title", request, ctx, "0x202028")

    async def run_effects_overlay(self, request: EffectRequest, ctx: RunContext) -> VideoResult:
        return await self._card("overlay", request, ctx, "0x283040")

    async def run_effects_transition(self, request: EffectRequest, ctx: RunContext) -> VideoResult:
        return await self._card("transition", request, ctx, "0x000000")

    async def run_effects_disclosure(self, request: EffectRequest, ctx: RunContext) -> VideoResult:
        return await self._card("disclosure", request, ctx, "0x401010")
