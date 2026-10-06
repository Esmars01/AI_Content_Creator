"""Benchmark verdicts and blind pairwise ratings (§24)."""

from __future__ import annotations

from ce_config.schemas import BenchmarkConfig
from ce_qc.bench import blind_sides, resolve_preference, verdict

CFG = BenchmarkConfig()


def test_blinding_is_stable_per_rater_and_resolves_back() -> None:
    sides = {blind_sides(f"p{i}", "rater") for i in range(40)}
    assert sides == {("a", "b"), ("b", "a")}  # both orders occur
    for i in range(10):
        left, right = blind_sides(f"p{i}", "rater")
        assert resolve_preference(f"p{i}", "rater", "left") == left
        assert resolve_preference(f"p{i}", "rater", "right") == right
    assert resolve_preference("p1", "rater", "tie") == "tie"


def test_verdict_waits_for_ratings_then_decides_on_win_rate() -> None:
    checks = [
        {"case": "c1", "seed": 0, "side": "a", "passed": True},
        {"case": "c1", "seed": 1, "side": "a", "passed": True},
    ]
    pending = verdict(checks, ["p1", "p2"], {"p1": ["a"]}, CFG)
    assert pending["verdict"] == "pending" and pending["rated_share"] == 0.5
    passed = verdict(checks, ["p1", "p2"], {"p1": ["a"], "p2": ["tie"]}, CFG)
    assert passed["verdict"] == "pass" and passed["win_rate"] == 0.75
    lost = verdict(checks, ["p1", "p2"], {"p1": ["b"], "p2": ["b"]}, CFG)
    assert lost["verdict"] == "fail" and "win rate" in lost["reasons"][0]


def test_failed_checks_fail_without_waiting_and_no_case_fails() -> None:
    checks = [{"case": "c1", "seed": 0, "side": "a", "passed": False}]
    assert verdict(checks, ["p1"], {}, CFG)["verdict"] == "fail"
    assert verdict([], [], {}, CFG)["verdict"] == "fail"
    alone = verdict([{"case": "c1", "seed": 0, "side": "a", "passed": True}], [], {}, CFG)
    assert alone["verdict"] == "pass" and "automatic checks only" in alone["reasons"][0]
