"""Research for the Director (§13 stage 3) and the SSRF-guarded fetcher (§33).

Status: Phase 4 implements in-memory research — pasted text and URLs fetched through the guarded
fetcher, chunked and retrieved by keyword (BM25) — and the guard every URL fetch must use.
Phase 12 adds persistent research (ADR 0058) —
- `extract`: text from HTML, PDF (pypdf), DOCX, SRT/VTT transcripts and plain text, with limits;
- `ingest`: facts with stable ids and character spans, and `HybridIndex` (BM25 plus the cosine of
  embeddings of the same model);
- `claims`: claim checks in code (`support`, `detect_claims`, `enforce`): the model's verdicts are
  re-checked against the cited evidence, unreported statistics are detected, and closed book
  admits only user-provided evidence;
- `ssrf`: binary bodies for ingestion (PDF) and `precheck` for request validation.
"""

from ce_research.documents import Chunk, Source, chunk_source, html_to_text, make_source
from ce_research.research import ResearchResult, SourceFailure, extract_urls, gather
from ce_research.retrieval import KeywordIndex, ScoredChunk, terms
from ce_research.ssrf import FetchedDocument, FetchError, GuardedFetcher, SSRFBlocked, blocked_reason

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 4

__all__ = [
    "Chunk",
    "FetchError",
    "FetchedDocument",
    "GuardedFetcher",
    "KeywordIndex",
    "ResearchResult",
    "SSRFBlocked",
    "ScoredChunk",
    "Source",
    "SourceFailure",
    "blocked_reason",
    "chunk_source",
    "extract_urls",
    "gather",
    "html_to_text",
    "make_source",
    "terms",
]
