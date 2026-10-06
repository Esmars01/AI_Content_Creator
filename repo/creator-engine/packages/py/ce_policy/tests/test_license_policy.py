"""License policy engine (ADR 0012, §32): rules over the license closure and the operator profile."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from ce_contracts.manifest import LicenseBlock
from ce_policy import OperatorProfile, ProvenanceRefused, evaluate, require_provenance_mode


def lic(name: str = "Apache-2.0", *, commercial: bool = True, **conditions: Any) -> LicenseBlock:
    return LicenseBlock(
        name=name,
        url="https://example.test/LICENSE",
        commercial_use=commercial,
        conditions=[{k: v} for k, v in conditions.items()],
        verified_at=date(2026, 10, 3),
        verified_by="test",
    )


EU = OperatorProfile()  # the §41 default: EU-serving, revenue below $1M


def test_permissive_closure_is_allowed() -> None:
    decision = evaluate([("m@1", lic()), ("dep@2", lic("MIT"))], EU)
    assert decision.verdict == "allow" and decision.allowed


def test_non_commercial_weights_or_dependencies_are_denied_outside_the_sandbox() -> None:
    closure = [("model@1", lic()), ("insightface@x", lic("non-commercial", commercial=False))]
    denied = evaluate(closure, EU)
    assert not denied.allowed and "insightface@x" in denied.reasons[0]
    assert evaluate(closure, EU, sandbox=True).allowed
    flagged = [("m@1", lic("custom", non_commercial=True))]
    assert not evaluate(flagged, EU).allowed


def test_territory_exclusions_follow_jurisdiction_and_regions_served() -> None:
    minimax = [("minimax-h3@1", lic("MiniMax", territories_excluded=["US", "EU", "UK", "KR"]))]
    assert not evaluate(minimax, EU).allowed  # MiniMax H3 is denied for an EU operator (license policy, ADR 0012)
    assert not evaluate(minimax, OperatorProfile(jurisdiction="JP", regions_served=("DE",))).allowed  # DE is EU
    assert not evaluate(minimax, OperatorProfile(jurisdiction="US", regions_served=("US",))).allowed
    assert evaluate(minimax, OperatorProfile(jurisdiction="JP", regions_served=("JP",))).allowed


@pytest.mark.parametrize(
    ("band", "cap", "allowed"),
    [
        ("lt_1m", 10_000_000, True),
        ("1m_10m", 10_000_000, True),
        ("gte_10m", 10_000_000, False),
        ("1m_10m", 1_000_000, False),
    ],
)
def test_revenue_caps(band: str, cap: int, allowed: bool) -> None:
    closure = [("ltx-2@1", lic("LTX-2", revenue_cap_usd=cap))]
    assert evaluate(closure, OperatorProfile(revenue_band=band)).allowed is allowed


def test_a_license_confirmation_lifts_a_revenue_cap() -> None:
    closure = [("ltx-2@1", lic("LTX-2", revenue_cap_usd=10_000_000))]
    big = OperatorProfile(revenue_band="gte_10m")
    assert not evaluate(closure, big).allowed
    assert evaluate(closure, OperatorProfile(revenue_band="gte_10m", license_confirmations={"LTX-2": True})).allowed


def test_mau_caps_need_a_known_band() -> None:
    closure = [("m@1", lic("X", mau_cap=1_000_000))]
    assert not evaluate(closure, EU).allowed  # unknown band: conservative
    assert evaluate(closure, OperatorProfile(mau_band="lt_100k")).allowed
    assert not evaluate(closure, OperatorProfile(mau_band="gte_1m")).allowed


def test_model_as_a_service_restrictions() -> None:
    closure = [("qwen-flash@1", lic("Qwen Community", maas_restricted=True))]
    assert evaluate(closure, EU).allowed
    assert not evaluate(closure, OperatorProfile(offers_model_as_service=True)).allowed


def test_attribution_becomes_an_obligation() -> None:
    decision = evaluate([("dnsmos@1", lic("CC-BY-4.0", attribution_text="DNSMOS © Microsoft, CC-BY-4.0"))], EU)
    assert decision.verdict == "allow_with_obligations"
    assert decision.obligations == ("DNSMOS © Microsoft, CC-BY-4.0",)


def test_use_based_restrictions_become_an_obligation_not_a_denial() -> None:
    """OpenRAIL-style licenses (MuseTalk's weights, Phase 8) allow commercial use with use-based
    restrictions that must be passed on to users: allowed, with the obligation recorded."""
    decision = evaluate([("musetalk@1", lic("CreativeML-OpenRAIL-M", use_restrictions="Attachment A"))], EU)
    assert decision.verdict == "allow_with_obligations"
    assert "Attachment A" in decision.obligations[0] and "terms of use" in decision.obligations[0]


def test_the_most_restrictive_license_of_the_closure_wins() -> None:
    closure = [("a@1", lic(attribution_text="credit")), ("b@1", lic("X", territories_excluded=["EU"]))]
    decision = evaluate(closure, EU)
    assert decision.verdict == "deny" and decision.obligations == ("credit",)


def test_provenance_mode_rule() -> None:
    assert require_provenance_mode("mock_dev", "dev") == "mock_dev"
    assert require_provenance_mode("real", "prod") == "real"
    with pytest.raises(ProvenanceRefused):
        require_provenance_mode("mock_dev", "prod")
    with pytest.raises(ProvenanceRefused):
        require_provenance_mode("fake", "dev")
