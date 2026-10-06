"""Benchmark verdicts and blind pairwise ratings (§24, Phase 11).

A benchmark runs a candidate engine on the golden evaluation set next to the production default for
the same capability. Every pair (same case, same seed) is shown to raters blind: `blind_sides`
decides, per pair and rater, which output is on the left, and `resolve_preference` maps the
rater's left/right choice back to candidate/baseline. `verdict` combines:

- the automatic checks of the cases (`min_check_pass_rate`);
- the share of pairs with enough ratings (`min_rated_share`, `min_ratings_per_pair`);
- the candidate's win rate over rated pairs, ties counting half (`min_win_rate`).

The verdict is `pending` until enough pairs are rated; `pass` sets `bench_passed`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ce_config.schemas import BenchmarkConfig

__all__ = ["blind_sides", "resolve_preference", "verdict"]

Preference = Literal["left", "right", "tie"]


def blind_sides(pair_id: str, rater_id: str) -> tuple[str, str]:
    """(left, right) as ("a", "b") or ("b", "a"): stable for a rater, unpredictable across raters."""
    flip = hashlib.sha256(f"bench-blind:{pair_id}:{rater_id}".encode()).digest()[0] & 1
    return ("b", "a") if flip else ("a", "b")


def resolve_preference(pair_id: str, rater_id: str, preferred: Preference) -> Literal["a", "b", "tie"]:
    if preferred == "tie":
        return "tie"
    left, right = blind_sides(pair_id, rater_id)
    return left if preferred == "left" else right  # type: ignore[return-value]


def verdict(
    checks: Sequence[Mapping[str, Any]],
    pairs: Sequence[str],
    ratings: Mapping[str, Sequence[str]],
    config: BenchmarkConfig,
) -> dict[str, Any]:
    """`checks`: [{case, seed, side, passed}] for the candidate (`side: a`); `ratings`: pair id →
    resolved choices (`a` = candidate, `b` = baseline, `tie`)."""
    mine = [c for c in checks if c.get("side", "a") == "a"]
    pass_rate = sum(bool(c.get("passed")) for c in mine) / len(mine) if mine else 0.0
    rated = [p for p in pairs if len(ratings.get(p, ())) >= config.min_ratings_per_pair]
    rated_share = len(rated) / len(pairs) if pairs else 0.0
    votes = [v for p in rated for v in ratings.get(p, ())]
    wins = sum(1.0 if v == "a" else 0.5 if v == "tie" else 0.0 for v in votes)
    win_rate = wins / len(votes) if votes else None
    reasons: list[str] = []
    if not mine:
        reasons.append("no case of the evaluation set applies to this engine")
        status = "fail"
    elif pass_rate < config.min_check_pass_rate:
        reasons.append(f"automatic checks {pass_rate:.0%} < {config.min_check_pass_rate:.0%}")
        status = "fail"
    elif not pairs:
        reasons.append("no baseline engine to compare with: decided on the automatic checks only")
        status = "pass"
    elif rated_share < config.min_rated_share:
        reasons.append(f"{rated_share:.0%} of the pairs rated; {config.min_rated_share:.0%} needed")
        status = "pending"
    elif win_rate is not None and win_rate < config.min_win_rate:
        reasons.append(f"win rate {win_rate:.0%} < {config.min_win_rate:.0%} against the production default")
        status = "fail"
    else:
        status = "pass"
    return {
        "verdict": status,
        "check_pass_rate": round(pass_rate, 4),
        "rated_share": round(rated_share, 4),
        "win_rate": None if win_rate is None else round(win_rate, 4),
        "votes": len(votes),
        "reasons": reasons,
    }
