"""Measured behavior profiles (§16.6): aggregation of behavior observations into
`model_behavior_profiles`, keyed adapter × translator version × model revision × dimension ×
language, each with a `source` (`mock | bench | production`).

Only observations of controls the engine itself executed (native or text-conditioned methods) and
with a measurable verdict count toward a control's success rate; editorial, keyframe and
audio-coupled realizations measure other nodes. The router ignores `mock` profiles unless
`MOCK_GPU=true`, and the compiler plans a declared control as APPROXIMATED when its success rate
is below `qc/behavior.yaml` `unreliable_control_success_rate`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["ENGINE_METHODS", "ObservationRow", "ProfileKey", "aggregate"]

ENGINE_METHODS = frozenset({"native_parametric", "native_segment", "text_prompt_segment", "text_prompt_global"})
MEASURABLE = ("CONFIRMED", "PARTIAL", "NOT_OBSERVED", "CONTRADICTED")


@dataclass(frozen=True)
class ObservationRow:
    adapter_id: str | None
    translator_version: str | None
    model_revision: str | None
    dimension: str
    language: str | None
    method: str
    verdict: str
    level_scope: str = "take"


@dataclass(frozen=True)
class ProfileKey:
    adapter_id: str
    translator_version: str
    revision: str
    dimension: str
    language: str


def aggregate(
    rows: Iterable[ObservationRow], *, audio_dimensions: frozenset[str] = frozenset()
) -> dict[ProfileKey, dict[str, object]]:
    """Per profile key: counts by verdict and method, and the success rate
    (`(CONFIRMED + 0.5·PARTIAL) / measurable`).

    Raw engine output only: take-level rows for visual dimensions; for audio dimensions (the voice
    has no takes) the viewer-level rows, judged on the segment audio at its timeline position."""
    counts: dict[ProfileKey, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for row in rows:
        scope = "viewer" if row.dimension in audio_dimensions else "take"
        if row.level_scope != scope or row.adapter_id is None or row.method not in ENGINE_METHODS:
            continue
        if row.verdict not in MEASURABLE:
            continue
        key = ProfileKey(
            adapter_id=row.adapter_id,
            translator_version=row.translator_version or "none",
            revision=row.model_revision or "none",
            dimension=row.dimension,
            language=(row.language or "und").split("-")[0],
        )
        counts[key][row.method][row.verdict] += 1
    out: dict[ProfileKey, dict[str, object]] = {}
    for key, by_method in counts.items():
        totals = {v: sum(m.get(v, 0) for m in by_method.values()) for v in MEASURABLE}
        n = sum(totals.values())
        success = (totals["CONFIRMED"] + 0.5 * totals["PARTIAL"]) / n if n else 0.0
        out[key] = {
            "n": n,
            "verdicts": totals,
            "by_method": {m: dict(v) for m, v in sorted(by_method.items())},
            "success_rate": round(success, 4),
        }
    return out
