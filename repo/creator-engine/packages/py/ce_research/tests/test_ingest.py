"""Persistent research (Phase 12): text extraction from every accepted document type, facts with
stable ids and spans, hybrid retrieval, and the claim checks that do not trust the model —
including closed-book enforcement (no unsupported claim gets through)."""

from __future__ import annotations

import uuid

import pytest
from ce_research.claims import ClaimEvidence, Verdict, detect_claims, enforce, numbers_in, support
from ce_research.extract import ExtractError, ExtractLimits, extract, kind_for_mime, parse_transcript
from ce_research.ingest import HybridIndex, StoredFact, fact_id, facts_from_text
from ce_testing.placeholders import placeholder_docx, placeholder_pdf

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


_pdf, _docx = placeholder_pdf, placeholder_docx


def test_pdf_text_layer_is_extracted() -> None:
    out = extract(
        _pdf(["Solar output rose 12 percent in 2025.", "Panels last 25 years."]), kind="pdf", mime="application/pdf"
    )
    assert "Solar output rose 12 percent in 2025." in out.text and "Panels last 25 years." in out.text
    assert out.pages == 1 and out.mime == "application/pdf"


def test_broken_and_empty_documents_are_refused() -> None:
    with pytest.raises(ExtractError):
        extract(b"%PDF-1.4 garbage", kind="pdf", mime="application/pdf")
    with pytest.raises(ExtractError, match="no text"):
        extract(b"   \n ", kind="note", mime="text/plain")
    with pytest.raises(ExtractError, match="speech recognition"):
        extract(b"x", kind="video", mime="video/mp4")
    with pytest.raises(ExtractError):
        extract(b"PK\x03\x04 not a zip", kind="doc", mime=DOCX)
    with pytest.raises(ExtractError, match="cannot be read"):
        extract(b"\x89PNG", kind="doc", mime="image/png")


def test_docx_paragraphs_and_title() -> None:
    out = extract(_docx(["First point.", "Second point."], title="Field notes"), kind="doc", mime=DOCX)
    assert out.text.splitlines() == ["First point.", "Second point."] and out.title == "Field notes"


def test_docx_body_size_is_capped() -> None:
    big = _docx(["x" * 5000])
    with pytest.raises(ExtractError, match="too large"):
        extract(big, kind="doc", mime=DOCX, limits=ExtractLimits(max_archive_bytes=1000))


def test_transcripts_drop_cue_numbers_timings_and_markup() -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,500\nHello <i>there</i>.\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond line.\n"
    vtt = "WEBVTT\n\nNOTE a comment\nignored\n\n00:01.000 --> 00:02.000\n<v Sam>Hi from VTT.\n"
    assert parse_transcript(srt) == ["Hello there.", "Second line."]
    assert parse_transcript(vtt) == ["Hi from VTT."]
    assert extract(srt.encode(), kind="transcript", mime="application/x-subrip").text == "Hello there.\nSecond line."


def test_html_and_text_are_cleaned_and_capped() -> None:
    html = b"<html><head><title>Report</title></head><body><script>evil()</script><p>Fact one.</p></body></html>"
    out = extract(html, kind="url", mime="text/html; charset=utf-8")
    assert out.title == "Report" and out.text == "Fact one." and "evil" not in out.text
    capped = extract(b"abc " * 100, kind="note", mime="text/plain", limits=ExtractLimits(max_chars=20))
    assert len(capped.text) == 20 and capped.warnings
    assert extract(b"bad\x00char\x07s", kind="note", mime="text/plain").text == "badchars"


def test_upload_media_types_map_to_source_kinds() -> None:
    assert kind_for_mime("application/pdf") == "pdf"
    assert kind_for_mime(DOCX) == "doc" and kind_for_mime("text/html") == "doc"
    assert kind_for_mime("text/vtt") == "transcript" and kind_for_mime("text/plain; charset=utf-8") == "note"
    assert kind_for_mime("video/mp4") is None


def test_facts_have_stable_ids_and_exact_spans() -> None:
    source = uuid.uuid4()
    text = " ".join(f"word{i}." if i % 7 == 6 else f"word{i}" for i in range(200))
    facts = facts_from_text(source, text, words=40, overlap=5)
    assert [f.id for f in facts] == [fact_id(source, f.index) for f in facts]
    assert facts_from_text(source, text, words=40, overlap=5) == facts  # deterministic re-ingestion
    for fact in facts:
        assert text[fact.start : fact.end] == fact.text


def test_hybrid_retrieval_uses_vectors_of_the_same_model_only() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    facts = [
        StoredFact(
            fact_id(a, 0), a, 0, "Solar panels convert sunlight.", 0, 30, embedding=(1.0, 0.0), embedding_model="m"
        ),
        StoredFact(fact_id(b, 0), b, 0, "Kittens sleep a lot.", 0, 20, embedding=(0.0, 1.0), embedding_model="m"),
        StoredFact(fact_id(b, 1), b, 1, "Unrelated text.", 0, 15, embedding=(1.0, 0.0), embedding_model="other"),
    ]
    index = HybridIndex(facts)
    semantic = index.search("photovoltaics", 2, query_vector=(1.0, 0.0), model="m")
    assert [f.text for f, _ in semantic] == [
        "Solar panels convert sunlight."
    ]  # no keyword overlap; the vector finds it
    keyword = index.search("kittens", 3)
    assert [f.text for f, _ in keyword] == ["Kittens sleep a lot."]
    assert facts[0].evidence_id == str(facts[0].id)


def test_support_needs_every_number_and_most_terms() -> None:
    evidence = "In our 2025 survey, 73% of creators said they post three times a week."
    assert support("73% of creators post three times a week", evidence) > 0.5
    assert support("80% of creators post three times a week", evidence) == 0.0  # a different number
    assert support("Creators prefer video podcasts", evidence) == 0.0
    assert numbers_in("It grew 1,200 % from 3.5 to 7") == {"1200%", "3.5", "7"}


def test_the_detector_finds_checkable_sentences() -> None:
    found = detect_claims([("seg_1", "Agents are not chatbots. Studies show 90% fail. Trust me.")])
    assert found == [("seg_1", "Studies show 90% fail.")]


def _fixture() -> tuple[dict[str, ClaimEvidence], list[ClaimEvidence]]:
    user = ClaimEvidence("u1", "Our survey: 73% of creators post three times a week.", "user_provided")
    web = ClaimEvidence("w1", "A blog says 73% of creators post three times a week.", "web")
    return {"u1": user, "w1": web}, [user, web]


def test_closed_book_lets_no_unsupported_claim_through() -> None:
    by_id, everything = _fixture()
    segments = [("seg_1", "73% of creators post three times a week. Also 40% quit within a year.")]
    reported: list[tuple[str, str, Verdict, list[str]]] = [
        ("seg_1", "73% of creators post three times a week", "supported", ["w1"]),  # web evidence only
        ("seg_1", "Also 40% quit within a year", "uncertain", []),
    ]
    checked = enforce(reported, segments=segments, evidence_of=by_id.get, search=lambda _: everything, closed_book=True)
    assert [c.verdict for c in checked] == ["unsupported", "unsupported"]
    assert all(c.blocking and not c.overridable for c in checked)
    ok = enforce(
        [("seg_1", "73% of creators post three times a week", "supported", ["u1"])],
        segments=[("seg_1", "73% of creators post three times a week.")],
        evidence_of=by_id.get,
        search=lambda _: everything,
        closed_book=True,
    )
    assert [(c.verdict, c.evidence_ids, c.blocking) for c in ok] == [("supported", ["u1"], False)]


def test_unreported_statistics_are_detected_and_checked() -> None:
    by_id, everything = _fixture()
    segments = [("seg_1", "Here is a fact. 73% of creators post three times a week. Nobody knows that 12% retire.")]
    closed = enforce([], segments=segments, evidence_of=by_id.get, search=lambda _: everything, closed_book=True)
    assert [(c.verdict, c.detected) for c in closed] == [("supported", True), ("unsupported", True)]
    assert closed[0].evidence_ids == ["u1"]  # closed book: only the user's source
    opened = enforce([], segments=segments, evidence_of=by_id.get, search=lambda _: everything, closed_book=False)
    assert [c.verdict for c in opened] == ["supported", "uncertain"]
    assert not opened[1].blocking  # open book: uncertain warns


def test_open_book_downgrades_unsupported_support_and_keeps_overrides_possible() -> None:
    by_id, _ = _fixture()
    checked = enforce(
        [
            ("seg_1", "90% of creators quit", "supported", ["u1"]),  # cited evidence does not say it
            ("seg_1", "Creators earn more than doctors", "unsupported", []),
        ],
        segments=[("seg_1", "90% of creators quit. Creators earn more than doctors.")],
        evidence_of=by_id.get,
        search=lambda _: [],
        closed_book=False,
    )
    assert [c.verdict for c in checked] == ["uncertain", "unsupported"]
    assert checked[1].blocking and checked[1].overridable
