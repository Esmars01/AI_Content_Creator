"""Closed-book planning (§13, Phase 12, ADR 0058): with `sources_policy: closed_book` only the
user's own sources — the input itself and attached `user_provided` research sources — count as
evidence, and no unsupported claim leaves the Director unflagged, whether or not the model
reported it (the recorded fact-check response reports no claims at all)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from ce_director import Director, PlanRequest
from ce_llm import create_llm_provider
from ce_research.ingest import StoredFact, fact_id
from ce_testing.director import FIXTURES_DIR, director_context, director_deps

pytestmark = [pytest.mark.behavior]

STAT = "Studies show 80% of agent projects stall in the first month."
SOURCE = uuid.uuid4()
FACT = str(fact_id(SOURCE, 0))


def _fact(text: str, trust: str) -> StoredFact:
    return StoredFact(fact_id(SOURCE, 0), SOURCE, 0, text, 0, len(text), trust=trust, source_title="Survey")


def _variant(tmp_path: Path) -> str:
    """`explain_ai_agents` with a statistic the input never gave added to the script."""
    doc = yaml.safe_load((FIXTURES_DIR / "explain_ai_agents.yaml").read_text(encoding="utf-8"))
    script = doc["responses"]["script"]
    final = script[-1] if isinstance(script, list) else script
    final["lines"][2] = {"beat": 2, "text": STAT}  # replaces a line: the word budget stays the same
    (tmp_path / "explain_ai_agents.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(doc["inputs"][0])


def _plan(tmp_path: Path, policy: str, facts: list[StoredFact], text: str | None = None) -> Any:
    provider = create_llm_provider("fixture", app_env="test", config={"fixtures_dir": str(tmp_path)})
    request = PlanRequest(
        input=text or _variant(tmp_path),
        sources_policy=policy,  # type: ignore[arg-type]
        sources=[SOURCE] if facts else [],
    )
    return asyncio.run(Director(director_deps(provider)).plan(request, director_context(facts=facts)))


def _stat(outcome: Any) -> Any:
    (claim,) = [c for c in outcome.claims if "80%" in c.text]
    return claim


def _blocking(outcome: Any) -> list[Any]:
    return [f for f in outcome.report.findings if f.kind == "fact_check" and f.severity == "blocking"]


def test_closed_book_blocks_an_unsourced_statistic_without_override(tmp_path: Path) -> None:
    outcome = _plan(tmp_path, "closed_book", [])
    assert outcome.planner == "llm"
    claim = _stat(outcome)
    assert claim.verdict == "unsupported" and claim.detected and claim.blocking and not claim.overridable
    (finding,) = [f for f in _blocking(outcome) if "80%" in f.message]
    assert finding.detail["overridable"] is False and finding.detail["closed_book"] is True
    assert "closed book" in finding.message


def test_closed_book_ignores_web_evidence_and_accepts_the_users_source(tmp_path: Path) -> None:
    web = _plan(tmp_path, "closed_book", [_fact("A blog: 80% of agent projects stall in the first month.", "web")])
    assert _stat(web).verdict == "unsupported"
    assert web.evidence.keys().isdisjoint({FACT})  # web facts never enter a closed-book plan
    user = _plan(
        tmp_path,
        "closed_book",
        [_fact("Our 2026 study: 80% of agent projects stall in the first month.", "user_provided")],
    )
    claim = _stat(user)
    assert claim.verdict == "supported" and claim.evidence_ids == [FACT]
    assert not [f for f in _blocking(user) if "80%" in f.message]
    assert user.evidence[FACT].source_id == SOURCE  # traceable to the stored fact and its source
    research = user.spec.model_dump(mode="json")["research"]
    assert [c["evidence_ids"] for c in research["claims"] if "80%" in c["text"]] == [[FACT]]
    assert str(SOURCE) in research["source_ids"]


def test_open_book_warns_about_the_same_statistic(tmp_path: Path) -> None:
    outcome = _plan(tmp_path, "open", [])
    claim = _stat(outcome)
    assert claim.verdict == "uncertain" and claim.detected and not claim.blocking
    assert [f.severity for f in outcome.report.findings if f.kind == "fact_check" and "80%" in f.message] == ["warning"]


def test_a_statistic_in_the_users_own_input_is_supported_by_it(tmp_path: Path) -> None:
    """The template Director (no fixture) plans the input's own sentences; the input is the user's
    source, so its statistic traces to it rather than being blocked."""
    outcome = _plan(
        tmp_path / "none", "closed_book", [], text="Posting habits. 73% of creators post three times a week."
    )
    assert outcome.planner == "template"
    (claim,) = [c for c in outcome.claims if "73%" in c.text]
    assert claim.verdict == "supported" and claim.evidence_ids
