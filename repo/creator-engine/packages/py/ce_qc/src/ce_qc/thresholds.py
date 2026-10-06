"""Per-node metric checks with thresholds keyed by metric adapter id (§26).

Metric scales differ between models (a SyncNet confidence, a DNSMOS MOS, a 0..1 quality score), so
`config/qc/{tier}.yaml` keys every threshold by the adapter that measured it
(`checks.lipsync.by_adapter.syncnet_v1.min_confidence`). A metric whose adapter has no thresholds
is reported but never gates (`basis: "no_thresholds"`), and an advisory check (lip sync until its
weights license is verified; any check in a `beta` language) only warns.

Threshold keys: `min_score`, `max_score`, `min_confidence` (a floor on the score) and
`good_confidence` (the band above which the result is rated `good`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ce_config.schemas import TierQC
from ce_contracts.models import QCMetricResult

__all__ = ["CHECK_OF", "MetricVerdict", "judge_metric", "metric_score"]

# capability → check section in the tier config
CHECK_OF = {"qc.lipsync": "lipsync", "qc.speech_quality": "speech", "qc.vqa": "visual_quality"}


@dataclass(frozen=True)
class MetricVerdict:
    capability: str
    adapter_id: str
    metric: str
    score: float
    passed: bool | None  # None: not judged (no thresholds for this adapter)
    advisory: bool
    basis: str  # thresholds | no_thresholds
    thresholds: dict[str, float] = field(default_factory=dict)
    rating: str | None = None  # good | acceptable | reject
    reason: str | None = None

    @property
    def gating_failure(self) -> bool:
        return self.passed is False and not self.advisory

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "adapter_id": self.adapter_id,
            "metric": self.metric,
            "score": self.score,
            "passed": self.passed,
            "advisory": self.advisory,
            "basis": self.basis,
            "thresholds": dict(self.thresholds),
            "rating": self.rating,
            "reason": self.reason,
        }


def _section(tier: TierQC, check: str) -> tuple[bool, dict[str, dict[str, float]]]:
    section = getattr(tier.checks, check, None)
    if section is None:
        return False, {}
    return bool(getattr(section, "advisory", False)), dict(getattr(section, "by_adapter", {}))


def judge_metric(
    tier: TierQC,
    capability: str,
    adapter_id: str,
    result: QCMetricResult,
    *,
    beta_language: bool = False,
) -> MetricVerdict:
    """The verdict of one metric result against the tier's thresholds for the adapter that made it."""
    check = CHECK_OF.get(capability)
    advisory, by_adapter = _section(tier, check) if check else (False, {})
    advisory = advisory or bool(result.advisory)
    reason = None
    if beta_language and capability == "qc.lipsync":
        advisory, reason = True, "lip sync only warns in beta languages (§26)"
    thresholds = dict(by_adapter.get(adapter_id, {}))
    score = float(result.score)
    if not thresholds:
        return MetricVerdict(
            capability,
            adapter_id,
            result.metric,
            score,
            None,
            True,
            "no_thresholds",
            reason=f"no thresholds for adapter {adapter_id!r} in this tier: reported, never gated",
        )
    floor = thresholds.get("min_score", thresholds.get("min_confidence"))
    ceiling = thresholds.get("max_score")
    passed = (floor is None or score >= floor) and (ceiling is None or score <= ceiling)
    good = thresholds.get("good_confidence")
    rating = "reject" if not passed else ("good" if good is None or score >= good else "acceptable")
    if not passed and reason is None:
        bound = f">= {floor}" if floor is not None and score < floor else f"<= {ceiling}"
        reason = f"{result.metric} {score:g} is not {bound} ({adapter_id})"
    return MetricVerdict(
        capability, adapter_id, result.metric, score, passed, advisory, "thresholds", thresholds, rating, reason
    )


def metric_score(verdicts: list[MetricVerdict]) -> float:
    """Take-ranking metric score in 0..1: judged metrics count pass = 1 / fail = 0, advisory ones at
    half weight; unjudged metrics do not count (D44)."""
    marks = [(0.0 if v.passed is False else 1.0, 0.5 if v.advisory else 1.0) for v in verdicts if v.passed is not None]
    weight = sum(w for _, w in marks)
    return round(sum(v * w for v, w in marks) / weight, 4) if weight else 1.0
