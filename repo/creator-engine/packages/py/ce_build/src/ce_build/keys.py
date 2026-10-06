"""Cache keys (§12.2): SHA-256 over canonical JSON of every input that can change a node's output.

The static part (kind, impl_version, spec fragment digest, referenced DNA/world/memory digests,
config digests, params, seed basis, take) is known when the graph is built; upstream artifact
hashes, the route identity and — for nodes that read requests — the CBS content digest join at run
time. A cache key never contains a version id.
"""

from __future__ import annotations

from collections.abc import Mapping

from ce_core.build import ExecutionNode
from ce_core.canonical import content_digest

__all__ = ["CACHE_KEY_VERSION", "cache_key"]

CACHE_KEY_VERSION = "ck1"


def cache_key(node: ExecutionNode, upstream: Mapping[str, str], cbs_content_digest: str | None = None) -> str:
    """`upstream` maps each dependency node key to its primary artifact's sha256."""
    missing = sorted(set(node.deps) - set(upstream))
    if missing:
        raise ValueError(f"{node.key}: upstream hashes missing for {missing}")
    if node.reads_requests and cbs_content_digest is None:
        raise ValueError(f"{node.key} reads behavior requests: the CBS content digest is part of its key")
    return content_digest(
        {
            "v": CACHE_KEY_VERSION,
            "static": node.static_digest(),
            "upstream": {dep: upstream[dep] for dep in sorted(node.deps)},
            "route": node.route.identity() if node.route else None,
            "cbs": cbs_content_digest if node.reads_requests else None,
        }
    )
