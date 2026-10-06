"""BehaviorCoverageReport (§15.7, §16.4): requested vs compiled vs observed, and the 12 outcomes."""

from __future__ import annotations

from collections import Counter
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from ce_core.enums import CoverageLevel, ObservationVerdict, Outcome, RealizationMethod
from ce_core.keys import CharacterKey
from ce_core.scalars import Token, Unit
from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPathStr

__all__ = [
    "EDITORIAL_METHODS",
    "BehaviorCoverageReport",
    "CompiledPart",
    "CoverageEntry",
    "ObservedPart",
    "display_summary",
    "is_delivered",
    "outcome_of",
]

EDITORIAL_METHODS = frozenset(
    {
        RealizationMethod.EDITORIAL_CUTAWAY,
        RealizationMethod.EDITORIAL_PUNCH,
        RealizationMethod.CAPTION_EMPHASIS,
        RealizationMethod.MUSIC_CUE,
        RealizationMethod.SFX_CUE,
    }
)


def outcome_of(level: CoverageLevel, verdict: ObservationVerdict | None) -> Outcome | None:
    """Outcome = `{level}_{verdict}` with the special cases of §16.4. None before observation."""
    if verdict is None:
        return None
    if verdict == ObservationVerdict.NOT_MEASURABLE:
        return Outcome.NOT_MEASURABLE
    if verdict == ObservationVerdict.NOT_APPLICABLE:
        return Outcome.NOT_APPLICABLE
    if level == CoverageLevel.UNSUPPORTED:
        observed = verdict in {ObservationVerdict.CONFIRMED, ObservationVerdict.PARTIAL}
        return Outcome.UNSUPPORTED_OBSERVED if observed else Outcome.UNSUPPORTED
    return Outcome(f"{level}_{verdict}")


def is_delivered(outcome: Outcome | None) -> bool:
    """Only `*_CONFIRMED` counts as delivered (I9)."""
    return outcome in {Outcome.HONORED_CONFIRMED, Outcome.APPROXIMATED_CONFIRMED}


def display_summary(level: CoverageLevel, outcome: Outcome | None, expected_for_method: bool) -> str:
    """The UI wording of §16.4 display rules."""
    # Models store enum values as plain strings (use_enum_values), so compare on str().
    if outcome is None:
        return f"{level} (not observed yet)"
    name = str(outcome)
    if name in {Outcome.NOT_MEASURABLE, Outcome.NOT_APPLICABLE}:
        return f"{level} → {name.replace('_', ' ')}"
    if name in {Outcome.UNSUPPORTED, Outcome.UNSUPPORTED_OBSERVED}:
        return "UNSUPPORTED" if name == Outcome.UNSUPPORTED else "UNSUPPORTED → OBSERVED (emergent)"
    if name.endswith(("_NOT_OBSERVED", "_CONTRADICTED")):
        suffix = " (expected: approximated editorially, not visible on the face)" if expected_for_method else ""
        return f"{level} → FAILED OBSERVATION{suffix}"
    if name.endswith("_PARTIAL"):
        return f"{level} → PARTIAL"
    return f"{level} → CONFIRMED"


class CompiledPart(SpecModel):
    level: CoverageLevel
    method: RealizationMethod
    detail: str = ""


class ObservedPart(SpecModel):
    verdict: ObservationVerdict
    measures: dict[str, float] = Field(default_factory=dict)
    confidence: Unit = 0.0


class CoverageEntry(SpecModel):
    item_ref: SpecPathStr
    character_key: CharacterKey
    dimension: Token
    requested: str
    compiled: CompiledPart
    approximation_executed: bool | None = None
    observed: ObservedPart | None = None
    outcome: Outcome | None = None
    summary: str = ""
    expected_for_method: bool = False

    @model_validator(mode="after")
    def _outcome_consistent(self) -> CoverageEntry:
        expected = outcome_of(self.compiled.level, self.observed.verdict if self.observed else None)
        if self.outcome != expected:
            raise ValueError(
                f"outcome {self.outcome} does not follow from {self.compiled.level} + observation ({expected})"
            )
        if self.expected_for_method != (self.compiled.method in EDITORIAL_METHODS):
            raise ValueError("expected_for_method is true exactly for editorial methods")
        return self


class BehaviorCoverageReport(SpecModel):
    stage: Literal["predicted", "compiled", "observed"]
    version_id: UUID | None = None
    entries: list[CoverageEntry] = Field(default_factory=list)

    def summary_counts(self) -> dict[str, int]:
        """Counts stored in `video_versions.coverage_summary`."""
        counts: Counter[str] = Counter()
        for entry in self.entries:
            counts[f"level:{entry.compiled.level}"] += 1
            if entry.outcome is not None:
                counts[f"outcome:{entry.outcome}"] += 1
                if is_delivered(entry.outcome):
                    counts["delivered"] += 1
        return dict(sorted(counts.items()))
