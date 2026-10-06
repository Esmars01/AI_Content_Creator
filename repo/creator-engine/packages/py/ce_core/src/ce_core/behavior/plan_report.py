"""PlanReport (§13 stage 11, previz): repetition, contradiction, timing drift and predicted coverage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import Field, NonNegativeFloat, PositiveInt

from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_core.enums import InputMode
from ce_core.keys import EventKey, SceneKey, StateKey
from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPathStr

if TYPE_CHECKING:
    from ce_core.spec.videospec import VideoSpec

__all__ = ["EventTiming", "Finding", "PlanReport", "StateTiming", "event_timings"]


class Finding(SpecModel):
    kind: Literal["repetition", "contradiction", "fact_check", "policy", "timing", "world", "other"]
    severity: Literal["info", "warning", "blocking"] = "warning"
    message: str
    refs: list[SpecPathStr] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class StateTiming(SpecModel):
    state_key: StateKey
    start_s: NonNegativeFloat
    end_s: NonNegativeFloat
    requested_start_s: NonNegativeFloat | None = None
    drift_s: float | None = None


class EventTiming(SpecModel):
    """Where a behavior event lands on the timeline (estimated at planning, measured after previz);
    the Performance Timeline places its markers here."""

    scene_key: SceneKey
    event_key: EventKey
    type: str
    at_s: NonNegativeFloat
    end_s: NonNegativeFloat | None = None
    duration_ms: PositiveInt | None = None


class PlanReport(SpecModel):
    version_id: UUID
    input_mode: InputMode
    planner: Literal["llm", "template"] = "llm"
    estimated_duration_s: NonNegativeFloat
    target_duration_s: NonNegativeFloat
    timing_source: Literal["estimated", "measured"] = "estimated"
    state_timings: list[StateTiming] = Field(default_factory=list)
    event_timings: list[EventTiming] = Field(default_factory=list)
    predicted_coverage: BehaviorCoverageReport | None = None
    memory_items_used: list[UUID] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    cost_estimate_usd: NonNegativeFloat | None = None


def event_timings(spec: VideoSpec, words: Mapping[str, Sequence[tuple[float, float]]]) -> list[EventTiming]:
    """Event markers from word times (`words[segment_key][i] = (start_s, end_s)`): an event sits at
    its `at` word (or its span's first word) and ends at its span's last word or after
    `duration_ms`. Events whose words have no time are left out."""

    def at(segment_key: str, index: int, edge: int) -> float | None:
        times = words.get(segment_key)
        return float(times[index][edge]) if times is not None and 0 <= index < len(times) else None

    out: list[EventTiming] = []
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        if scene.acting is None:
            continue
        for event in scene.acting.events:
            ref = event.at or (event.span.start if event.span is not None else None)
            if ref is None:
                continue
            start = at(ref.segment_key, ref.word, 0)
            if start is None:
                continue
            end: float | None = None
            if event.span is not None:
                end = at(event.span.end.segment_key, event.span.end.word, 1)
            elif event.duration_ms:
                end = start + event.duration_ms / 1000.0
            out.append(
                EventTiming(
                    scene_key=scene.key,
                    event_key=event.key,
                    type=str(event.type),
                    at_s=round(start, 3),
                    end_s=round(end, 3) if end is not None else None,
                    duration_ms=event.duration_ms,
                )
            )
    return sorted(out, key=lambda e: (e.at_s, e.event_key))
