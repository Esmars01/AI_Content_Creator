"""The situational acting plan (§15.3): situation, a trajectory of states, and keyed events."""

from __future__ import annotations

from pydantic import Field, NonNegativeInt, PositiveInt, model_validator

from ce_core.enums import ElementSource, Priority
from ce_core.keys import CharacterKey, EventKey, StateKey
from ce_core.scalars import NonEmptyStr, SignedUnit, Token, Unit
from ce_core.spec.anchors import DurationSpan, WordRef, WordSpan
from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPathStr

__all__ = [
    "ActingPlan",
    "ActingState",
    "BehaviorEvent",
    "EmotionSpec",
    "EmotionValue",
    "InternalState",
    "OutOfCharacter",
    "Situation",
    "Stimulus",
    "Strategies",
    "TransitionIn",
    "Trigger",
]

STATE_SOURCES = frozenset(
    {ElementSource.DIRECTOR, ElementSource.USER_TAG, ElementSource.USER_EDIT, ElementSource.INTENT_POLICY}
)
EVENT_SOURCES = frozenset(
    {ElementSource.DIRECTOR, ElementSource.USER_TAG, ElementSource.USER_EDIT, ElementSource.COMPILER_APPROXIMATION}
)


class Stimulus(SpecModel):
    kind: Token = Field(description="stimulus_kind: element | clip | statement | memory")
    ref: str = Field(description="element key, asset id, quoted statement or memory item id")


class Situation(SpecModel):
    kind: Token
    description: str = ""
    audience_stance: Token
    stimulus: Stimulus | None = None


class InternalState(SpecModel):
    label: Token
    description: str = ""


class EmotionValue(SpecModel):
    label: Token
    intensity: Unit


class EmotionSpec(SpecModel):
    """`masking: true` = felt differs from displayed ("pretending to stay calm while surprised")."""

    felt: EmotionValue
    displayed: EmotionValue
    masking: bool = False

    @model_validator(mode="after")
    def _masking_needs_difference(self) -> EmotionSpec:
        if self.masking and self.felt == self.displayed:
            raise ValueError("masking requires felt ≠ displayed (§15.3)")
        return self


class Strategies(SpecModel):
    prosody: Token
    gaze: Token
    gesture: Token
    posture: Token
    reaction: Token
    camera_awareness: Token


class Trigger(SpecModel):
    kind: Token
    at: WordRef
    description: str = ""


class TransitionIn(SpecModel):
    trigger: Trigger | None = None
    style: Token = Field(description="transition_style: sudden | gradual | lagged")
    duration_words: NonNegativeInt = 0


class OutOfCharacter(SpecModel):
    allowed: bool
    reason: NonEmptyStr


class ActingState(SpecModel):
    """One span of a character's emotional trajectory. `carry: true` inherits the previous state's values."""

    key: StateKey
    character_key: CharacterKey
    source: ElementSource
    span: WordSpan | DurationSpan
    carry: bool = False
    internal_state: InternalState | None = None
    social_goal: Token | None = None
    audience_goal: Token | None = None
    performance_intent: Token | None = None
    emotion: EmotionSpec | None = None
    confidence_delta: SignedUnit = 0.0
    attention_target: str | None = Field(default=None, description="camera | off_camera_person | notes | self | el_…")
    strategies: Strategies | None = None
    transition_in: TransitionIn | None = None
    priority: Priority = Priority.SHOULD
    out_of_character: OutOfCharacter | None = None

    @model_validator(mode="after")
    def _complete_unless_carried(self) -> ActingState:
        if self.source not in STATE_SOURCES:
            raise ValueError(f"state source must be one of {sorted(STATE_SOURCES)}")
        if not self.carry:
            missing = [
                name
                for name in ("internal_state", "social_goal", "audience_goal", "performance_intent", "emotion")
                if getattr(self, name) is None
            ]
            missing += [name for name in ("attention_target", "strategies") if getattr(self, name) is None]
            if missing:
                raise ValueError(f"state {self.key}: {', '.join(missing)} required unless carry is true")
        return self


class BehaviorEvent(SpecModel):
    """A discrete behavior at a word (`at`) or over a span. The type's vocabulary entry gives its dimension."""

    key: EventKey
    character_key: CharacterKey
    type: Token
    at: WordRef | None = None
    span: WordSpan | None = None
    duration_ms: PositiveInt | None = None
    direction: Token | None = None
    target: str | None = Field(default=None, description="a world element key or attention target")
    intensity: Unit | None = None
    purpose: Token | None = None
    trigger_ref: SpecPathStr | None = None
    priority: Priority = Priority.SHOULD
    source: ElementSource = ElementSource.DIRECTOR

    @model_validator(mode="after")
    def _anchored_once(self) -> BehaviorEvent:
        if (self.at is None) == (self.span is None):
            raise ValueError(f"event {self.key}: exactly one of `at` or `span` is required")
        if self.source not in EVENT_SOURCES:
            raise ValueError(f"event source must be one of {sorted(EVENT_SOURCES)}")
        return self


class ActingPlan(SpecModel):
    situation: Situation
    states: list[ActingState] = Field(default_factory=list)
    events: list[BehaviorEvent] = Field(default_factory=list)
