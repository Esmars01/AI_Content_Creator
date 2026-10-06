"""The decision ladder (§26): rung order, budgets in count and USD, fallback and cheaper-fix notes."""

from __future__ import annotations

from typing import Any

from ce_qc.ladder import Budgets, LadderInput, Usage, next_rung

LADDER = ["attempt_seed", "fallback_route", "cheaper_fix", "needs_review"]


def inp(**kw: Any) -> LadderInput:
    base: dict[str, Any] = {
        "ladder": LADDER,
        "budgets": Budgets(retries_per_node=2, retries_per_version=6, usd_per_version=2.0),
        "usage": Usage(),
    }
    return LadderInput(**{**base, **kw})


def run(step: str, **extra: Any) -> dict[str, Any]:
    return {"step": step, "outcome": "run", **extra}


def test_seed_then_fallback_then_review() -> None:
    first = next_rung(inp(fallback="mock_b"))
    assert (first.action, first.step) == ("retry", "attempt_seed")
    second = next_rung(inp(fallback="mock_b", history=[run("attempt_seed")]))
    assert (second.action, second.adapter_id) == ("fallback", "mock_b")
    third = next_rung(inp(fallback=None, history=[run("attempt_seed"), run("fallback_route", adapter_id="mock_b")]))
    assert third.action == "needs_review"
    assert [n["step"] for n in third.notes] == ["attempt_seed", "cheaper_fix"]
    assert "per-node retry budget spent (2/2)" in third.notes[0]["reason"]


def test_seed_retries_use_the_budget_when_there_is_no_fallback() -> None:
    history: list[dict[str, Any]] = []
    actions = []
    for _ in range(4):
        rung = next_rung(inp(history=history, fallback_reason="no fallback route"))
        actions.append(rung.action)
        if rung.action != "retry":
            break
        history.append(run(rung.step))
    assert actions == ["retry", "retry", "needs_review"]
    assert next_rung(inp(history=history)).notes[0] == {
        "step": "fallback_route",
        "outcome": "skipped",
        "reason": "no fallback route",
    }


def test_one_seed_retry_is_made_even_when_the_budget_is_one_and_a_fallback_exists() -> None:
    budgets = Budgets(retries_per_node=1, retries_per_version=6, usd_per_version=2.0)
    first = next_rung(inp(budgets=budgets, fallback="mock_b"))
    assert first.step == "attempt_seed"
    second = next_rung(inp(budgets=budgets, fallback="mock_b", history=[run("attempt_seed")]))
    assert second.action == "needs_review"
    assert any("per-node retry budget spent" in n["reason"] for n in second.notes)


def test_per_version_count_and_usd_budgets_cap_retries() -> None:
    spent = next_rung(inp(usage=Usage(version_retries=6)))
    assert spent.action == "needs_review" and "per-version retry budget" in spent.notes[0]["reason"]
    usd = next_rung(inp(usage=Usage(version_usd=1.9), est_usd=0.2))
    assert usd.action == "needs_review" and "USD" in usd.notes[0]["reason"]
    fits = next_rung(inp(usage=Usage(version_usd=1.5), est_usd=0.5))
    assert fits.action == "retry"


def test_zero_budget_means_no_retry() -> None:
    rung = next_rung(inp(budgets=Budgets(0, 6, 2.0), fallback="mock_b"))
    assert rung.action == "needs_review"
    assert rung.notes[0]["step"] == "attempt_seed" and rung.notes[0]["outcome"] == "skipped"


def test_not_retryable_failures_go_to_review_with_the_reason() -> None:
    rung = next_rung(inp(retryable=False, not_retryable_reason="the retry target tts.segment is outside the shot"))
    assert rung.action == "needs_review"
    assert {n["reason"] for n in rung.notes if n["step"] in ("attempt_seed", "fallback_route")} == {
        "the retry target tts.segment is outside the shot"
    }


def test_a_cheaper_fix_is_proposed_never_executed() -> None:
    history = [run("attempt_seed"), run("attempt_seed")]
    rung = next_rung(inp(history=history, cheaper_fix={"op": "lipsync_patch", "shot_key": "sht_1"}))
    assert rung.action == "needs_review"
    proposed = [n for n in rung.notes if n["step"] == "cheaper_fix"]
    assert proposed and proposed[0]["outcome"] == "proposed" and proposed[0]["proposal"]["op"] == "lipsync_patch"


def test_notes_are_not_repeated_across_decisions() -> None:
    first = next_rung(inp(budgets=Budgets(2, 6, 2.0), fallback=None, fallback_reason="none"))
    assert first.action == "retry" and first.notes == []
    history = [run("attempt_seed"), {"step": "fallback_route", "outcome": "skipped", "reason": "none"}]
    second = next_rung(inp(history=history, fallback=None, fallback_reason="none"))
    assert all(n["step"] != "fallback_route" for n in second.notes)
