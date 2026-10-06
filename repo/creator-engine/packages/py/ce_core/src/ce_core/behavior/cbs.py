"""The CanonicalBehaviorSpec (§15.6, ADR 0018): model-independent, fully resolved behavior per scene.

Only `content` is cached (artifact kind `cbs`) and digested; the envelope is assembled per
version for traceability. Engine coverage never appears here — it lives in CompiledBehavior.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, NonNegativeInt, PositiveInt, model_validator

from ce_core.canonical import content_digest
from ce_core.enums import ElementSource, Priority, ReliabilityClass, TemporalPrecision, TimeOfDay, Weather
from ce_core.keys import CameraPositionKey, CharacterKey, EventKey, SceneKey, SegmentKey, StateKey
from ce_core.scalars import Digest, NonEmptyStr, Position3, Token, Unit
from ce_core.spec.acting import EmotionSpec, Strategies, TransitionIn
from ce_core.spec.anchors import DurationSpan, WordRef, WordSpan
from ce_core.spec.base import SpecModel
from ce_core.spec.intent import SceneIntent
from ce_core.spec.paths import SpecPathStr

__all__ = [
    "CBS_VERSION",
    "BehaviorProfile",
    "CBSCastMember",
    "CBSContent",
    "CBSEnvelope",
    "CBSEvent",
    "CBSSituation",
    "CanonicalBehaviorSpec",
    "Constraints",
    "ContinuityRequirement",
    "Habit",
    "Pause",
    "ProsodyDirective",
    "ProvenanceEntry",
    "RequestedControl",
    "TrajectoryState",
    "WorldAffordances",
]

CBS_VERSION: Literal["1.0"] = "1.0"


class CastRefs(SpecModel):
    character_key: CharacterKey
    creator_version_id: UUID
    appearance_version_id: UUID
    voice_version_id: UUID
    memory_snapshot_id: UUID | None = None


class WorldRefs(SpecModel):
    world_version_id: UUID
    camera_position_key: CameraPositionKey
    time_of_day: TimeOfDay
    weather: Weather


class EnvelopeRefs(SpecModel):
    cast: list[CastRefs] = Field(default_factory=list)
    world: WorldRefs | None = None


class Scope(SpecModel):
    video_id: UUID
    version_id: UUID
    scene_key: SceneKey


class CBSEnvelope(SpecModel):
    cbs_version: Literal["1.0"] = CBS_VERSION
    vocab_version: NonEmptyStr
    scope: Scope
    refs: EnvelopeRefs
    content_digest: Digest


class Habit(SpecModel):
    memory_item_id: UUID
    kind: NonEmptyStr
    value: dict[str, Any]
    confidence: Unit


class Baseline(SpecModel):
    energy: Unit
    confidence: Unit
    warmth: Unit
    expressivity: Unit


class BehaviorProfile(SpecModel):
    baseline: Baseline
    emotion_ranges: dict[Token, tuple[Unit, Unit]] = Field(default_factory=dict)
    habits: list[Habit] = Field(default_factory=list)


class CBSCastMember(SpecModel):
    character_key: CharacterKey
    dna_behavior_digest: Digest
    behavior_profile: BehaviorProfile


class WorldAffordances(SpecModel):
    attention_targets: list[str] = Field(default_factory=list)
    seated: bool = False
    allowed_postures: list[Token] = Field(default_factory=list)
    hand_space: Token = "none"


class CBSWorld(SpecModel):
    behavior_digest: Digest
    affordances: WorldAffordances


class CBSStimulus(SpecModel):
    kind: Token
    ref: str


class CBSSituation(SpecModel):
    kind: Token
    audience_stance: Token
    stimulus: CBSStimulus | None = None


class CBSInternalState(SpecModel):
    label: Token


class TrajectoryState(SpecModel):
    """A resolved state: absolute confidence, clamped intensities, strategies for every channel."""

    key: StateKey
    character_key: CharacterKey
    span: WordSpan | DurationSpan
    internal_state: CBSInternalState
    social_goal: Token
    audience_goal: Token
    performance_intent: Token
    emotion: EmotionSpec
    confidence: Unit
    attention_target: str
    strategies: Strategies
    transition_in: TransitionIn | None = None
    priority: Priority


class CBSEvent(SpecModel):
    key: EventKey
    character_key: CharacterKey
    type: Token
    dimension: Token
    at: WordRef | None = None
    span: WordSpan | None = None
    duration_ms: PositiveInt | None = None
    direction: Token | None = None
    target: str | None = None
    intensity: Unit | None = None
    purpose: Token | None = None
    priority: Priority
    source: ElementSource


class Pause(SpecModel):
    after_word: NonNegativeInt
    ms: PositiveInt


class ProsodyDirective(SpecModel):
    character_key: CharacterKey
    segment_key: SegmentKey
    strategy: Token
    rate: float = Field(gt=0.3, lt=3.0)
    energy: Unit
    pitch_variation: Unit
    emphasis_words: list[NonNegativeInt] = Field(default_factory=list)
    pauses: list[Pause] = Field(default_factory=list)
    nonverbal: list[dict[str, Any]] = Field(default_factory=list, description="[{tag, after_word}]")
    delivery: list[dict[str, Any]] = Field(default_factory=list, description="[{tag, start_word, end_word}]")


class ContinuityRequirement(SpecModel):
    """Behavioral continuity only (posture carry, attention targets); visual continuity is QC's job.
    An attention target that is a world element carries its bound state and, when the scene moved
    it, its position (§19.5: a change to a targeted element changes the CBS)."""

    kind: Literal["posture_carry", "attention_target", "affordance"]
    character_key: CharacterKey
    ref: str
    state: Token | None = None
    position: Position3 | None = None


class CBSContinuity(SpecModel):
    requirements: list[ContinuityRequirement] = Field(default_factory=list)


class Constraints(SpecModel):
    locks: list[Token] = Field(default_factory=list)
    avoidances: list[str] = Field(default_factory=list, description="dimension:token or dimension:label>cap")
    out_of_character: list[SpecPathStr] = Field(default_factory=list)
    max_intensity: Unit = 1.0


class RequestedControl(SpecModel):
    """One requested behavior item × channel: the unit coverage and observation evaluate (I4).

    `span` (and `duration_ms` for events) anchor the item in words, so the compiler and the
    judgement can place it without the spec."""

    item_ref: SpecPathStr
    character_key: CharacterKey
    dimension: Token
    value: NonEmptyStr
    temporal_precision: TemporalPrecision
    priority: Priority
    planner_confidence: Unit
    observation_reliability: ReliabilityClass
    span: WordSpan | DurationSpan | None = None
    duration_ms: PositiveInt | None = None


class ProvenanceEntry(SpecModel):
    item_ref: SpecPathStr
    source: Literal[
        "director", "user_tag", "user_edit", "intent_policy", "dna_default", "memory_habit", "compiler_approximation"
    ]
    supported_by: list[str] = Field(default_factory=list)


class CBSContent(SpecModel):
    cast: list[CBSCastMember] = Field(min_length=1)
    world: CBSWorld | None = None
    intent: SceneIntent
    situation: CBSSituation | None = None
    trajectory: list[TrajectoryState] = Field(default_factory=list)
    events: list[CBSEvent] = Field(default_factory=list)
    prosody_directives: list[ProsodyDirective] = Field(default_factory=list)
    continuity: CBSContinuity = Field(default_factory=CBSContinuity)
    constraints: Constraints = Field(default_factory=Constraints)
    requested_controls: list[RequestedControl] = Field(default_factory=list)
    provenance: list[ProvenanceEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _characters_known(self) -> CBSContent:
        cast = {c.character_key for c in self.cast}
        used = (
            {s.character_key for s in self.trajectory}
            | {e.character_key for e in self.events}
            | {p.character_key for p in self.prosody_directives}
            | {r.character_key for r in self.requested_controls}
        )
        unknown = sorted(used - cast)
        if unknown:
            raise ValueError(f"characters not in the CBS cast: {unknown}")
        return self

    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class CanonicalBehaviorSpec(SpecModel):
    envelope: CBSEnvelope
    content: CBSContent

    @model_validator(mode="after")
    def _digest_matches_content(self) -> CanonicalBehaviorSpec:
        if self.envelope.content_digest != self.content.digest():
            raise ValueError("envelope.content_digest does not match the content")
        return self
