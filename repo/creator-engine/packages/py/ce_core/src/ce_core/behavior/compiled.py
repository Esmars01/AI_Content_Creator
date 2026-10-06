"""CompiledBehavior (§15.7): the compiler's claim about execution on one route.

It references CBS items by `item_ref`; engine coverage lives here and is never written back
into the CBS (I1). Translators turn its abstract plans into engine syntax.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from ce_core.enums import CoverageLevel, RealizationMethod
from ce_core.keys import CharacterKey, SegmentKey, ShotKey
from ce_core.scalars import Digest, NonEmptyStr, Token, Unit
from ce_core.spec.anchors import WordRef, WordSpan
from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPathStr

__all__ = [
    "CompiledBehavior",
    "CoverageCounts",
    "EditorialAction",
    "Knobs",
    "ProsodyPlan",
    "Realization",
    "VisualPlan",
    "VisualSubSpan",
]


class Realization(SpecModel):
    item_ref: SpecPathStr
    dimension: Token
    level: CoverageLevel
    method: RealizationMethod
    detail: str = ""


class PlanPause(SpecModel):
    after_word: NonNegativeInt
    ms: PositiveInt


class ProsodyPlan(SpecModel):
    """Abstract, engine-ready prosody for one segment (the voice translator renders it)."""

    character_key: CharacterKey
    segment_key: SegmentKey
    strategy: Token
    emotion: Token | None = None
    emotion_intensity: Unit | None = None
    rate: float = Field(gt=0.3, lt=3.0)
    energy: Unit
    pitch_variation: Unit
    emphasis_words: list[NonNegativeInt] = Field(default_factory=list)
    pauses: list[PlanPause] = Field(default_factory=list)
    nonverbal: list[dict[str, Any]] = Field(default_factory=list)
    delivery: Token | None = None


class Knobs(SpecModel):
    """Abstract knob values; manifests map them to engine parameters after calibration (§15.7)."""

    expressivity: Unit | None = None
    motion_energy: Unit | None = None
    head_motion: Unit | None = None


class VisualSubSpan(SpecModel):
    span: WordSpan
    descriptors: list[str] = Field(default_factory=list, description="vocabulary descriptors from descriptions.yaml")
    knobs: Knobs = Field(default_factory=Knobs)
    item_refs: list[SpecPathStr] = Field(default_factory=list)


class VisualPlan(SpecModel):
    shot_key: ShotKey
    chunk: NonNegativeInt | None = None
    character_key: CharacterKey
    sub_spans: list[VisualSubSpan] = Field(default_factory=list)


class EditorialAction(SpecModel):
    kind: Literal["cutaway", "punch_in", "punch_out", "hold", "shot_split", "caption_emphasis", "music_cue", "sfx_cue"]
    item_ref: SpecPathStr
    at: WordRef | None = None
    span: WordSpan | None = None
    detail: str = ""


class CoverageCounts(SpecModel):
    honored: NonNegativeInt = 0
    approximated: NonNegativeInt = 0
    unsupported: NonNegativeInt = 0


class CompiledBehavior(SpecModel):
    node_kind: Literal["behavior.compile_voice", "behavior.compile_visual", "plan_time"]
    pass_: Literal["plan_time", "build_time"] = Field(alias="pass")
    cbs_content_digest: Digest
    route_digest: Digest | None = None
    target_key: NonEmptyStr = Field(description="segment key, shot key (+chunk) or scene key the plan is for")
    realizations: list[Realization] = Field(default_factory=list)
    prosody_plans: list[ProsodyPlan] = Field(default_factory=list)
    visual_plans: list[VisualPlan] = Field(default_factory=list)
    editorial_actions: list[EditorialAction] = Field(default_factory=list)
    predicted_coverage: CoverageCounts = Field(default_factory=CoverageCounts)
