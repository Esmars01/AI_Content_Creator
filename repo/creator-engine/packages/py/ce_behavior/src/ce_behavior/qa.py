"""Performance QA (§16.5, `config/qc/behavior.yaml`) and retry targets by method (§15.7).

- `must` items: `HONORED_NOT_OBSERVED` / `HONORED_CONTRADICTED`, or `APPROXIMATED_NOT_OBSERVED` /
  `APPROXIMATED_CONTRADICTED` when the method is not editorial, request a QC retry of the node the
  method table names; after a failed retry: the fallback route when its measured profile is
  better, else `needs_review` with the best take. `*_PARTIAL` warns.
- `should` items rank takes; `nice` items are informational.
- `NOT_MEASURABLE`, `NOT_APPLICABLE` and low-reliability proxies never fail; they warn.
- Editorial items with `approximation_executed: false` are render defects.

Since Phase 11 the QC gate executes the decisions (ADR 0056): a take-level `retry` re-runs the
target nodes through the decision ladder (`ce_qc.ladder`, `ce_exec.qcgate`); a viewer-level
`retry` or `render_defect` left after the takes were accepted flags the version `needs_review`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ce_config.schemas import BehaviorQC
from ce_core.behavior.cbs import RequestedControl
from ce_core.behavior.coverage import EDITORIAL_METHODS, CoverageEntry
from ce_core.behavior.observed import ItemObservation
from ce_core.enums import ObservationVerdict, RealizationMethod

__all__ = ["QADecision", "decide", "retry_target", "take_behavior_score"]

M = RealizationMethod
PRIORITY_WEIGHT = {"must": 3.0, "should": 2.0, "nice": 1.0}
VERDICT_SCORE = {
    ObservationVerdict.CONFIRMED: 1.0,
    ObservationVerdict.PARTIAL: 0.5,
    ObservationVerdict.NOT_OBSERVED: 0.0,
    ObservationVerdict.CONTRADICTED: -0.5,
}


def retry_target(method: str, *, voice_locked: bool, audio: bool = False) -> list[str]:
    """Node kinds to re-run when Performance QA wants a retry (§15.7). `["needs_review"]` when the
    retry would need a locked voice; `[]` when nothing can be regenerated for the behavior. An
    audio-channel item (prosody, delivery) realized by the engine is re-synthesized by the voice."""
    m = M(method)
    if audio and m in (M.NATIVE_PARAMETRIC, M.NATIVE_SEGMENT, M.TEXT_PROMPT_SEGMENT, M.TEXT_PROMPT_GLOBAL):
        return ["needs_review"] if voice_locked else ["tts.segment"]
    if m in (
        M.NATIVE_PARAMETRIC,
        M.NATIVE_SEGMENT,
        M.TEXT_PROMPT_SEGMENT,
        M.TEXT_PROMPT_GLOBAL,
        M.SHOT_SPLIT,
        M.POSE_GUIDED,
    ):
        return ["avatar.render"]
    if m == M.KEYFRAME_CONDITIONING:
        return ["image.keyframe", "avatar.render"]
    if m in (M.PROSODY_TRANSFER, M.AUDIO_NONVERBAL):
        return ["needs_review"] if voice_locked else ["tts.segment", "avatar.render"]
    if m == M.POST_EXPRESSION:
        return ["post.expression"]
    if m in EDITORIAL_METHODS:
        return ["render.final"]  # only when approximation_executed is false (a render defect)
    return []


def take_behavior_score(
    observations: Iterable[ItemObservation], controls: Mapping[tuple[str, str], RequestedControl]
) -> float | None:
    """Priority-weighted verdict score of one take in 0..1 (None when nothing was measurable).
    Low-reliability proxies count at half weight; `nice` items count least."""
    total = weight_sum = 0.0
    for obs in observations:
        control = controls.get((obs.item_ref, obs.dimension))
        verdict = ObservationVerdict(obs.verdict)
        if control is None or verdict not in VERDICT_SCORE:
            continue
        weight = PRIORITY_WEIGHT[str(control.priority)]
        if str(control.observation_reliability) == "low":
            weight *= 0.5
        total += weight * VERDICT_SCORE[verdict]
        weight_sum += weight
    if weight_sum == 0:
        return None
    return round(max(0.0, min(1.0, (total / weight_sum + 0.5) / 1.5)), 4)


@dataclass
class QADecision:
    action: str = "pass"  # pass | warn | retry | render_defect
    retries: list[dict[str, object]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    defects: list[dict[str, str]] = field(default_factory=list)
    executed: bool = True  # executed by the QC gate (take level) or the review flag (viewer level)

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "retries": self.retries,
            "warnings": self.warnings,
            "defects": self.defects,
            "executed": self.executed,
            "ladder": "qc_gate",
        }


def decide(
    entries: Iterable[CoverageEntry],
    controls: Mapping[tuple[str, str], RequestedControl],
    policy: BehaviorQC,
    *,
    voice_locked: bool,
    audio_dimensions: frozenset[str] = frozenset(),
) -> QADecision:
    decision = QADecision()
    for entry in entries:
        control = controls.get((entry.item_ref, entry.dimension))
        if control is None:
            continue
        outcome = str(entry.outcome) if entry.outcome else None
        item = {"item_ref": entry.item_ref, "dimension": entry.dimension}
        if entry.expected_for_method and entry.approximation_executed is False:
            decision.defects.append({**item, "reason": policy.editorial_not_executed})
            continue
        if outcome is None or outcome in policy.never_fail:
            continue
        low = str(control.observation_reliability) == "low"
        if str(control.priority) != "must":
            continue
        failing = outcome in policy.must.retry_on or (
            outcome in policy.must.retry_on_unexpected_editorial and not entry.expected_for_method
        )
        if failing and low:
            decision.warnings.append({**item, "reason": f"{outcome} on a low-reliability proxy (warn only)"})
        elif failing:
            decision.retries.append(
                {
                    **item,
                    "outcome": outcome,
                    "method": str(entry.compiled.method),
                    "rerun": retry_target(
                        str(entry.compiled.method),
                        voice_locked=voice_locked,
                        audio=entry.dimension in audio_dimensions,
                    ),
                    "then": list(policy.must.after_retry),
                }
            )
        elif outcome in policy.must.warn_on:
            decision.warnings.append({**item, "reason": outcome})
    if decision.defects:
        decision.action = "render_defect"
    elif decision.retries:
        decision.action = "retry"
    elif decision.warnings:
        decision.action = "warn"
    return decision
