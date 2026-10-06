"""Structured outputs of the LLM stages (§13) and the Director's request and result.

Stage outputs are deliberately compact: the model makes the creative choices (brief, hooks,
wording, scene intent, the acting chain) and code builds everything structural around them
(keys, spans, tiling, shots, world bindings, policies, validation). Every control field is
checked against the closed vocabularies after parsing (I13, `ce_director.vocabmap`); free text
lives only in `description`/`notes`-like fields.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ActingOut",
    "AnnotationOut",
    "AssertionOut",
    "Beat",
    "BriefOut",
    "CastHints",
    "CastRequest",
    "CharSpan",
    "ClaimOut",
    "EmotionOut",
    "EventOut",
    "FactCheckOut",
    "OverlayOut",
    "PlanRequest",
    "SceneActingOut",
    "SceneIntentOut",
    "SceneOut",
    "ScenesOut",
    "ScriptLine",
    "ScriptOut",
    "SettingHints",
    "SituationOut",
    "StateOut",
    "StimulusOut",
    "StrategiesOut",
    "StrategyOut",
    "TimedRequest",
    "TransitionOut",
    "VideoIntentOut",
    "WordAt",
]


class _Out(BaseModel):
    """Stage output: unknown keys are rejected, so an injected field never slips through (I10)."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------- stage 1: interpret


class CharSpan(_Out):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _forward(self) -> CharSpan:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class CastHints(_Out):
    description: str = ""
    age: int | None = Field(default=None, ge=18, le=99)
    gender: Literal["female", "male", "unspecified"] = "unspecified"


class SettingHints(_Out):
    world_kind: str | None = None  # world_kind
    posture: str | None = None  # strategy.posture
    camera_profile: str | None = None  # a camera profile id
    device: str = ""  # free text, e.g. "iPhone"


class TimedRequest(_Out):
    """A seconds-based acting request (§15.4): `label` is an emotion."""

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    label: str
    note: str = ""


class BriefOut(_Out):
    input_mode: Literal["idea", "structured", "exact_script", "brain_dump"]
    title: str = Field(min_length=1, max_length=120)
    mode: str
    language: str = "en-US"
    target_duration_s: float | None = Field(default=None, gt=0, le=600)
    platform_targets: list[str] = Field(default_factory=list)
    audience: str = ""
    angle: str = ""
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    sources_policy: Literal["open", "closed_book"] = "open"
    script_span: CharSpan | None = None
    cast: CastHints = Field(default_factory=CastHints)
    setting: SettingHints = Field(default_factory=SettingHints)
    acting_brief: str = ""
    time_requests: list[TimedRequest] = Field(default_factory=list)


# ---------------------------------------------------------------------- stage 4: strategy


class Beat(_Out):
    purpose: str  # intent.scene_purpose
    summary: str = ""


class VideoIntentOut(_Out):
    narrative_goal: str | None = None
    audience_effect: str | None = None
    persuasion_goal: str | None = None
    information_goal: str | None = None
    emotional_arc: list[str] = Field(default_factory=list)
    attention_goal: str | None = None
    cta_goal: str | None = None


class StrategyOut(_Out):
    strategy_pack: str
    audience: str = ""
    angle: str = ""
    hooks: list[str] = Field(min_length=1, max_length=5)
    selected_hook: int = Field(default=0, ge=0)
    beats: list[Beat] = Field(min_length=1)
    video_intent: VideoIntentOut = Field(default_factory=VideoIntentOut)


# ---------------------------------------------------------------------- stage 5: script


class ScriptLine(_Out):
    beat: int = Field(ge=0)
    text: str = Field(min_length=1)


class AnnotationOut(_Out):
    segment_key: str
    type: str  # annotation type
    tag: str  # annotation_tag.<type>
    word: int = Field(ge=0)
    end_word: int | None = Field(default=None, ge=0)


class ScriptOut(_Out):
    """AI mode: `lines` (canonical acting tags allowed inline). Exact mode: `boundaries` (absolute
    character offsets in the raw input where segments 2..n start), the beat of each segment and
    optional annotations; code slices and verifies the text, the model never retypes it."""

    lines: list[ScriptLine] = Field(default_factory=list)
    boundaries: list[int] = Field(default_factory=list)
    segment_beats: list[int] = Field(default_factory=list)
    annotations: list[AnnotationOut] = Field(default_factory=list)


# ---------------------------------------------------------------------- stage 6: fact and persona check


class ClaimOut(_Out):
    segment_key: str
    text: str = Field(min_length=1)
    verdict: Literal["supported", "unsupported", "uncertain", "conflicting"]
    evidence_ids: list[str] = Field(default_factory=list)


class AssertionOut(_Out):
    segment_key: str
    subject: str
    predicate: str
    object: str
    kind: Literal["fact", "stance"] = "fact"


class FactCheckOut(_Out):
    claims: list[ClaimOut] = Field(default_factory=list)
    assertions: list[AssertionOut] = Field(default_factory=list)


# ---------------------------------------------------------------------- stage 7: scenes


class WordAt(_Out):
    segment_key: str
    word: int = Field(ge=0)


class SceneIntentOut(_Out):
    narrative_goal: str | None = None
    emotional_goal: str | None = None
    audience_effect: str | None = None
    persuasion_goal: str | None = None
    information_goal: str | None = None
    attention_goal: str | None = None
    reveal_strategy: str | None = None
    tension_level: float | None = Field(default=None, ge=0, le=1)
    curiosity_level: float | None = Field(default=None, ge=0, le=1)
    performance_strategy: str | None = None
    notes: str = ""


class OverlayOut(_Out):
    kind: Literal["broll", "title_card"]
    start: WordAt
    end: WordAt
    prompt: str = ""
    title: str = ""


class SceneOut(_Out):
    segment_keys: list[str] = Field(min_length=1)
    purpose: str  # intent.scene_purpose
    intent: SceneIntentOut = Field(default_factory=SceneIntentOut)
    reveal_at: WordAt | None = None
    camera_position: str | None = None
    framing: str | None = None  # camera.framing
    overlays: list[OverlayOut] = Field(default_factory=list)


class ScenesOut(_Out):
    scenes: list[SceneOut] = Field(min_length=1)


# ---------------------------------------------------------------------- stage 8: acting


class StimulusOut(_Out):
    kind: str  # stimulus_kind
    ref: str


class SituationOut(_Out):
    kind: str  # situation_kind
    description: str = ""
    audience_stance: str  # audience_stance
    stimulus: StimulusOut | None = None


class EmotionOut(_Out):
    label: str  # emotion
    intensity: float = Field(ge=0, le=1)


class StrategiesOut(_Out):
    prosody: str
    gaze: str
    gesture: str
    posture: str
    reaction: str
    camera_awareness: str


class TransitionOut(_Out):
    style: str  # transition_style
    trigger_kind: str | None = None  # trigger_kind
    trigger_at: WordAt | None = None
    description: str = ""
    duration_words: int = Field(default=1, ge=0, le=20)


class StateOut(_Out):
    """`start` (a word) or `start_s` (a requested second, mapped to the nearest word, §15.4)."""

    start: WordAt | None = None
    start_s: float | None = Field(default=None, ge=0)
    internal_state: str
    internal_description: str = ""
    social_goal: str
    audience_goal: str
    performance_intent: str
    felt: EmotionOut
    displayed: EmotionOut
    masking: bool = False
    confidence_delta: float = Field(default=0.0, ge=-1, le=1)
    attention_target: str = "camera"
    strategies: StrategiesOut
    transition: TransitionOut | None = None
    priority: Literal["must", "should", "nice"] = "should"

    @model_validator(mode="after")
    def _one_start(self) -> StateOut:
        if (self.start is None) == (self.start_s is None):
            raise ValueError("give exactly one of `start` or `start_s`")
        return self


class EventOut(_Out):
    type: str  # event_type
    at: WordAt
    duration_ms: int | None = Field(default=None, gt=0, le=10_000)
    direction: str | None = None  # direction
    target: str | None = None
    intensity: float | None = Field(default=None, ge=0, le=1)
    purpose: str | None = None  # event_purpose
    priority: Literal["must", "should", "nice"] = "should"
    reacts_to_state: int | None = Field(default=None, ge=0, description="index of the state whose trigger it follows")


class SceneActingOut(_Out):
    scene_key: str
    situation: SituationOut
    states: list[StateOut] = Field(min_length=1)
    events: list[EventOut] = Field(default_factory=list)
    annotations: list[AnnotationOut] = Field(default_factory=list)
    deviations: list[str] = Field(default_factory=list, description="intent-policy proposals not followed, with why")


class ActingOut(_Out):
    scenes: list[SceneActingOut] = Field(min_length=1)


# ---------------------------------------------------------------------- the request


class CastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creator_id: UUID
    role: Literal["host", "guest", "narrator"] = "host"
    voice_version_id: UUID | None = None
    wardrobe_version_id: UUID | None = None


class PlanRequest(BaseModel):
    """`POST /v1/projects/{id}/videos` as Director constraints (§30)."""

    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=50_000)
    input_mode: Literal["auto", "idea", "structured", "exact_script", "brain_dump"] = "auto"
    cast: list[CastRequest] = Field(default_factory=list, max_length=1)
    world_id: UUID | None = None
    mode: str | None = None
    platform_targets: list[str] = Field(default_factory=list)
    primary_aspect: Literal["9:16", "16:9", "1:1", "4:5"] | None = None
    target_duration_s: float | None = Field(default=None, gt=0, le=600)
    language: str | None = None
    quality_tier: Literal["draft", "final"] = "draft"
    camera_profile_id: str | None = None
    caption_style_id: str | None = None
    music_mood: str | None = None
    sources_policy: Literal["open", "closed_book"] | None = None
    sources: list[UUID] = Field(
        default_factory=list, max_length=50, description="persistent research sources (Phase 12)"
    )
    strategy_pack: str | None = None
    takes: int = Field(default=1, ge=1, le=4)
    routing_profile: str | None = None
    intent_hints: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=10)
    acting_hints: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=10)
    instruction: str | None = Field(default=None, max_length=4000, description="replan: what to change")
