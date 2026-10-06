"""Mock research engine: no network access; fetch returns a labelled placeholder document."""

from __future__ import annotations

from ce_contracts.common import RunContext
from ce_contracts.interfaces import ResearchEngine
from ce_contracts.models import (
    ResearchFetchRequest,
    ResearchFetchResult,
    ResearchIngestRequest,
    ResearchIngestResult,
    ResearchSearchRequest,
    ResearchSearchResult,
)

from ce_plugins_mock._base import MockAdapter

__all__ = ["MockResearch"]


class MockResearch(MockAdapter, ResearchEngine):
    async def run_research_fetch(self, request: ResearchFetchRequest, ctx: RunContext) -> ResearchFetchResult:
        out = self.workdir(ctx, "fetch") / "document.txt"
        out.write_text(f"MOCK DOCUMENT (not fetched): {request.url}\n", encoding="utf-8")
        ref = await self.write(ctx, out, "other", role="document", mime="text/plain")
        return ResearchFetchResult(document=ref, title="mock document", content_type="text/plain")

    async def run_research_ingest(self, request: ResearchIngestRequest, ctx: RunContext) -> ResearchIngestResult:
        text = (await ctx.read_artifact(request.document)).read_text(encoding="utf-8", errors="replace")
        size = request.chunk_chars
        return ResearchIngestResult(chunks=[text[i : i + size] for i in range(0, len(text), size)] or [""])

    async def run_research_search(self, request: ResearchSearchRequest, ctx: RunContext) -> ResearchSearchResult:
        return ResearchSearchResult(hits=[])
