"""In-memory research: HTML extraction, chunking, keyword retrieval, URL gathering through the
guard and dossier evidence checking."""

from __future__ import annotations

import itertools

import httpx
from ce_config.schemas import ResearchConfig, ResearchFetchConfig
from ce_core.spec.videospec import Claim
from ce_research import GuardedFetcher, KeywordIndex, chunk_source, extract_urls, gather, html_to_text, make_source

PUBLIC = "93.184.215.14"


def test_html_to_text_drops_scripts_and_keeps_blocks() -> None:
    title, text = html_to_text(
        "<html><head><title> Solar  power </title><style>p{}</style></head><body>"
        "<script>ignore()</script><h1>Panels</h1><p>Panels convert <b>light</b>.</p>"
        "<p>Second &amp; last</p></body></html>"
    )
    assert title == "Solar power"
    assert text.splitlines() == ["Panels", "Panels convert light.", "Second & last"]
    assert "ignore" not in text


def test_chunks_cover_the_text_with_overlap_and_offsets() -> None:
    words = [f"w{i}." if i % 10 == 9 else f"w{i}" for i in range(100)]
    source = make_source(" ".join(words), kind="pasted")
    chunks = chunk_source(source, words=30, overlap=5)
    assert chunks[0].text.startswith("w0") and chunks[-1].text.endswith("w99.")
    for chunk in chunks:
        assert source.text[chunk.start : chunk.end] == chunk.text
        assert len(chunk.text.split()) <= 30
    for a, b in itertools.pairwise(chunks):
        assert b.start < a.end  # overlapping windows
    assert len({c.id for c in chunks}) == len(chunks)
    assert make_source(source.text, kind="pasted").id == source.id  # stable ids


def test_keyword_retrieval_ranks_the_relevant_chunk_first() -> None:
    sources = [
        make_source("Solar panels convert sunlight into electricity using photovoltaic cells.", kind="pasted"),
        make_source("Wind turbines spin when the wind blows and drive a generator.", kind="pasted"),
        make_source("Batteries store electricity for the night.", kind="pasted"),
    ]
    chunks = [c for s in sources for c in chunk_source(s, words=50, overlap=0)]
    hits = KeywordIndex(chunks).search("how do photovoltaic solar panels work", 2)
    assert hits[0].chunk.source_id == sources[0].id
    assert KeywordIndex(chunks).search("quantum chromodynamics", 3) == []


def test_extract_urls_trims_punctuation_and_dedupes() -> None:
    text = "See https://a.test/x, and (http://b.test/y). Again https://a.test/x."
    assert extract_urls(text) == ["https://a.test/x", "http://b.test/y"]


async def test_gather_fetches_through_the_guard_and_records_refusals() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Fusion</title><p>Fusion reactors fuse hydrogen isotopes into helium.</p>",
        )

    async def resolve(host: str, port: int) -> list[str]:
        return {"good.test": [PUBLIC], "intranet.test": ["10.1.2.3"]}[host]

    fetcher = GuardedFetcher(ResearchFetchConfig(), resolver=resolve, transport=httpx.MockTransport(answer))
    raw = "Explain fusion. Sources: https://good.test/fusion and http://intranet.test/secret"
    result = await gather(raw, config=ResearchConfig(), fetcher=fetcher)
    kinds = sorted(s.kind for s in result.sources)
    assert kinds == ["pasted", "url"]
    (failure,) = result.failures
    assert failure.blocked and failure.url == "http://intranet.test/secret"
    best = result.evidence("hydrogen isotopes helium", 1)[0]
    assert best.chunk.source_id == next(s.id for s in result.sources if s.kind == "url")


async def test_closed_book_skips_fetching_and_retrieval() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("closed book must not fetch")

    fetcher = GuardedFetcher(ResearchFetchConfig(), transport=httpx.MockTransport(fail))
    result = await gather(
        "Use only this: https://x.test/ says water is wet", config=ResearchConfig(), fetcher=fetcher, closed_book=True
    )
    assert result.evidence("water wet", 3) == []
    assert result.dossier([]).closed_book


async def test_dossier_drops_evidence_ids_the_model_invented() -> None:
    result = await gather("The Eiffel Tower is 330 metres tall.", config=ResearchConfig(), fetcher=None)
    real = result.chunks[0].id
    claim = Claim(key="clm_1", text="The tower is 330 m", verdict="supported", evidence_ids=[real, "made-up#c9"])
    dossier = result.dossier([claim])
    assert dossier.claims[0].evidence_ids == [real]
    assert dossier.source_ids == [result.sources[0].id]
