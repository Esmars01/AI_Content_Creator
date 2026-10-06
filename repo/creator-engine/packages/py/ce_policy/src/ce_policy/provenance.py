"""Provenance mode rule (ADR 0013, I11): production never runs with mock provenance."""

from __future__ import annotations

__all__ = [
    "AI_LABEL",
    "LABELLING_REGIONS",
    "MOCK_LABEL",
    "ProvenanceRefused",
    "burned_labels",
    "require_provenance_mode",
    "resolve_visible_label",
]

MOCK_LABEL = "MOCK PROVENANCE — NOT FOR DISTRIBUTION"  # ADR 0013
AI_LABEL = "AI-generated"
# Regions whose rules expect a visible disclosure on synthetic media by default: EU AI Act Art. 50,
# California SB 942 and China's labelling measures. `auto` resolves to on when any is served.
LABELLING_REGIONS = frozenset({"EU", "US-CA", "CN"})


class ProvenanceRefused(RuntimeError):
    """Raised when a render would use mock provenance where it is not allowed."""


def require_provenance_mode(mode: str, app_env: str) -> str:
    """Returns the mode a render must use, or refuses. `mock_dev` is allowed only in dev and test;
    in prod only `real` is accepted (invisible watermarks and C2PA cannot be disabled)."""
    if mode not in ("real", "mock_dev"):
        raise ProvenanceRefused(f"unknown provenance mode {mode!r}")
    if mode == "mock_dev" and app_env not in ("dev", "test"):
        raise ProvenanceRefused(f"PROVENANCE_MODE=mock_dev is not allowed with APP_ENV={app_env} (I11)")
    return mode


def resolve_visible_label(setting: str, regions_served: tuple[str, ...] | list[str]) -> bool:
    """`provenance.visible_label`: `on`/`off` as set; `auto` from the regions the operator serves."""
    if setting == "on":
        return True
    if setting == "off":
        return False
    return any(region in LABELLING_REGIONS for region in regions_served)


def burned_labels(mode: str, visible_label: bool) -> list[str]:
    """Labels a render burns into its frames: the mock-provenance warning (dev/test) and the AI label."""
    labels = [MOCK_LABEL] if mode == "mock_dev" else []
    if visible_label:
        labels.append(AI_LABEL)
    return labels
