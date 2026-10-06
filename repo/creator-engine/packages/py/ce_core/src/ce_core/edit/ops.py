"""`EditOperation`: the closed union of §28 and the **only** way to mutate a spec (ADR 0025).

Every operation names its target scope with keys (scenes, shots, characters, states, events,
segments) or a word span. Operations proposed by the Director may carry a reference slot
(`scope.ref = "@selection"`, `at_ref = "@emphasis"`), which the reference resolver replaces with
concrete keys and anchors before an operation is validated or stored (§28 step 1); a stored
proposal only contains resolved operations.

Values are closed-vocabulary tokens (I13); `reason` is free text shown to the user and never
drives control. Code translates operations into a `SpecPatch` (`ce_core.edit.translate`).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, NonNegativeInt, PositiveInt, TypeAdapter, model_validator

from ce_core.enums import CastRole, Priority, QualityTier
from ce_core.keys import (
    AnnotationKey,
    CameraMoveKey,
    CameraPositionKey,
    CharacterKey,
    EffectKey,
    ElementKey,
    EventKey,
    MusicCueKey,
    ProductKey,
    SceneKey,
    SegmentKey,
    SfxKey,
    ShotKey,
    StateKey,
    TakeKey,
)
from ce_core.scalars import Aspect, LanguageTag, NonEmptyStr, SignedUnit, Token, Unit
from ce_core.spec.anchors import WordRef, WordSpan
from ce_core.spec.base import SpecModel
from ce_core.spec.common import Effect
from ce_core.spec.videospec import (
    BrollSpec,
    CaptionTranslation,
    Lock,
    LockScope,
    MusicCue,
    OutputPreset,
    ProductRef,
    ReactionSource,
    Reframe,
    Scene,
    Segment,
    SfxEvent,
    Shot,
    SpeedSegment,
    VoiceProsody,
    WebcamBubble,
    Zoom,
)
from ce_core.spec.world import AcousticsOverride, AddedElement, ContinuityRef, LightingOverride, MovedElement

__all__ = [
    "EDIT_OPERATION_TYPES",
    "OPERATIONS",
    "ActingChanges",
    "EditOperation",
    "EditOperations",
    "EditScope",
    "EmotionChange",
    "EmotionValueChange",
    "operation_list",
    "parse_operations",
]

SlotRef = Annotated[str, Field(pattern=r"^@[a-z0-9_]+$", max_length=40)]
STRATEGY_CHANNELS = ("prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness")


class _Op(SpecModel):
    reason: str = Field(default="", max_length=500, description="shown in the proposal; never drives control")


class EditScope(SpecModel):
    """What an operation applies to. Empty lists mean "none"; `None` means "not restricted"."""

    ref: SlotRef | None = Field(default=None, description="a reference slot resolved before validation")
    scene_keys: list[SceneKey] | None = None
    shot_keys: list[ShotKey] | None = None
    character_keys: list[CharacterKey] | None = None
    state_keys: list[StateKey] | None = None
    span: WordSpan | None = Field(default=None, description="a time range resolved to word anchors")

    @property
    def resolved(self) -> bool:
        return self.ref is None


# ---------------------------------------------------------------------- intent and acting


class SetIntent(_Op):
    """Video intent (no scope) or scene intent fields; triggers an intent-policy replan (§14)."""

    op: Literal["set_intent"] = "set_intent"
    scope: EditScope | None = Field(default=None, description="scenes; none = the video intent")
    fields: dict[Token, Token | float | list[Token] | None] = Field(min_length=1)


class EmotionValueChange(SpecModel):
    label: Token | None = None
    intensity: Unit | None = None
    intensity_delta: SignedUnit | None = Field(default=None, description="relative change, clamped to 0..1")

    @model_validator(mode="after")
    def _one_intensity(self) -> EmotionValueChange:
        if self.intensity is not None and self.intensity_delta is not None:
            raise ValueError("give intensity or intensity_delta, not both")
        return self


class EmotionChange(SpecModel):
    felt: EmotionValueChange | None = None
    displayed: EmotionValueChange | None = None
    masking: bool | None = None


class TransitionChange(SpecModel):
    style: Token | None = None
    trigger_kind: Token | None = None
    trigger_at: WordRef | None = None
    duration_words: NonNegativeInt | None = None


class SituationChange(SpecModel):
    kind: Token | None = None
    audience_stance: Token | None = None
    description: str | None = None


class ActingChanges(SpecModel):
    internal_state: Token | None = None
    social_goal: Token | None = None
    audience_goal: Token | None = None
    performance_intent: Token | None = None
    emotion: EmotionChange | None = None
    confidence_delta: SignedUnit | None = None
    attention_target: str | None = Field(default=None, max_length=64)
    strategies: dict[Literal["prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness"], Token] = Field(
        default_factory=dict
    )
    transition_in: TransitionChange | None = None
    priority: Priority | None = None

    @property
    def is_empty(self) -> bool:
        return self == ActingChanges()


class SetActing(_Op):
    """The acting states in scope (split at the span's edges when it cuts a state)."""

    op: Literal["set_acting"] = "set_acting"
    scope: EditScope = Field(default_factory=EditScope)
    situation: SituationChange | None = None
    changes: ActingChanges = Field(default_factory=ActingChanges)


class NewEvent(SpecModel):
    key: EventKey | None = Field(default=None, description="assigned when missing")
    character_key: CharacterKey | None = Field(default=None, description="default: the scope's character")
    type: Token
    at: WordRef | None = None
    at_ref: SlotRef | None = Field(default=None, description="a named anchor such as @emphasis or @span_start")
    span: WordSpan | None = None
    duration_ms: PositiveInt | None = None
    direction: Token | None = None
    target: str | None = Field(default=None, max_length=64)
    intensity: Unit | None = None
    purpose: Token | None = None
    priority: Priority = Priority.SHOULD


class AddBehaviorEvent(_Op):
    op: Literal["add_behavior_event"] = "add_behavior_event"
    scope: EditScope = Field(default_factory=EditScope)
    event: NewEvent


class RemoveBehaviorEvent(_Op):
    op: Literal["remove_behavior_event"] = "remove_behavior_event"
    scene_key: SceneKey
    event_key: EventKey


class EventChanges(SpecModel):
    type: Token | None = None
    at: WordRef | None = None
    span: WordSpan | None = None
    duration_ms: PositiveInt | None = None
    direction: Token | None = None
    target: str | None = Field(default=None, max_length=64)
    intensity: Unit | None = None
    intensity_delta: SignedUnit | None = None
    purpose: Token | None = None
    priority: Priority | None = None


class SetBehaviorEvent(_Op):
    op: Literal["set_behavior_event"] = "set_behavior_event"
    scene_key: SceneKey | None = None
    scope: EditScope | None = Field(default=None, description="events of a type in scope (with `event_type`)")
    event_key: EventKey | None = None
    event_type: Token | None = Field(default=None, description="every event of this type in scope")
    changes: EventChanges

    @model_validator(mode="after")
    def _target(self) -> SetBehaviorEvent:
        if (self.event_key is None) == (self.event_type is None):
            raise ValueError("set_behavior_event needs exactly one of event_key or event_type")
        if self.event_key is not None and self.scene_key is None:
            raise ValueError("set_behavior_event with event_key needs scene_key")
        return self


class NewAnnotation(SpecModel):
    key: AnnotationKey | None = None
    type: Token
    tag: Token
    span: WordSpan | None = None
    at_ref: SlotRef | None = None
    pause_ms: PositiveInt | None = None
    respelling: str | None = None


class AddAnnotation(_Op):
    op: Literal["add_annotation"] = "add_annotation"
    segment_key: SegmentKey | None = Field(default=None, description="default: the span's segment")
    scope: EditScope | None = None
    annotation: NewAnnotation


class RemoveAnnotation(_Op):
    op: Literal["remove_annotation"] = "remove_annotation"
    segment_key: SegmentKey
    annotation_key: AnnotationKey


class AnnotationChanges(SpecModel):
    tag: Token | None = None
    span: WordSpan | None = None
    pause_ms: PositiveInt | None = None
    respelling: str | None = None


class SetAnnotation(_Op):
    op: Literal["set_annotation"] = "set_annotation"
    segment_key: SegmentKey
    annotation_key: AnnotationKey
    changes: AnnotationChanges


# ---------------------------------------------------------------------- world, cast, camera


class SetWorldBinding(_Op):
    op: Literal["set_world_binding"] = "set_world_binding"
    scope: EditScope = Field(default_factory=EditScope)
    world_version_id: UUID | None = None
    world_query: str | None = Field(
        default=None, max_length=200, description="a world description the Director resolves to an approved world"
    )
    camera_position_key: CameraPositionKey | None = None
    time_of_day: Token | None = None
    weather: Token | None = None
    continuity_ref: ContinuityRef | None = None


class SetWorldOverride(_Op):
    """Scene-local overrides; never mutates World DNA (I6)."""

    op: Literal["set_world_override"] = "set_world_override"
    scope: EditScope = Field(default_factory=EditScope)
    element_states: dict[ElementKey, Token | None] = Field(default_factory=dict, description="null clears")
    hide: list[ElementKey] = Field(default_factory=list)
    unhide: list[ElementKey] = Field(default_factory=list)
    add_elements: list[AddedElement] = Field(default_factory=list)
    remove_added: list[str] = Field(default_factory=list)
    move_elements: list[MovedElement] = Field(default_factory=list)
    lighting: LightingOverride | None = None
    clear_lighting: bool = False
    acoustics: AcousticsOverride | None = None
    clear_acoustics: bool = False


class SetWardrobe(_Op):
    op: Literal["set_wardrobe"] = "set_wardrobe"
    scope: EditScope = Field(default_factory=EditScope)
    character_key: CharacterKey | None = None
    wardrobe_version_id: UUID | None = None
    wardrobe_query: str | None = Field(default=None, max_length=200, description="resolved to an approved wardrobe")


class SetCast(_Op):
    op: Literal["set_cast"] = "set_cast"
    character_key: CharacterKey | None = Field(default=None, description="default: the only cast member")
    appearance_version_id: UUID | None = None
    clear_appearance: bool = False
    voice_version_id: UUID | None = None
    voice_query: str | None = Field(default=None, max_length=200, description="resolved to an approved voice")
    clear_voice: bool = False
    voice_prosody: VoiceProsody | None = None
    clear_voice_prosody: bool = False
    role: CastRole | None = None


class NewMove(SpecModel):
    key: CameraMoveKey | None = None
    type: Token
    at: WordRef | None = Field(default=None, description="default: the shot's first word")
    at_ref: SlotRef | None = None
    scale: float | None = Field(default=None, gt=0.5, lt=3.0)
    transition: Token = "cut"


class SetCamera(_Op):
    op: Literal["set_camera"] = "set_camera"
    scope: EditScope = Field(default_factory=EditScope, description="shots; default every talking shot")
    profile_id: Token | None = None
    framing: Token | None = None
    angle: Token | None = None
    add_moves: list[NewMove] = Field(default_factory=list)
    remove_moves: list[CameraMoveKey] = Field(default_factory=list)
    remove_move_types: list[Token] = Field(default_factory=list)
    camera_position_key: CameraPositionKey | None = None


class SetPacing(_Op):
    op: Literal["set_pacing"] = "set_pacing"
    scope: EditScope = Field(default_factory=EditScope)
    target_wpm_delta: float | None = Field(default=None, ge=-0.5, le=0.5)
    cut_cadence: Literal["slow", "medium", "fast"] | None = None
    clear_cut_cadence: bool = False
    pause_scale: float | None = Field(default=None, ge=0.25, le=4.0, description="scales every pause in scope")


class EditScript(_Op):
    """Replaces a segment's text; anchors are rebased (refused while wording is locked)."""

    op: Literal["edit_script"] = "edit_script"
    segment_key: SegmentKey
    text: NonEmptyStr


# ---------------------------------------------------------------------- structure


class AddScene(_Op):
    op: Literal["add_scene"] = "add_scene"
    scene: Scene
    segments: list[Segment] = Field(default_factory=list, description="new segments the scene speaks")
    after_scene_key: SceneKey | None = Field(default=None, description="none = first")


class RemoveScene(_Op):
    op: Literal["remove_scene"] = "remove_scene"
    scene_key: SceneKey


class MoveScene(_Op):
    op: Literal["move_scene"] = "move_scene"
    scene_key: SceneKey
    after_scene_key: SceneKey | None = None


class AddShot(_Op):
    op: Literal["add_shot"] = "add_shot"
    scene_key: SceneKey
    shot: Shot


class RemoveShot(_Op):
    op: Literal["remove_shot"] = "remove_shot"
    scene_key: SceneKey | None = None
    shot_key: ShotKey


class SplitShot(_Op):
    op: Literal["split_shot"] = "split_shot"
    scene_key: SceneKey | None = None
    shot_key: ShotKey
    at: WordRef
    new_shot_key: ShotKey | None = None


class ShotChanges(SpecModel):
    type: Token | None = None
    layer: Literal["base", "overlay"] | None = None
    span: WordSpan | None = None
    broll_prompt: str | None = None
    reaction_source: ReactionSource | None = None
    zooms: list[Zoom] | None = None
    speed_segments: list[SpeedSegment] | None = None
    webcam_bubble: WebcamBubble | None = None
    takes_count: PositiveInt | None = None


class SetShot(_Op):
    op: Literal["set_shot"] = "set_shot"
    scene_key: SceneKey | None = None
    shot_key: ShotKey
    changes: ShotChanges


# ---------------------------------------------------------------------- video level


class SetMeta(_Op):
    op: Literal["set_meta"] = "set_meta"
    quality_tier: QualityTier | None = None
    platform_targets: list[Token] | None = None
    primary_aspect: Aspect | None = None
    target_duration_s: float | None = Field(default=None, gt=0)
    title: NonEmptyStr | None = None
    add_template_ids: list[UUID] = Field(default_factory=list, description="spec templates applied (Phase 12)")


class SetRenderOutputs(_Op):
    op: Literal["set_render_outputs"] = "set_render_outputs"
    outputs: list[OutputPreset] | None = None
    reframe: Reframe | None = None


class ReplaceBroll(_Op):
    op: Literal["replace_broll"] = "replace_broll"
    scene_key: SceneKey | None = None
    shot_key: ShotKey
    broll: BrollSpec


class MusicCueChanges(SpecModel):
    mode: Literal["generate", "library", "asset"] | None = None
    mood: str | None = None
    bpm: PositiveInt | None = None
    asset_id: UUID | None = None
    duck_db: float | None = Field(default=None, le=0)


class SetMusic(_Op):
    op: Literal["set_music"] = "set_music"
    add: list[MusicCue] = Field(default_factory=list)
    remove: list[MusicCueKey] = Field(default_factory=list)
    update: dict[MusicCueKey, MusicCueChanges] = Field(default_factory=dict)


class SfxChanges(SpecModel):
    description: NonEmptyStr | None = None
    gain_db: float | None = Field(default=None, le=6)
    asset_id: UUID | None = None


class SetSfx(_Op):
    op: Literal["set_sfx"] = "set_sfx"
    add: list[SfxEvent] = Field(default_factory=list)
    remove: list[SfxKey] = Field(default_factory=list)
    update: dict[SfxKey, SfxChanges] = Field(default_factory=dict)


class CaptionChanges(SpecModel):
    enabled: bool | None = None
    style_id: Token | None = None
    language: LanguageTag | None = None
    max_words_per_line: PositiveInt | None = None
    highlight: Literal["active_word", "phrase", "none"] | None = None
    placement: Literal["platform_safe_zone", "bottom", "center", "top"] | None = None
    translations: list[CaptionTranslation] | None = None


class SetCaptions(_Op):
    op: Literal["set_captions"] = "set_captions"
    changes: CaptionChanges


class SetEffects(_Op):
    op: Literal["set_effects"] = "set_effects"
    add: list[Effect] = Field(default_factory=list)
    remove: list[EffectKey] = Field(default_factory=list)


class SetBrand(_Op):
    op: Literal["set_brand"] = "set_brand"
    brand_kit_id: UUID | None = None
    clear_brand_kit: bool = False
    logo_overlay: bool | None = None


class SetProduct(_Op):
    op: Literal["set_product"] = "set_product"
    add: list[ProductRef] = Field(default_factory=list)
    remove: list[ProductKey] = Field(default_factory=list)


class SetProvenanceLabel(_Op):
    op: Literal["set_provenance_label"] = "set_provenance_label"
    visible_label: Literal["auto", "on", "off"]


# ---------------------------------------------------------------------- generation control


class Regenerate(_Op):
    """Seed override and/or a component re-run (§12.7): `seed_policy: new` writes
    `generation.seed_overrides` for the component's nodes in scope."""

    op: Literal["regenerate"] = "regenerate"
    scope: EditScope = Field(default_factory=EditScope)
    components: list[Token] = Field(min_length=1)
    seed_policy: Literal["same", "new"] = "new"
    takes: PositiveInt | None = None
    strategy: Literal["full", "lipsync_patch"] = "full"


class Reroute(_Op):
    """Explicitly re-routes nodes (§12.4); `adapter_id` also pins the engine (`engine_hints`)."""

    op: Literal["reroute"] = "reroute"
    node_keys: list[NonEmptyStr] = Field(min_length=1)
    adapter_id: Token | None = None


class SelectTake(_Op):
    op: Literal["select_take"] = "select_take"
    shot_key: ShotKey
    take_key: TakeKey | None = Field(description="none returns the choice to QC ranking")


class LockRemoval(SpecModel):
    group: Token
    scope: LockScope | None = Field(default=None, description="none removes every lock of the group")


class SetLock(_Op):
    op: Literal["set_lock"] = "set_lock"
    add: list[Lock] = Field(default_factory=list)
    remove: list[LockRemoval] = Field(default_factory=list)


class RefreshMemory(_Op):
    op: Literal["refresh_memory"] = "refresh_memory"
    character_keys: list[CharacterKey] | None = None
    snapshot_ids: dict[CharacterKey, UUID] = Field(
        default_factory=dict, description="new snapshots pinned at proposal time"
    )


class MemoryFeedback(_Op):
    """Proposes a Creator Memory item (a side effect outside the spec, confirmed separately, §18.4)."""

    op: Literal["memory_feedback"] = "memory_feedback"
    character_key: CharacterKey | None = None
    kind: NonEmptyStr
    value: dict[str, Any] = Field(default_factory=dict)
    text: str = Field(default="", max_length=500)


EditOperation = Annotated[
    SetIntent
    | SetActing
    | AddBehaviorEvent
    | RemoveBehaviorEvent
    | SetBehaviorEvent
    | AddAnnotation
    | RemoveAnnotation
    | SetAnnotation
    | SetWorldBinding
    | SetWorldOverride
    | SetWardrobe
    | SetCast
    | SetCamera
    | SetPacing
    | EditScript
    | AddScene
    | RemoveScene
    | MoveScene
    | AddShot
    | RemoveShot
    | SplitShot
    | SetShot
    | SetMeta
    | SetRenderOutputs
    | ReplaceBroll
    | SetMusic
    | SetSfx
    | SetCaptions
    | SetEffects
    | SetBrand
    | SetProduct
    | SetProvenanceLabel
    | Regenerate
    | Reroute
    | SelectTake
    | SetLock
    | RefreshMemory
    | MemoryFeedback,
    Field(discriminator="op"),
]

OPERATIONS: tuple[type[SpecModel], ...] = (
    SetIntent,
    SetActing,
    AddBehaviorEvent,
    RemoveBehaviorEvent,
    SetBehaviorEvent,
    AddAnnotation,
    RemoveAnnotation,
    SetAnnotation,
    SetWorldBinding,
    SetWorldOverride,
    SetWardrobe,
    SetCast,
    SetCamera,
    SetPacing,
    EditScript,
    AddScene,
    RemoveScene,
    MoveScene,
    AddShot,
    RemoveShot,
    SplitShot,
    SetShot,
    SetMeta,
    SetRenderOutputs,
    ReplaceBroll,
    SetMusic,
    SetSfx,
    SetCaptions,
    SetEffects,
    SetBrand,
    SetProduct,
    SetProvenanceLabel,
    Regenerate,
    Reroute,
    SelectTake,
    SetLock,
    RefreshMemory,
    MemoryFeedback,
)
EDIT_OPERATION_TYPES: tuple[str, ...] = tuple(str(model.model_fields["op"].default) for model in OPERATIONS)

operation_list: TypeAdapter[list[EditOperation]] = TypeAdapter(list[EditOperation])


class EditOperations(SpecModel):
    """A list of operations as one document: the JSON schema of `POST /v1/versions/{id}/edits`
    `operations` and of the Advanced editors (§28)."""

    operations: list[EditOperation] = Field(max_length=50)


def parse_operations(data: Any) -> list[EditOperation]:
    """Validates a JSON list of operations (unknown op names and fields are rejected)."""
    return operation_list.validate_python(data)
