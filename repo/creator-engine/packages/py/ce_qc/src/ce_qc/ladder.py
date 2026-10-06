"""The decision ladder per failed node (§26) and its budgets.

Rungs, in the tier's `ladder` order:
1. `attempt_seed` — re-run the failed node with an attempt seed `hash(base_seed, retry_n)`;
2. `fallback_route` — re-run it on the route's next fallback adapter (skipped, with the reason, when
   there is none or when Performance QA wants a measured improvement the profiles do not show);
3. `cheaper_fix` — a cheaper repair such as a lip-sync patch instead of a re-render. It changes the
   graph, so it is proposed in the report for the user to apply, never executed by the gate;
4. `needs_review` — the version keeps its best attempt and is flagged.

Every re-run counts against the per-node and per-version budgets, in count and in USD (the spent
retry cost plus the estimate of the next one). Seed retries keep one rung of the per-node budget
for the fallback route when one exists, but at least one seed retry is made when the budget
allows any. The ladder is a pure function of the history so far: the workflow replays it
deterministically, and the same inputs always give the same rung.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Budgets", "LadderInput", "Rung", "Usage", "next_rung"]


@dataclass(frozen=True)
class Budgets:
    retries_per_node: int
    retries_per_version: int
    usd_per_version: float


@dataclass(frozen=True)
class Usage:
    """Retries already spent by the version (every gated node, the exact-script loop included)."""

    version_retries: int = 0
    version_usd: float = 0.0


@dataclass(frozen=True)
class LadderInput:
    ladder: Sequence[str]
    budgets: Budgets
    usage: Usage
    history: Sequence[Mapping[str, Any]] = ()  # steps of this gate so far: {"step", "outcome", ...}
    est_usd: float = 0.0  # estimated cost of one re-run of the targets
    retryable: bool = True  # False: the failure has no target the gate can re-run
    not_retryable_reason: str | None = None
    fallback: str | None = None  # the next untried fallback adapter, when one may be used
    fallback_reason: str | None = None  # why there is none
    cheaper_fix: Mapping[str, Any] | None = None  # a proposal (e.g. a lip-sync patch), never auto-applied
    cheaper_fix_reason: str | None = None


@dataclass(frozen=True)
class Rung:
    action: str  # retry | fallback | needs_review
    step: str
    reason: str = ""
    adapter_id: str | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)  # rungs passed over in this decision

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "step": self.step,
            "reason": self.reason,
            "adapter_id": self.adapter_id,
            "notes": list(self.notes),
        }


def _budget_exceeded(inp: LadderInput, node_runs: int) -> str | None:
    b, u = inp.budgets, inp.usage
    if node_runs >= b.retries_per_node:
        return f"per-node retry budget spent ({node_runs}/{b.retries_per_node})"
    if u.version_retries >= b.retries_per_version:
        return f"per-version retry budget spent ({u.version_retries}/{b.retries_per_version})"
    if u.version_usd + inp.est_usd > b.usd_per_version + 1e-9:
        return (
            f"per-version retry USD budget: {u.version_usd:.4f} spent + {inp.est_usd:.4f} estimated "
            f"> {b.usd_per_version:.4f}"
        )
    return None


def next_rung(inp: LadderInput) -> Rung:
    runs = [h for h in inp.history if h.get("outcome") == "run"]
    seen = {(str(h.get("step")), str(h.get("outcome"))) for h in inp.history}
    seed_runs = sum(1 for h in runs if h.get("step") == "attempt_seed")
    fallback_runs = sum(1 for h in runs if h.get("step") == "fallback_route")
    notes: list[dict[str, Any]] = []

    def note(step: str, outcome: str, reason: str, **extra: Any) -> None:
        if (step, outcome) not in seen:
            notes.append({"step": step, "outcome": outcome, "reason": reason, **extra})

    for step in inp.ladder:
        if step == "attempt_seed":
            if not inp.retryable:
                note(step, "skipped", inp.not_retryable_reason or "nothing the gate can re-run")
                continue
            fallback_left = "fallback_route" in inp.ladder and inp.fallback is not None and fallback_runs == 0
            allowed = inp.budgets.retries_per_node
            if allowed > 0 and fallback_left:
                allowed = max(1, allowed - 1)
            if seed_runs >= allowed:
                if seed_runs == 0:
                    note(step, "skipped", f"per-node retry budget is {inp.budgets.retries_per_node}")
                continue
            exceeded = _budget_exceeded(inp, len(runs))
            if exceeded:
                note(step, "skipped", exceeded)
                continue
            return Rung("retry", step, f"attempt seed retry {seed_runs + 1}", notes=notes)
        if step == "fallback_route":
            if fallback_runs:
                continue
            if not inp.retryable:
                note(step, "skipped", inp.not_retryable_reason or "nothing the gate can re-run")
                continue
            if inp.fallback is None:
                note(step, "skipped", inp.fallback_reason or "no fallback route")
                continue
            exceeded = _budget_exceeded(inp, len(runs))
            if exceeded:
                note(step, "skipped", exceeded)
                continue
            return Rung("fallback", step, f"fallback route {inp.fallback}", adapter_id=inp.fallback, notes=notes)
        if step == "cheaper_fix":
            if inp.cheaper_fix is not None:
                note(
                    step,
                    "proposed",
                    "a cheaper fix changes the graph: proposed for the user",
                    proposal=dict(inp.cheaper_fix),
                )
            else:
                note(step, "skipped", inp.cheaper_fix_reason or "no cheaper fix for this failure")
            continue
        if step == "needs_review":
            return Rung("needs_review", step, "flagged for review with the best attempt", notes=notes)
    return Rung("needs_review", "needs_review", "ladder exhausted", notes=notes)
