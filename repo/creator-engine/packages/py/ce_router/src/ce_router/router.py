"""`route(request, catalog) -> RouteDecision` (§23).

1. Hard filters: capability and features; language (validated always, unvalidated only for
   `beta` languages); resolution and duration limits; license policy over the model's closure;
   plugin status (`production`, `sandbox` only in sandbox runs) and validation (production needs
   ≥ smoke_passed, or `MOCK_GPU=true` for mocks); `allowed_envs`; a pool that serves the family
   with enough VRAM; health; the product decision `enabled` (disabled plugins never route); and
   real CPU engines that `CPU_REAL_ENGINES` or missing assets make unavailable. When a real
   implementation passes for a request, the mocks are dropped (§1 rule 11: real wherever it can
   run), unless the input itself was made by a mock engine (`prefer_mock`: only a mock ASR can
   read a mock TTS's tones).
2. Score: the routing profile's weighted sum of quality (benchmark quality and historical QC pass
   rate), measured behavior success for the requested dimensions (mock-sourced profiles count only
   with `MOCK_GPU=true`), and estimated cost and latency (normalized across candidates). Missing
   measurements score a neutral 0.5 — declared abilities are claims, not evidence (ADR 0027) —
   except where the matrix declares the dimension `none` or `emergent`: a declared inability
   scores 0 (the compiler never sends that request to the engine).
3. Pinning: a pinned route that still passes the hard filters is reused (§12.4).
4. User pins (`engine_hints`) win when they pass the filters.
5. Fallbacks: the other passing candidates by score, up to the profile's chain depth.

Everything here is pure: the caller loads the catalog (manifests, config, DB measurements).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ce_config.schemas import GpuPool, LanguageEntry, RoutingProfile
from ce_contracts.manifest import PluginManifest, license_closure
from ce_core.build import RouteDecision
from ce_policy.license import OperatorProfile, evaluate

__all__ = [
    "Candidate",
    "MeasuredProfile",
    "RouteRequest",
    "RouterCatalog",
    "RoutingError",
    "fallback_route",
    "hard_filter",
    "preview",
    "route",
    "unreliable_dimensions",
]

PRODUCTION_VALIDATIONS = frozenset({"smoke_passed", "bench_passed"})
NEUTRAL = 0.5
UNDIRECTABLE = frozenset({"none", "emergent"})  # declared controls that cannot carry a request


class RoutingError(LookupError):
    """No adapter passes the hard filters. `rejections` maps adapter id → reasons."""

    def __init__(self, capability: str, rejections: Mapping[str, list[str]]) -> None:
        self.capability = capability
        self.rejections = dict(rejections)
        detail = (
            "; ".join(f"{a}: {', '.join(r)}" for a, r in sorted(self.rejections.items())) or "no adapter declares it"
        )
        super().__init__(f"no route for {capability}: {detail}")


@dataclass(frozen=True)
class MeasuredProfile:
    success_rate: float
    n: int
    source: str  # mock | bench | production


@dataclass
class RouterCatalog:
    manifests: Mapping[str, PluginManifest]
    profiles: Mapping[str, RoutingProfile]
    languages: Mapping[str, LanguageEntry]
    pools: Sequence[GpuPool]
    gpu_class_vram: Mapping[str, float]
    operator: OperatorProfile
    app_env: str
    mock_gpu: bool
    gpu_providers: frozenset[str] = frozenset({"mock"})  # registered GPUProvider plugin keys
    gpu_prices: Mapping[str, float] = field(default_factory=dict)  # USD/hour per GPU class
    disabled: frozenset[str] = frozenset()
    unhealthy: frozenset[str] = frozenset()
    measured: Mapping[tuple[str, str], MeasuredProfile] = field(default_factory=dict)  # (adapter, dimension)
    quality: Mapping[tuple[str, str], float] = field(default_factory=dict)  # (adapter, capability)
    qc_pass_rate: Mapping[tuple[str, str], float] = field(default_factory=dict)  # (adapter, capability)
    cpu_unavailable: Mapping[str, str] = field(default_factory=dict)  # real CPU engine id → why it is off


@dataclass(frozen=True)
class RouteRequest:
    capability: str
    routing_profile: str = "draft"
    features: frozenset[str] = frozenset()
    language: str | None = None
    height: int | None = None
    width: int | None = None  # with it, `Np` resolutions bound the short side (720p = 720×1280 portrait)
    duration_s: float | None = None
    requested: Mapping[str, float] = field(default_factory=dict)  # dimension → priority weight
    engine_hint: str | None = None
    pinned: RouteDecision | None = None
    exclude: frozenset[str] = frozenset()
    sandbox: bool = False
    work_units: float = 1.0  # output seconds (or items) the estimate scales with
    prefer_mock: bool = False  # the input comes from a mock engine: real engines cannot interpret it


@dataclass
class Candidate:
    manifest: PluginManifest
    score: float = 0.0
    terms: dict[str, float] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.manifest.id


def _primary(language: str) -> str:
    return language.split("-")[0].lower()


def _pool_fits(manifest: PluginManifest, catalog: RouterCatalog) -> bool:
    """An enabled pool serves the family with enough VRAM through a registered provider. Pools whose
    only provider is `mock` run mock adapters only (simulated workers cannot run real engines)."""
    family = manifest.runtime.family
    if family == "cpu_inproc":
        return True
    for pool in catalog.pools:
        if not pool.enabled or family not in pool.families:
            continue
        providers = set(pool.providers) & catalog.gpu_providers
        if not providers or (not manifest.mock and providers == {"mock"}):
            continue
        if any(catalog.gpu_class_vram.get(c, 0.0) >= manifest.runtime.min_vram_gb for c in pool.gpu_classes):
            return True
    return False


def hard_filter(manifest: PluginManifest, request: RouteRequest, catalog: RouterCatalog) -> list[str]:
    """Reasons this adapter cannot serve the request (empty = it passes)."""
    reasons: list[str] = []
    decl = manifest.capability(request.capability)
    if decl is None:
        return [f"does not declare {request.capability}"]
    missing = sorted(request.features - set(decl.features))
    if missing:
        reasons.append(f"lacks features {missing}")
    if request.language and (decl.languages.validated or decl.languages.unvalidated):
        lang = _primary(request.language)
        validated = {_primary(x) for x in decl.languages.validated}
        unvalidated = {_primary(x) for x in decl.languages.unvalidated}
        support = catalog.languages.get(lang)
        if lang not in validated:
            if lang in unvalidated and support is not None and support.support == "beta":
                pass
            elif lang in unvalidated:
                reasons.append(f"language {lang} is unvalidated and its support is not beta")
            else:
                reasons.append(f"language {lang} not supported")
    max_height = decl.max_height()
    # "720p" names the short side of a frame in either orientation; without a width, the height is compared
    side = min(request.height, request.width) if request.height and request.width else request.height
    if side and max_height and side > max_height:
        reasons.append(f"{'short side' if request.width else 'height'} {side} above {max_height}")
    if request.duration_s and decl.max_duration_s and request.duration_s > decl.max_duration_s:
        reasons.append(f"duration {request.duration_s:.1f}s above {decl.max_duration_s:.0f}s")
    decision = evaluate(license_closure(manifest), catalog.operator, sandbox=request.sandbox)
    if not decision.allowed:
        reasons += [f"license: {r}" for r in decision.reasons]
    profile = catalog.profiles.get(request.routing_profile)
    allowed_statuses = {str(s) for s in (profile.allowed_statuses if profile else ["production"])}
    if request.sandbox:
        allowed_statuses.add("sandbox")
    if manifest.status not in allowed_statuses:
        reasons.append(f"status {manifest.status}")
    if manifest.mock:
        if not catalog.mock_gpu:
            reasons.append("mock adapter while MOCK_GPU=false")
    elif manifest.status == "production" and not request.sandbox and manifest.validation not in PRODUCTION_VALIDATIONS:
        reasons.append(f"validation {manifest.validation} (production routing needs smoke_passed)")
    if catalog.app_env not in manifest.allowed_envs:
        reasons.append(f"not allowed in APP_ENV={catalog.app_env}")
    if not _pool_fits(manifest, catalog):
        reasons.append(
            f"no enabled pool serves family {manifest.runtime.family} with {manifest.runtime.min_vram_gb} GB"
        )
    if manifest.id in catalog.unhealthy:
        reasons.append("unhealthy")
    if manifest.id in catalog.disabled:
        reasons.append("disabled (product decision)")
    if manifest.id in catalog.cpu_unavailable:
        reasons.append(f"real CPU engine unavailable: {catalog.cpu_unavailable[manifest.id]}")
    if manifest.id in request.exclude:
        reasons.append("excluded by the caller (fallback escalation)")
    return reasons


def _estimate(manifest: PluginManifest, request: RouteRequest, catalog: RouterCatalog) -> tuple[float, float]:
    """(seconds, USD) from the manifest's pricing hint and the cheapest pool price for its family."""
    seconds = float(manifest.pricing.get("seconds_per_unit", 1.0)) * max(request.work_units, 0.1)
    if manifest.runtime.family == "cpu_inproc":
        return seconds, 0.0
    prices = [
        catalog.gpu_prices.get(c, 0.0)
        for pool in catalog.pools
        if pool.enabled and manifest.runtime.family in pool.families
        for c in pool.gpu_classes
    ]
    hourly = min(prices) if prices else 0.0
    return seconds, seconds * hourly / 3600.0


def estimate(adapter_id: str, request: RouteRequest, catalog: RouterCatalog) -> tuple[float, float]:
    """Planning estimate (seconds, USD) of running `request` on `adapter_id` (impact preview, §12.9)."""
    return _estimate(catalog.manifests[adapter_id], request, catalog)


def _quality(manifest: PluginManifest, request: RouteRequest, catalog: RouterCatalog) -> float:
    values = [
        v
        for v in (
            catalog.quality.get((manifest.id, request.capability)),
            catalog.qc_pass_rate.get((manifest.id, request.capability)),
        )
        if v is not None
    ]
    return sum(values) / len(values) if values else NEUTRAL


def _behavior(manifest: PluginManifest, request: RouteRequest, catalog: RouterCatalog) -> float:
    if not request.requested:
        return NEUTRAL
    total = weight_sum = 0.0
    matrix = manifest.behavior_matrix
    for dimension, weight in request.requested.items():
        profile = catalog.measured.get((manifest.id, dimension))
        if profile is not None and (profile.source != "mock" or catalog.mock_gpu):
            value = profile.success_rate
        elif matrix is not None and matrix.control(dimension).control in UNDIRECTABLE:
            value = 0.0  # a declared inability is not a claim awaiting evidence (ADR 0027)
        else:
            value = NEUTRAL
        total += weight * value
        weight_sum += weight
    return total / weight_sum if weight_sum else NEUTRAL


def unreliable_dimensions(
    catalog: RouterCatalog, adapter_id: str, *, threshold: float, min_n: int = 5
) -> frozenset[str]:
    """Dimensions whose measured success rate on `adapter_id` is below `threshold` (§16.6): the
    compiler plans those declared controls as APPROXIMATED. Mock profiles count only with
    `MOCK_GPU=true`; fewer than `min_n` observations prove nothing."""
    out: set[str] = set()
    for (adapter, dimension), profile in catalog.measured.items():
        if adapter != adapter_id or profile.n < min_n or (profile.source == "mock" and not catalog.mock_gpu):
            continue
        if profile.success_rate < threshold:
            out.add(dimension)
    return frozenset(out)


def _normalize_low_better(values: Mapping[str, float]) -> dict[str, float]:
    lo, hi = min(values.values()), max(values.values())
    if math.isclose(lo, hi):
        return dict.fromkeys(values, 1.0)
    return {k: 1.0 - (v - lo) / (hi - lo) for k, v in values.items()}


def _score(candidates: list[Candidate], request: RouteRequest, catalog: RouterCatalog) -> None:
    profile = catalog.profiles[request.routing_profile]
    w = profile.weights
    seconds: dict[str, float] = {}
    cost: dict[str, float] = {}
    for c in candidates:
        seconds[c.id], cost[c.id] = _estimate(c.manifest, request, catalog)
    latency_n = _normalize_low_better(seconds)
    cost_n = _normalize_low_better(cost)
    for c in candidates:
        c.terms = {
            "quality": _quality(c.manifest, request, catalog),
            "measured_behavior": _behavior(c.manifest, request, catalog),
            "cost": cost_n[c.id],
            "latency": latency_n[c.id],
            "est_seconds": seconds[c.id],
            "est_usd": cost[c.id],
        }
        c.score = round(
            w.quality * c.terms["quality"]
            + w.measured_behavior * c.terms["measured_behavior"]
            + w.cost * c.terms["cost"]
            + w.latency * c.terms["latency"],
            6,
        )


def _decision(
    candidate: Candidate, request: RouteRequest, *, reason: str, fallbacks: list[str], pinned: bool
) -> RouteDecision:
    model = candidate.manifest.primary_model
    translator_version: str | None = None
    if candidate.manifest.behavior_translator:
        translator_version = candidate.manifest.defaults.get("translator_version") or candidate.manifest.version
    return RouteDecision(
        adapter_id=candidate.id,
        model_id=model.key if model else candidate.id,
        revision=model.source.revision if model else candidate.manifest.version,
        translator_version=translator_version,
        routing_profile=request.routing_profile,
        reason=reason,
        score=candidate.score,
        reasons=[f"{k}={v:.3f}" for k, v in sorted(candidate.terms.items())],
        fallbacks=fallbacks,
        pinned=pinned,
    )


def _passing(request: RouteRequest, catalog: RouterCatalog) -> tuple[list[Candidate], dict[str, list[str]]]:
    passing: list[Candidate] = []
    rejected: dict[str, list[str]] = {}
    for manifest in catalog.manifests.values():
        if manifest.capability(request.capability) is None:
            continue
        reasons = hard_filter(manifest, request, catalog)
        if reasons:
            rejected[manifest.id] = reasons
        else:
            passing.append(Candidate(manifest))
    real = sorted(c.id for c in passing if not c.manifest.mock)
    mocks = sorted(c.id for c in passing if c.manifest.mock)
    if request.prefer_mock and mocks and real:
        for c in [c for c in passing if not c.manifest.mock]:
            rejected[c.id] = [f"input made by a mock engine; kept on the mock {', '.join(mocks)}"]
        return [c for c in passing if c.manifest.mock], rejected
    if real and mocks:  # §1 rule 11: real implementations wherever they can run; mocks only otherwise
        for c in [c for c in passing if c.manifest.mock]:
            rejected[c.id] = [f"mock replaced by the real implementation {', '.join(real)}"]
        passing = [c for c in passing if not c.manifest.mock]
    return passing, rejected


def fallback_route(planned: RouteDecision, adapter_id: str, catalog: RouterCatalog) -> RouteDecision:
    """The route of a QC ladder fallback (§26): `adapter_id` from `planned.fallbacks` (precomputed
    from the candidates that passed the hard filters), with its own model, revision and translator."""
    if adapter_id not in planned.fallbacks:
        raise ValueError(f"{adapter_id!r} is not a fallback of {planned.adapter_id!r} ({planned.fallbacks})")
    manifest = catalog.manifests.get(adapter_id)
    if manifest is None:
        raise ValueError(f"fallback adapter {adapter_id!r} is not installed")
    model = manifest.primary_model
    translator_version: str | None = None
    if manifest.behavior_translator:
        translator_version = manifest.defaults.get("translator_version") or manifest.version
    return RouteDecision(
        adapter_id=adapter_id,
        model_id=model.key if model else adapter_id,
        revision=model.source.revision if model else manifest.version,
        translator_version=translator_version,
        routing_profile=planned.routing_profile,
        reason=f"QC ladder fallback from {planned.adapter_id} (§26)",
        params=dict(planned.params),
        fallbacks=[a for a in planned.fallbacks if a != adapter_id],
    )


def route(request: RouteRequest, catalog: RouterCatalog) -> RouteDecision:
    if request.routing_profile not in catalog.profiles:
        raise ValueError(f"unknown routing profile {request.routing_profile!r}")
    passing, rejected = _passing(request, catalog)
    if not passing:
        raise RoutingError(request.capability, rejected)
    _score(passing, request, catalog)
    ranked = sorted(passing, key=lambda c: (-c.score, c.id))
    depth = catalog.profiles[request.routing_profile].fallback_chain_depth
    by_id = {c.id: c for c in ranked}

    def fallbacks_for(chosen: str) -> list[str]:
        return [c.id for c in ranked if c.id != chosen][:depth]

    if request.pinned is not None and request.pinned.adapter_id in by_id:
        chosen = by_id[request.pinned.adapter_id]
        decision = _decision(
            chosen,
            request,
            reason="pinned (clean node or locked group, §12.4)",
            fallbacks=fallbacks_for(chosen.id),
            pinned=True,
        )
        if decision.same_route(request.pinned):
            return decision
    if request.engine_hint and request.engine_hint in by_id:
        chosen = by_id[request.engine_hint]
        return _decision(
            chosen, request, reason="user pin (engine_hints)", fallbacks=fallbacks_for(chosen.id), pinned=False
        )
    best = ranked[0]
    reason = "highest score"
    if request.pinned is not None:
        reason = f"re-routed: pinned {request.pinned.adapter_id} no longer passes the hard filters or changed revision"
    return _decision(best, request, reason=reason, fallbacks=fallbacks_for(best.id), pinned=False)


def preview(capability: str, language: str | None, routing_profile: str, catalog: RouterCatalog) -> list[str]:
    """Route preview (§23): candidates passing only the capability, language and profile filters
    (Director stage 8 uses it before a CBS exists)."""
    request = RouteRequest(capability=capability, routing_profile=routing_profile, language=language)
    profile = catalog.profiles[routing_profile]
    out: list[str] = []
    for manifest in catalog.manifests.values():
        reasons = hard_filter(manifest, request, catalog)
        relevant = [r for r in reasons if r.startswith(("does not declare", "language", "status", "lacks features"))]
        if not relevant and manifest.status in {str(s) for s in profile.allowed_statuses}:
            out.append(manifest.id)
    return sorted(out)
