"""Dirty analysis and impact (§12.9): `diff_graph(old, new) -> Impact`, without executing anything.

A node of the new graph is
- **regenerated** when it is new, or its own cache-key inputs changed (static digest: spec
  fragment, referenced DNA/world/memory fields, config digests, params, seed basis, take), or its
  route changed;
- **cascaded** when its own inputs are unchanged but it runs again: a dependency's output changes
  (chunk k+1…n after chunk k, §12.6), or — for nodes that read behavior requests (compile, keyframe
  state, `qc.shot`, coverage) — the CBS content it reads changed;
- **kept** otherwise, and when it reuses the parent's artifact by construction (a voice lock).

`behavior.resolve`, the compile nodes and `behavior.keyframe_state` are cheap CPU nodes: callers
pass `evaluate`, which runs them for the old and the new version and returns the digests of the
outputs their consumers read (the CBS content; the compiled output generation reads). When such a
node runs again but that digest is unchanged, its consumers are not affected — generation stays
kept — and the change is listed in `no_visible_effect` (the engine cannot show it). Without
`evaluate` the analysis is conservative: every changed behavior node propagates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ce_core.build import ExecutionGraph, ExecutionNode

__all__ = ["BEHAVIOR_EVALUATED_KINDS", "Impact", "RouteChange", "diff_graph"]

BEHAVIOR_EVALUATED_KINDS = frozenset(
    {"behavior.resolve", "behavior.compile_voice", "behavior.compile_visual", "behavior.keyframe_state"}
)

Evaluate = Callable[[ExecutionNode | None, ExecutionNode], tuple[str | None, str | None]]


@dataclass(frozen=True)
class RouteChange:
    node_key: str
    old: str | None  # adapter id
    new: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"node_key": self.node_key, "from": self.old, "to": self.new, "reason": self.reason}


@dataclass
class Impact:
    regenerate: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    cascade: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    no_visible_effect: list[dict[str, str]] = field(default_factory=list)
    route_changes: list[RouteChange] = field(default_factory=list)
    locks_blocking: list[dict[str, str]] = field(default_factory=list)
    estimate: dict[str, float] = field(default_factory=dict)

    @property
    def executes(self) -> list[str]:
        return [*self.regenerate, *self.cascade]

    def as_dict(self) -> dict[str, Any]:
        return {
            "regenerate": self.regenerate,
            "keep": self.keep,
            "cascade": self.cascade,
            "removed": self.removed,
            "no_visible_effect": self.no_visible_effect,
            "route_changes": [c.as_dict() for c in self.route_changes],
            "locks_blocking": self.locks_blocking,
            "estimate": self.estimate,
        }


def _route_changed(old: ExecutionNode, new: ExecutionNode) -> bool:
    if old.route is None and new.route is None:
        return False
    if old.route is None or new.route is None:
        return True
    return not new.route.same_route(old.route)


def diff_graph(
    old: ExecutionGraph | None,
    new: ExecutionGraph,
    *,
    evaluate: Evaluate | None = None,
    locks_blocking: Iterable[dict[str, str]] = (),
    parent_outputs: Mapping[str, str] | None = None,
) -> Impact:
    """`parent_outputs` (node key → output sha256) recognizes nodes that reuse the parent's artifact."""
    old_nodes = old.by_key() if old is not None else {}
    new_nodes = new.by_key()
    impact = Impact(locks_blocking=list(locks_blocking))
    executes: set[str] = set()
    changed_output: set[str] = set()
    cbs_changed: set[str] = set()
    outputs = parent_outputs or {}
    for node in new.nodes:  # topological order
        previous = old_nodes.get(node.key)
        if previous is None:
            impact.regenerate.append(node.key)
            executes.add(node.key)
            changed_output.add(node.key)
            if node.kind == "behavior.resolve":
                cbs_changed.add(node.key)
            continue
        reuse = node.params.get("reuse")
        if reuse is not None and outputs.get(node.key) == reuse:
            impact.keep.append(node.key)
            continue
        route_changed = _route_changed(previous, node)
        own_changed = previous.static_digest() != node.static_digest() or route_changed
        if route_changed:
            impact.route_changes.append(
                RouteChange(
                    node.key,
                    previous.route.adapter_id if previous.route else None,
                    node.route.adapter_id if node.route else None,
                    node.route.reason if node.route else "no longer routed",
                )
            )
        changed_deps = [d for d in node.deps if d in changed_output]
        rejudge = node.reads_requests and any(d in cbs_changed for d in node.deps)
        if not (own_changed or changed_deps or rejudge):
            impact.keep.append(node.key)
            continue
        (impact.regenerate if own_changed else impact.cascade).append(node.key)
        executes.add(node.key)
        evaluable = (
            evaluate is not None
            and node.kind in BEHAVIOR_EVALUATED_KINDS
            and not route_changed
            and all(new_nodes[d].kind in BEHAVIOR_EVALUATED_KINDS for d in changed_deps)
        )
        if evaluable:
            before, after = evaluate(previous, node)  # type: ignore[misc]
            if before is not None and before == after:
                what = "CBS" if node.kind == "behavior.resolve" else "compiled output"
                impact.no_visible_effect.append(
                    {"node_key": node.key, "reason": f"the change leaves the {what} unchanged"}
                )
                continue
        changed_output.add(node.key)
        if node.kind == "behavior.resolve":
            cbs_changed.add(node.key)
    new_keys = {n.key for n in new.nodes}
    impact.removed = [k for k in old_nodes if k not in new_keys]
    seconds = usd = 0.0
    model_nodes = 0
    for node in new.nodes:
        if node.key in executes and node.estimate:
            seconds += node.estimate.get("seconds", 0.0)
            usd += node.estimate.get("usd", 0.0)
            model_nodes += 1
    impact.estimate = {
        "nodes": float(len(executes)),
        "model_nodes": float(model_nodes),
        "gpu_seconds": round(seconds, 3),
        "usd": round(usd, 6),
    }
    return impact
