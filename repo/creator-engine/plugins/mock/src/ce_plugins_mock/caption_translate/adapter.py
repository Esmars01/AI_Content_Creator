"""Mock caption translation: prefixes each dialogue line with the target language (review state pending)."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import CaptionEngine
from ce_contracts.models import CaptionResult, CaptionTranslateRequest

from ce_plugins_mock._base import MockAdapter

__all__ = ["MockCaptionTranslate"]


class MockCaptionTranslate(MockAdapter, CaptionEngine):
    async def run_captions_translate(self, request: CaptionTranslateRequest, ctx: RunContext) -> CaptionResult:
        source = (await ctx.read_artifact(request.captions)).read_text(encoding="utf-8")
        lines = []
        for line in source.splitlines():
            if line.startswith("Dialogue:"):
                head, _, text = line.rpartition(",")
                line = f"{head},[{request.target_language}] {text}"
            lines.append(line)
        out = self.workdir(ctx, "translate") / f"captions.{request.target_language}.ass"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ref = await self.write(
            ctx,
            out,
            "captions",
            role="ass",
            mime="text/x-ssa",
            language=request.target_language,
            review_state="pending",
        )
        return CaptionResult(ass=ref)
