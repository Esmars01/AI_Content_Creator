"""Director stage 3 (§13), in memory: gather sources from the pasted text and the URLs in the input,
chunk them, and retrieve evidence by keyword for the strategy, script and fact-check stages.

`closed_book` (the brief's `sources_policy`) skips fetching and retrieval: the dossier records it
and the fact check marks claims `uncertain` unless the user's own text supports them.
A URL that cannot be fetched (refused by the SSRF guard, too big, wrong type) is recorded as a
failure; it never stops planning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import regex
from ce_config.schemas import ResearchConfig
from ce_core.spec.videospec import Claim, ResearchDossier

from ce_research.documents import Chunk, Source, chunk_source, html_to_text, make_source
from ce_research.retrieval import KeywordIndex, ScoredChunk
from ce_research.ssrf import FetchError, GuardedFetcher, SSRFBlocked

__all__ = ["ResearchResult", "SourceFailure", "extract_urls", "gather"]

_URL = regex.compile(r"https?://[^\s<>\"'`\]\)]+", regex.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """http(s) URLs in the user's text, in order, without duplicates or trailing punctuation."""
    found = [m.group(0).rstrip(".,;:!?") for m in _URL.finditer(text)]
    return list(dict.fromkeys(found))


@dataclass(frozen=True)
class SourceFailure:
    url: str
    reason: str
    blocked: bool  # refused by the SSRF guard (vs. a fetch error)


@dataclass
class ResearchResult:
    closed_book: bool
    sources: list[Source] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)
    _index: KeywordIndex | None = None

    @property
    def index(self) -> KeywordIndex:
        if self._index is None:
            self._index = KeywordIndex(self.chunks)
        return self._index

    def evidence(self, query: str, k: int) -> list[ScoredChunk]:
        return [] if self.closed_book else self.index.search(query, k)

    def chunk(self, evidence_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.id == evidence_id), None)

    def dossier(
        self,
        claims: list[Claim],
        *,
        extra_evidence: set[str] | None = None,
        extra_sources: list[uuid.UUID] | None = None,
    ) -> ResearchDossier:
        """Claims keep only evidence ids that exist in this research (model output is not trusted);
        `extra_evidence`/`extra_sources` are persistent facts and sources the plan used (Phase 12)."""
        known = {c.id for c in self.chunks} | set(extra_evidence or ())
        checked = [c.model_copy(update={"evidence_ids": [e for e in c.evidence_ids if e in known]}) for c in claims]
        sources = [s.id for s in self.sources] + [
            s for s in (extra_sources or []) if s not in {x.id for x in self.sources}
        ]
        return ResearchDossier(claims=checked, source_ids=sources, closed_book=self.closed_book)


async def gather(
    raw_input: str,
    *,
    config: ResearchConfig,
    fetcher: GuardedFetcher | None,
    pasted: list[str] | None = None,
    closed_book: bool = False,
) -> ResearchResult:
    """Sources: the user's input itself (it often carries facts), any extra pasted texts, and up to
    `max_urls` URLs found in the input, fetched through the guard."""
    result = ResearchResult(closed_book=closed_book)
    texts = [raw_input, *(pasted or [])]
    for text in texts:
        clipped = text[: config.max_pasted_chars]
        if clipped.strip():
            result.sources.append(make_source(clipped, kind="pasted", title="pasted text"))
    if not closed_book and fetcher is not None:
        for url in extract_urls(raw_input)[: config.max_urls]:
            try:
                document = await fetcher.fetch(url)
            except SSRFBlocked as exc:
                result.failures.append(SourceFailure(url, str(exc), blocked=True))
                continue
            except FetchError as exc:
                result.failures.append(SourceFailure(url, str(exc), blocked=False))
                continue
            title, text = (
                html_to_text(document.text)
                if document.content_type in ("text/html", "application/xhtml+xml")
                else ("", document.text)
            )
            if text.strip():
                result.sources.append(make_source(text, kind="url", title=title, url=document.final_url))
    seen: set[str] = set()
    for source in result.sources:
        if source.digest in seen:
            continue
        seen.add(source.digest)
        result.chunks.extend(chunk_source(source, words=config.chunk_words, overlap=config.chunk_overlap_words))
    return result
