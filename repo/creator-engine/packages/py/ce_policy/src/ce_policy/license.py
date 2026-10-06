"""License policy engine (ADR 0012, §32): `evaluate(closure, operator_profile, context)`.

The closure is a model plus every weight it depends on; the most restrictive license decides.
Rules, in order (every failing rule adds a reason; any reason denies):

- `commercial_use: false` or a `non_commercial` condition → deny outside the sandbox;
- `territories_excluded` ∩ (jurisdiction ∪ regions served) → deny ("EU" also matches EU member codes);
- `revenue_cap_usd` / `mau_cap` above the operator's band → deny, unless the operator profile
  records a license confirmation for that ref or license name;
- `maas_restricted` while the operator offers model-as-a-service → deny;
- `attribution_text` → an obligation (UI, docs, export metadata);
- `copyleft` → an obligation (license notice and source offer when distributing);
- `use_restrictions` (OpenRAIL-style use-based restrictions) → an obligation: the restrictions are
  passed on to users in the terms of use (they bind how outputs may be used, not whether the
  model may run commercially).

Whether a plugin is *enabled* is a separate product decision, never decided here (§32).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ce_contracts.manifest import LicenseBlock

__all__ = ["EU_MEMBERS", "REVENUE_BANDS", "Decision", "OperatorProfile", "evaluate"]

EU_MEMBERS = frozenset(
    [
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    ]
)
# Lower bound (USD) of each operator revenue band.
REVENUE_BANDS: dict[str, int] = {"lt_1m": 0, "1m_10m": 1_000_000, "gte_10m": 10_000_000}
MAU_BANDS: dict[str, int] = {"lt_100k": 0, "100k_1m": 100_000, "gte_1m": 1_000_000}


@dataclass(frozen=True)
class OperatorProfile:
    """Who operates this installation (§29 `operator_profiles`, defaults in config/policy, §41)."""

    jurisdiction: str = "EU"
    regions_served: tuple[str, ...] = ("EU",)
    revenue_band: str = "lt_1m"
    mau_band: str | None = None
    offers_model_as_service: bool = False
    license_confirmations: Mapping[str, Any] = field(default_factory=dict)

    def territories(self) -> set[str]:
        values = {self.jurisdiction.upper(), *(r.upper() for r in self.regions_served)}
        if values & EU_MEMBERS:
            values.add("EU")
        if "EU" in values:
            values |= EU_MEMBERS
        return values

    def confirmed(self, ref: str, license_name: str) -> bool:
        return bool(self.license_confirmations.get(ref) or self.license_confirmations.get(license_name))


@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "deny", "allow_with_obligations"]
    reasons: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict != "deny"


def _territory_set(value: Any) -> set[str]:
    items = value if isinstance(value, list | tuple | set) else [value]
    out = {str(v).upper() for v in items}
    if "EU" in out:
        out |= EU_MEMBERS
    return out


def evaluate(
    closure: Iterable[tuple[str, LicenseBlock]],
    operator: OperatorProfile,
    *,
    sandbox: bool = False,
) -> Decision:
    reasons: list[str] = []
    obligations: list[str] = []
    territories = operator.territories()
    for ref, lic in closure:
        if (not lic.commercial_use or lic.condition("non_commercial")) and not sandbox:
            reasons.append(f"{ref}: {lic.name} does not allow commercial use (sandbox only)")
        excluded = lic.condition("territories_excluded")
        if excluded:
            hit = sorted(_territory_set(excluded) & territories)
            if hit:
                reasons.append(f"{ref}: {lic.name} excludes territories served by the operator ({', '.join(hit[:5])})")
        cap = lic.condition("revenue_cap_usd")
        if cap is not None and not operator.confirmed(ref, lic.name):
            lower = REVENUE_BANDS.get(operator.revenue_band)
            if lower is None or lower >= float(cap):
                reasons.append(
                    f"{ref}: {lic.name} requires a separate license at revenue ≥ ${float(cap):,.0f} "
                    f"(operator band {operator.revenue_band}, no license confirmation)"
                )
        mau_cap = lic.condition("mau_cap")
        if mau_cap is not None and not operator.confirmed(ref, lic.name):
            lower = MAU_BANDS.get(operator.mau_band or "")
            if lower is None or lower >= float(mau_cap):
                band = operator.mau_band or "unknown"
                reasons.append(
                    f"{ref}: {lic.name} caps monthly users at {mau_cap} (operator band {band}, no confirmation)"
                )
        if lic.condition("maas_restricted") and operator.offers_model_as_service:
            reasons.append(f"{ref}: {lic.name} restricts model-as-a-service, which the operator offers")
        attribution = lic.condition("attribution_text")
        if attribution:
            obligations.append(str(attribution))
        restrictions = lic.condition("use_restrictions")
        if restrictions:
            obligations.append(
                f"{ref} is licensed under {lic.name} with use-based restrictions ({restrictions}): the operator's "
                "terms of use must pass them on to users"
            )
        copyleft = lic.condition("copyleft")
        if copyleft:
            obligations.append(
                f"{ref} is {copyleft}: distributing software that includes it carries the copyleft terms "
                "(license notice and source offer)"
            )
    if reasons:
        return Decision("deny", tuple(reasons), tuple(obligations))
    if obligations:
        return Decision("allow_with_obligations", (), tuple(dict.fromkeys(obligations)))
    return Decision("allow")
