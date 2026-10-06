"""Model router (§23): hard filters, scoring, pinning, fallbacks and the route preview.

Status: implemented and tested in Phase 2. Routes are computed from plugin manifests, the
routing profile, the language table, GPU pools, the operator profile (license closure through
`ce_policy`) and measured data (behavior profiles, QC pass rates, benchmark quality). Measured
behavior profiles are produced from Phase 3 (mock) and Phase 8/11 (real); until then candidates
score neutral on that term.
"""

from ce_router.catalog import build_catalog, check_cpu_engines, cpu_unavailable, operator_from_config
from ce_router.router import (
    Candidate,
    MeasuredProfile,
    RouterCatalog,
    RouteRequest,
    RoutingError,
    estimate,
    fallback_route,
    hard_filter,
    preview,
    route,
    unreliable_dimensions,
)

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2

__all__ = [
    "Candidate",
    "MeasuredProfile",
    "RouteRequest",
    "RouterCatalog",
    "RoutingError",
    "build_catalog",
    "check_cpu_engines",
    "cpu_unavailable",
    "estimate",
    "fallback_route",
    "hard_filter",
    "operator_from_config",
    "preview",
    "route",
    "unreliable_dimensions",
]
