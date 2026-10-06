"""Calibration of low-reliability checks against human ratings (§16.2, §16.8, §19.6; Phase 11).

Human ratings are ground truth for what a proxy claims to see. Each rated item pairs the automatic
verdict (the check said the behavior / element / lighting expectation holds, or not) with the
human answer; per check and analyzer revision the confusion counts give precision, recall and F1,
and `reliability_class` turns them into `high | medium | low` (fewer than `min_n` ratings keep
`low`). The rows are stored like the Phase 7 fixture calibrations (ADR 0045) with
`fixture_set: human_ratings`, so QC uses the measured reliability of the analyzer revision that
produced the tracks (`ce_behavior.calibration.calibrated_vocabulary`).

World checks are calibrated under the pseudo-analyzer `world_qc` (revision = the `qc.world`
implementation version): their measured reliability is reported with every world QC result.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ce_behavior.calibration import ProxyCalibration, reliability_class

__all__ = ["WORLD_QC_ANALYZER", "RatedCheck", "calibrate", "summarize"]

WORLD_QC_ANALYZER = "world_qc"


@dataclass(frozen=True)
class RatedCheck:
    check: str  # the proxy key (look_away, glance_at_element, …) or the world check
    analyzer: str  # capability: face.landmarks, vision.video, world_qc
    adapter_id: str
    revision: str
    automatic: bool  # the check found the behavior / expectation
    human: bool  # the rater saw it


def calibrate(
    rated: Iterable[RatedCheck], *, high: float = 0.85, medium: float = 0.65, min_n: int = 20
) -> list[dict[str, Any]]:
    """Rows for `ce_db.calibration.store_proxy_calibrations`, one per (check, adapter, revision)."""
    counts: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for r in rated:
        cell = counts[(r.check, r.analyzer, r.adapter_id, r.revision)]
        index = (0 if r.human else 1) if r.automatic else (2 if r.human else 3)  # tp, fp, fn, tn
        cell[index] += 1
    rows = []
    for (check, analyzer, adapter_id, revision), (tp, fp, fn, tn) in sorted(counts.items()):
        cal = ProxyCalibration(
            proxy=check,
            analyzer=analyzer,
            adapter_id=adapter_id,
            revision=revision,
            tp=tp,
            fp=fp,
            fn=fn,
            tn=tn,
            reliability="low",
            method="human_ratings",
            fixture_set="human_ratings",
        )
        cal = ProxyCalibration(
            **{**cal.__dict__, "reliability": reliability_class(cal.f1, cal.n, high=high, medium=medium, min_n=min_n)}
        )
        rows.append({"adapter_id": adapter_id, "revision": revision, "proxy": check, "measured": cal.measured()})
    return rows


def summarize(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "check": r["proxy"],
            "adapter_id": r["adapter_id"],
            "revision": r["revision"],
            **{k: dict(r["measured"]).get(k) for k in ("n", "precision", "recall", "f1", "reliability", "analyzer")},
        }
        for r in rows
    ]
