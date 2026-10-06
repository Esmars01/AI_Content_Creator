"""Execution graph, node keys, cache keys, BuildManifest, seeds, route pinning and dirty analysis (§12).

Status: implemented and tested in Phase 2. `build_graph` derives the graph from a VideoSpec for
every §12.1 node kind (behavior nodes are pass-through stubs until Phase 3; `silent_hold` and
`two_shot` shots are built from Phase 5/7, §39.5). Routes are pinned to the parent version's
manifest for clean nodes and for locked groups; cache keys are content-only (no version ids).
Phase 6: behavior-aware dirty propagation (generation digests, an evaluate hook, output-change
propagation), per-camera-position plate views (§19.5), the voice lock's content-pinned outputs,
pacing as re-performance vs editorial, and lip-sync patch builds (ADR 0041).
"""

from ce_build.dirty import Impact, RouteChange, diff_graph
from ce_build.graph import BuildOptions, GraphError, ParentBuild, build_graph, generation_size, shot_words
from ce_build.keys import CACHE_KEY_VERSION, cache_key
from ce_build.kinds import KINDS, NodeKind
from ce_build.manifest import (
    ManifestError,
    NodeRecord,
    assemble,
    manifest_from_planned,
    parent_build,
    planned_routes,
    records_for,
)
from ce_build.refs import BuildRefs
from ce_build.seeds import attempt_seed, default_seed, seed_basis

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2

__all__ = [
    "CACHE_KEY_VERSION",
    "KINDS",
    "BuildOptions",
    "BuildRefs",
    "GraphError",
    "Impact",
    "ManifestError",
    "NodeKind",
    "NodeRecord",
    "ParentBuild",
    "RouteChange",
    "assemble",
    "attempt_seed",
    "build_graph",
    "cache_key",
    "default_seed",
    "diff_graph",
    "generation_size",
    "manifest_from_planned",
    "parent_build",
    "planned_routes",
    "records_for",
    "seed_basis",
    "shot_words",
]
