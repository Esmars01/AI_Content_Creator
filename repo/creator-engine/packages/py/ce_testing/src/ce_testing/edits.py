"""Derived-version builds for dirty-set and edit tests (§12.4, §12.9, §19.5): a parent graph with a
fake BuildManifest and fake output hashes, and a child graph pinned to it with the behavior
evaluator, plus the impact `diff_graph` reports. Nothing touches a database or storage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ce_behavior.evaluate import BehaviorEvaluator, VersionInputs
from ce_build import BuildOptions, Impact, ParentBuild, assemble, build_graph, diff_graph, records_for
from ce_build.refs import BuildRefs
from ce_config.loader import ConfigBundle
from ce_core.build import ExecutionGraph
from ce_core.spec.videospec import VideoSpec
from ce_router import RouterCatalog

from ce_testing.build import config_bundle, example_build_refs, mock_catalog

__all__ = ["Built", "derive", "executed", "first_build", "parent_build_of", "propose_ops"]


@dataclass
class Built:
    spec: VideoSpec
    graph: ExecutionGraph
    outputs: dict[str, str] = field(default_factory=dict)
    impact: Impact | None = None

    def keys(self, *prefixes: str) -> set[str]:
        return {n.key for n in self.graph.nodes if not prefixes or n.key.startswith(prefixes)}


def _spec(data: VideoSpec | Mapping[str, Any]) -> VideoSpec:
    return data if isinstance(data, VideoSpec) else VideoSpec.model_validate(dict(data))


def first_build(
    data: VideoSpec | Mapping[str, Any],
    *,
    refs: BuildRefs | None = None,
    bundle: ConfigBundle | None = None,
    catalog: RouterCatalog | None = None,
    options: BuildOptions | None = None,
) -> Built:
    spec = _spec(data)
    graph = build_graph(
        spec,
        refs or example_build_refs(),
        bundle or config_bundle(),
        catalog or mock_catalog(),
        options=options or BuildOptions(),
    )
    return Built(spec, graph, {n.key: f"sha:{n.key}:{n.static_digest()[-12:]}" for n in graph.nodes})


def derive(
    parent: Built,
    data: VideoSpec | Mapping[str, Any],
    *,
    refs: BuildRefs | None = None,
    parent_refs: BuildRefs | None = None,
    bundle: ConfigBundle | None = None,
    catalog: RouterCatalog | None = None,
    options: BuildOptions | None = None,
) -> Built:
    """The child of `parent` as the build would plan it, with the impact of the edit."""
    spec = _spec(data)
    refs = refs or example_build_refs()
    bundle = bundle or config_bundle()
    catalog = catalog or mock_catalog()
    rows = [
        r for n in parent.graph.nodes for r in records_for(n, artifact_id=f"art:{n.key}", effective_seed=n.seed_base)
    ]
    pb = ParentBuild(parent.graph, assemble(rows), spec=parent.spec, outputs=dict(parent.outputs))
    evaluator = BehaviorEvaluator(
        VersionInputs(parent.spec, parent_refs or refs), VersionInputs(spec, refs), bundle, catalog
    )
    graph = build_graph(spec, refs, bundle, catalog, parent=pb, options=options or BuildOptions(), evaluate=evaluator)
    impact = diff_graph(parent.graph, graph, evaluate=evaluator, parent_outputs=pb.outputs)
    executes = set(impact.executes)
    outputs = {
        n.key: parent.outputs[n.key] if n.key not in executes and n.key in parent.outputs else f"sha:{n.key}:new"
        for n in graph.nodes
    }
    return replace(Built(spec, graph, outputs), impact=impact)


def executed(built: Built, *kinds: str) -> set[str]:
    """Node keys the derived build runs (regenerated or cascaded), optionally of some kinds."""
    assert built.impact is not None
    keys: Iterable[str] = built.impact.executes
    return {k for k in keys if not kinds or k.split(":", 1)[0] in kinds}


def parent_build_of(built: Built) -> ParentBuild:
    """The parent build of `built` as `load_parent` would read it (a fake manifest, fake outputs)."""
    rows = [
        r for n in built.graph.nodes for r in records_for(n, artifact_id=f"art:{n.key}", effective_seed=n.seed_base)
    ]
    return ParentBuild(built.graph, assemble(rows), spec=built.spec, outputs=dict(built.outputs))


def propose_ops(
    data: VideoSpec | Mapping[str, Any],
    operations: Iterable[Mapping[str, Any]],
    *,
    actor: str = "user",
    allow_lock_removal: bool = False,
    refs: BuildRefs | None = None,
) -> Any:
    """`compute_proposal` for typed operations (as dicts) on a built parent, without persistence."""
    from ce_core.edit.ops import parse_operations
    from ce_director.proposal import ProposalInputs, compute_proposal

    from ce_testing.fixtures import example_references

    built = first_build(data, refs=refs)
    refs = refs or example_build_refs()
    inputs = ProposalInputs(
        parent_spec=built.spec,
        parent_refs=refs,
        bundle=config_bundle(),
        catalog=mock_catalog(),
        references=example_references(),
        refs_for=lambda _spec: refs,
        parent_build=parent_build_of(built),
        actor=actor,
        allow_lock_removal=allow_lock_removal,
    )
    return compute_proposal(inputs, parse_operations([dict(o) for o in operations]))
