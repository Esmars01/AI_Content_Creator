"""Coverage reports (§15.7, §16.4): requested vs compiled vs observed, one entry per requested
control (item × channel), in three stages:

- `predicted` (plan time, against the plan-time routes),
- `compiled` (build time, from the CompiledBehavior artifacts),
- `observed` (after `render.final`: viewer-level observations and `approximation_executed`).

Every requested control gets an entry (I4): an item no target rendered is reported UNSUPPORTED
(`omit`, "no shot or segment renders this item"), never left out. Outcomes follow `outcome_of`
(the 12 outcomes); only `*_CONFIRMED` counts as delivered (I9).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from ce_core.behavior.cbs import CBSContent, RequestedControl
from ce_core.behavior.compiled import Realization
from ce_core.behavior.coverage import (
    EDITORIAL_METHODS,
    BehaviorCoverageReport,
    CompiledPart,
    CoverageEntry,
    ObservedPart,
    display_summary,
    outcome_of,
)
from ce_core.behavior.observed import ItemObservation
from ce_core.enums import CoverageLevel, RealizationMethod
from ce_core.vocab import Vocabulary

from ce_behavior.judge import control_label

__all__ = ["NOT_RENDERED", "coverage_report", "requested_text"]

NOT_RENDERED = "no shot or segment renders this item"
Key = tuple[str, str]


def requested_text(control: RequestedControl, vocab: Vocabulary) -> str:
    """A readable request: the vocabulary description of the label plus the exact value."""
    label = control_label(control)
    for category in ("event_type", "emotion", f"strategy.{control.dimension}", "dimension"):
        entry = vocab.descriptions.get(category, {}).get(label)
        if entry is not None:
            return f"{entry.text} ({control.value})"
    return control.value


def _compiled_part(realization: Realization | None) -> CompiledPart:
    if realization is None:
        return CompiledPart(level=CoverageLevel.UNSUPPORTED, method=RealizationMethod.OMIT, detail=NOT_RENDERED)
    return CompiledPart(level=realization.level, method=realization.method, detail=realization.detail)


def coverage_report(
    cbs_by_scene: Mapping[str, CBSContent],
    realizations: Mapping[Key, Realization],
    vocab: Vocabulary,
    *,
    stage: Literal["predicted", "compiled", "observed"],
    version_id: UUID | None = None,
    observations: Mapping[Key, ItemObservation] | None = None,
    executed: Mapping[Key, bool | None] | None = None,
) -> BehaviorCoverageReport:
    entries: list[CoverageEntry] = []
    for scene_key in sorted(cbs_by_scene):
        cbs = cbs_by_scene[scene_key]
        for control in cbs.requested_controls:
            key = (control.item_ref, control.dimension)
            compiled = _compiled_part(realizations.get(key))
            editorial = compiled.method in EDITORIAL_METHODS
            observed = None
            outcome = None
            if stage == "observed":
                observation = (observations or {}).get(key)
                if observation is not None:
                    observed = ObservedPart(
                        verdict=observation.verdict,
                        measures=dict(observation.measures),
                        confidence=observation.confidence,
                    )
                    outcome = outcome_of(compiled.level, observation.verdict)
            entries.append(
                CoverageEntry(
                    item_ref=control.item_ref,
                    character_key=control.character_key,
                    dimension=control.dimension,
                    requested=requested_text(control, vocab),
                    compiled=compiled,
                    approximation_executed=(executed or {}).get(key) if editorial else None,
                    observed=observed,
                    outcome=outcome,
                    summary=display_summary(compiled.level, outcome, editorial),
                    expected_for_method=editorial,
                )
            )
    return BehaviorCoverageReport(stage=stage, version_id=version_id, entries=entries)
