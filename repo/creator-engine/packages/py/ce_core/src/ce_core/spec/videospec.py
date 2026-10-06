"""The canonical VideoSpec (§11): the creative source of truth, immutable per version (ADR 0002).

Structural rules that need only the model itself (types, key formats, discriminated unions,
unique keys, read-only computed fields) are enforced here. Rules that need the vocabulary,
referenced records, World DNA or the tokenizer (anchors in bounds, tiling, durations) live in
`ce_core.spec.validate.validate_spec`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from ce_core.canonical import content_digest
from ce_core.enums import AnnotationType, CastRole, ElementSource, InputMode, QualityTier, ShotLayer, ShotType
from ce_core.keys import (
    AnnotationKey,
    CameraMoveKey,
    CharacterKey,
    ClaimKey,
    HookKey,
    MusicCueKey,
    ProductKey,
    SceneKey,
    SegmentKey,
    SfxKey,
    ShotKey,
    TakeKey,
    ZoneKey,
    ZoomKey,
)
from ce_core.scalars import Aspect, LanguageTag, NonEmptyStr, Token, Unit
from ce_core.spec.acting import ActingPlan
from ce_core.spec.anchors import DurationSpan, TimePoint, TimeSpan, WordRef, WordSpan
from ce_core.spec.base import SpecModel
from ce_core.spec.common import AssetRef, DerivedFrom, Effect
from ce_core.spec.intent import SceneIntent, VideoIntentBlock
from ce_core.spec.world import WorldBinding

__all__ = [
    "SCHEMA_VERSION",
    "Annotation",
    "Audio",
    "Brand",
    "BrollSpec",
    "CameraMove",
    "CameraSpec",
    "CaptionTranslation",
    "Captions",
    "CastMember",
    "CastOverrides",
    "Claim",
    "CreativeBrief",
    "Generation",
    "HookCandidate",
    "Keyframe",
    "Lock",
    "LockScope",
    "Loudness",
    "MemoryPins",
    "Meta",
    "MusicCue",
    "OutputPreset",
    "Pacing",
    "ProductRef",
    "Provenance",
    "QuoteSource",
    "ReactionSource",
    "Reframe",
    "Render",
    "ResearchDossier",
    "Scene",
    "SceneCast",
    "ScreenSpec",
    "Script",
    "Segment",
    "SfxEvent",
    "Shot",
    "SnapshotPin",
    "SpeedSegment",
    "Takes",
    "TitleCard",
    "VideoSpec",
    "VisualSpec",
    "VoiceProsody",
    "WebcamBubble",
    "Zoom",
]

SCHEMA_VERSION: Literal["1.0"] = "1.0"


# ---------------------------------------------------------------------- meta and brief


class Meta(SpecModel):
    title: NonEmptyStr
    mode: Token = Field(description="a mode template id (config/modes)")
    language: LanguageTag
    platform_targets: list[Token] = Field(default_factory=list)
    primary_aspect: Aspect = "9:16"
    target_duration_s: PositiveFloat
    quality_tier: QualityTier = QualityTier.DRAFT
    template_ids: list[UUID] = Field(default_factory=list)
    strategy_pack: Token | None = None


class HookCandidate(SpecModel):
    key: HookKey
    text: NonEmptyStr


class CreativeBrief(SpecModel):
    """Director stage 1 output (§13). `raw_input` is the user's text, stored byte for byte."""

    input_mode: InputMode
    raw_input: str
    audience: str = ""
    angle: str = ""
    assumptions: list[str] = Field(default_factory=list)
    hook_candidates: list[HookCandidate] = Field(default_factory=list)
    selected_hook_key: HookKey | None = None
    sources_policy: Literal["open", "closed_book"] = "open"
    constraints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _selected_hook_exists(self) -> CreativeBrief:
        if self.selected_hook_key and self.selected_hook_key not in {h.key for h in self.hook_candidates}:
            raise ValueError(f"selected_hook_key {self.selected_hook_key} is not a hook candidate")
        return self


class Claim(SpecModel):
    key: ClaimKey
    text: NonEmptyStr
    verdict: Literal["supported", "unsupported", "uncertain", "conflicting"] = "uncertain"
    evidence_ids: list[str] = Field(default_factory=list)
    overridden: bool = False


class ResearchDossier(SpecModel):
    """Director stage 3 output; source text is data, never instructions (I10)."""

    claims: list[Claim] = Field(default_factory=list)
    source_ids: list[UUID] = Field(default_factory=list)
    closed_book: bool = False


# ---------------------------------------------------------------------- identity references


class SnapshotPin(SpecModel):
    character_key: CharacterKey
    snapshot_id: UUID


class MemoryPins(SpecModel):
    """One MemorySnapshot per cast member (I7). Derived versions inherit them unless `refresh_memory` is applied."""

    snapshots: list[SnapshotPin] = Field(default_factory=list)


class CastOverrides(SpecModel):
    appearance_version_id: UUID | None = None
    voice_version_id: UUID | None = None


class VoiceProsody(SpecModel):
    """A per-video offset on the voice version's default prosody (§10.6)."""

    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch_semitones: float = Field(default=0.0, ge=-12, le=12)
    energy: Unit = 0.5


class CastMember(SpecModel):
    key: CharacterKey
    role: CastRole = CastRole.HOST
    creator_version_id: UUID
    overrides: CastOverrides = Field(default_factory=CastOverrides)
    voice_prosody: VoiceProsody | None = None


class ProductRef(SpecModel):
    key: ProductKey
    product_version_id: UUID


# ---------------------------------------------------------------------- script


class QuoteSource(SpecModel):
    """A real customer quote: needs consent (§32)."""

    consent_id: UUID
    asset_id: UUID


class Annotation(SpecModel):
    key: AnnotationKey
    type: AnnotationType
    tag: Token = Field(description="a token of annotation_tag.<type>")
    span: WordSpan
    source: ElementSource = ElementSource.DIRECTOR
    pause_ms: PositiveInt | None = Field(default=None, description="explicit pause length; otherwise from the tag")
    respelling: str | None = Field(default=None, description="pronunciation respelling (type pronunciation)")

    @model_validator(mode="after")
    def _type_specific_fields(self) -> Annotation:
        if self.pause_ms is not None and self.type != AnnotationType.PAUSE:
            raise ValueError("pause_ms is only valid on pause annotations")
        if self.type == AnnotationType.PRONUNCIATION and not self.respelling:
            raise ValueError("pronunciation annotations need a respelling")
        if self.respelling is not None and self.type != AnnotationType.PRONUNCIATION:
            raise ValueError("respelling is only valid on pronunciation annotations")
        return self


class Segment(SpecModel):
    key: SegmentKey
    speaker_key: CharacterKey
    text: NonEmptyStr
    language: LanguageTag | None = Field(default=None, description="null = meta.language")
    annotations: list[Annotation] = Field(default_factory=list)
    claim_keys: list[ClaimKey] = Field(default_factory=list)
    quote_source: QuoteSource | None = None


class Script(SpecModel):
    segments: list[Segment] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _wording_locked_is_read_only(cls, data: Any) -> Any:
        if isinstance(data, dict) and "wording_locked" in data:
            raise ValueError("script.wording_locked is a read-only computed property; set or clear the `script` lock")
        return data

    def segment(self, key: str) -> Segment:
        for seg in self.segments:
            if seg.key == key:
                return seg
        raise KeyError(key)


# ---------------------------------------------------------------------- scenes and shots


class SceneCast(SpecModel):
    character_key: CharacterKey
    wardrobe_version_id: UUID | None = None
    placement: ZoneKey | None = None
    default_posture: Token | None = None


class Pacing(SpecModel):
    target_wpm_delta: float = Field(default=0.0, ge=-0.5, le=0.5, description="relative change of the WPM target")
    cut_cadence: Literal["slow", "medium", "fast"] | None = None


class CameraMove(SpecModel):
    key: CameraMoveKey
    type: Token = Field(description="camera.move_type")
    at: WordRef
    scale: float | None = Field(default=None, gt=0.5, lt=3.0)
    transition: Token = "cut"
    derived_from: list[DerivedFrom] = Field(default_factory=list)


class CameraSpec(SpecModel):
    profile_id: Token = Field(description="config/camera_profiles id")
    framing: Token
    angle: Token = "eye_level"
    moves: list[CameraMove] = Field(default_factory=list)


class Keyframe(SpecModel):
    strategy: Literal["composite", "asset"] = "composite"
    asset_id: UUID | None = None

    @model_validator(mode="after")
    def _asset_strategy_needs_asset(self) -> Keyframe:
        if self.strategy == "asset" and self.asset_id is None:
            raise ValueError("keyframe strategy 'asset' needs an asset_id")
        return self


class VisualSpec(SpecModel):
    keyframe: Keyframe = Field(default_factory=Keyframe)
    prompt_extra: str = ""
    negative: str = ""


class Takes(SpecModel):
    count: PositiveInt = 1
    selected_take_key: TakeKey | None = None

    @model_validator(mode="after")
    def _selected_take_in_range(self) -> Takes:
        if self.selected_take_key is not None and int(self.selected_take_key.removeprefix("tk_")) > self.count:
            raise ValueError(f"selected take {self.selected_take_key} exceeds takes.count {self.count}")
        return self


class BrollSpec(SpecModel):
    source: Literal["generate", "asset", "library"] = "generate"
    asset_id: UUID | None = None
    prompt: str = ""
    world_bound: bool = False
    allow_text_in_frame: bool = False

    @model_validator(mode="after")
    def _source_fields(self) -> BrollSpec:
        if self.source == "asset" and self.asset_id is None:
            raise ValueError("B-roll source 'asset' needs an asset_id")
        if self.source == "generate" and not self.prompt:
            raise ValueError("generated B-roll needs a prompt")
        return self


class Zoom(SpecModel):
    key: ZoomKey
    span: WordSpan | DurationSpan
    rect: tuple[Unit, Unit, Unit, Unit] = Field(description="x, y, width, height of the zoom window (0..1)")
    ease: Literal["linear", "ease_in_out", "ease_out", "snap"] = "ease_in_out"


class SpeedSegment(SpecModel):
    span: WordSpan | DurationSpan
    speed: float = Field(gt=0.1, le=8.0)


class WebcamBubble(SpecModel):
    enabled: bool = False
    corner: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = "bottom_right"
    size: Unit = 0.25


class ScreenSpec(SpecModel):
    asset_id: UUID
    zooms: list[Zoom] = Field(default_factory=list)
    speed_segments: list[SpeedSegment] = Field(default_factory=list)
    webcam_bubble: WebcamBubble = Field(default_factory=WebcamBubble)


class ReactionSource(SpecModel):
    asset_id: UUID
    range_s: tuple[float, float]
    layout: Literal["pip", "split"] = "pip"

    @model_validator(mode="after")
    def _range_forward(self) -> ReactionSource:
        if not 0 <= self.range_s[0] < self.range_s[1]:
            raise ValueError("reaction range_s must be [start, end] with 0 <= start < end")
        return self


class TitleCard(SpecModel):
    text: NonEmptyStr
    style_id: Token | None = None


class Shot(SpecModel):
    key: ShotKey
    type: ShotType
    layer: ShotLayer
    span: WordSpan | DurationSpan
    character_key: CharacterKey | None = None
    camera: CameraSpec
    visual: VisualSpec | None = None
    takes: Takes = Field(default_factory=Takes)
    broll: BrollSpec | None = None
    screen: ScreenSpec | None = None
    reaction_source: ReactionSource | None = None
    product_key: ProductKey | None = None
    title: TitleCard | None = None
    derived_from: list[DerivedFrom] = Field(default_factory=list)

    @model_validator(mode="after")
    def _type_specific_parts(self) -> Shot:
        t = self.type
        needs_character = {ShotType.TALKING_HEAD, ShotType.SILENT_HOLD, ShotType.TWO_SHOT}
        if t in needs_character and self.character_key is None:
            raise ValueError(f"shot {self.key}: a {t} shot needs a character_key")
        if t == ShotType.BROLL and self.broll is None:
            raise ValueError(f"shot {self.key}: a broll shot needs `broll`")
        if t == ShotType.SCREEN and self.screen is None:
            raise ValueError(f"shot {self.key}: a screen shot needs `screen`")
        if t == ShotType.REACTION_CLIP and self.reaction_source is None:
            raise ValueError(f"shot {self.key}: a reaction_clip shot needs `reaction_source`")
        if t == ShotType.PRODUCT and self.product_key is None:
            raise ValueError(f"shot {self.key}: a product shot needs `product_key`")
        if t == ShotType.TITLE_CARD and self.title is None:
            raise ValueError(f"shot {self.key}: a title_card shot needs `title`")
        if self.broll is not None and t not in {ShotType.BROLL, ShotType.INSERT, ShotType.PRODUCT}:
            raise ValueError(f"shot {self.key}: `broll` is only valid on broll, insert or product shots")
        return self


class Scene(SpecModel):
    key: SceneKey
    purpose: Token = Field(description="intent.scene_purpose")
    order: PositiveInt
    segment_keys: list[SegmentKey] = Field(default_factory=list)
    intent: SceneIntent = Field(default_factory=SceneIntent)
    world: WorldBinding | None = None
    cast: list[SceneCast] = Field(default_factory=list)
    acting: ActingPlan | None = None
    pacing: Pacing | None = None
    shots: list[Shot] = Field(default_factory=list)


# ---------------------------------------------------------------------- audio, captions, output


class MusicCue(SpecModel):
    key: MusicCueKey
    span: TimeSpan
    mode: Literal["generate", "library", "asset"] = "generate"
    mood: str = ""
    bpm: PositiveInt | None = None
    asset_id: UUID | None = None
    duck_db: float = Field(default=-18.0, le=0)
    derived_from: list[DerivedFrom] = Field(default_factory=list)


class MusicBlock(SpecModel):
    cues: list[MusicCue] = Field(default_factory=list)


class SfxEvent(SpecModel):
    key: SfxKey
    at: TimePoint
    description: NonEmptyStr
    gain_db: float = Field(default=-10.0, le=6)
    asset_id: UUID | None = None
    derived_from: list[DerivedFrom] = Field(default_factory=list)


class Acoustics(SpecModel):
    source: Literal["world", "override"] = "world"
    mic_profile: Token | None = None
    room_profile: Token | None = None

    @model_validator(mode="after")
    def _override_needs_room(self) -> Acoustics:
        if self.source == "override" and self.room_profile is None:
            raise ValueError("acoustics source 'override' needs a room_profile")
        return self


class Loudness(SpecModel):
    integrated_lufs: float = Field(default=-14.0, ge=-40, le=-5)
    true_peak_dbtp: float = Field(default=-1.0, ge=-10, le=0)


class Audio(SpecModel):
    music: MusicBlock = Field(default_factory=MusicBlock)
    sfx: list[SfxEvent] = Field(default_factory=list)
    acoustics: Acoustics = Field(default_factory=Acoustics)
    loudness: Loudness = Field(default_factory=Loudness)


class CaptionTranslation(SpecModel):
    language: LanguageTag


class Captions(SpecModel):
    enabled: bool = True
    style_id: Token = "bold_pop_highlight"
    language: LanguageTag | None = None
    max_words_per_line: PositiveInt = 3
    highlight: Literal["active_word", "phrase", "none"] = "active_word"
    placement: Literal["platform_safe_zone", "bottom", "center", "top"] = "platform_safe_zone"
    translations: list[CaptionTranslation] = Field(default_factory=list)
    review_state: dict[str, Literal["pending", "approved", "rejected"]] = Field(default_factory=dict)


class Brand(SpecModel):
    brand_kit_id: UUID | None = None
    logo_overlay: bool = False


class OutputPreset(SpecModel):
    preset_id: Token = Field(description="a render preset id from config/platforms")
    aspect: Aspect


class Reframe(SpecModel):
    strategy: Literal["subject_aware", "center", "none"] = "subject_aware"
    regenerate_if_crop_loss_above: Unit = 0.25


class Render(SpecModel):
    outputs: list[OutputPreset] = Field(default_factory=list)
    reframe: Reframe = Field(default_factory=Reframe)


class Provenance(SpecModel):
    visible_label: Literal["auto", "on", "off"] = "auto"
    consent_ids: list[UUID] = Field(default_factory=list)


class Generation(SpecModel):
    """`seed_namespace` is the original video_id (copied on duplicate); seeds never derive from version ids."""

    seed_namespace: UUID
    seed_overrides: dict[str, NonNegativeInt] = Field(default_factory=dict)
    engine_hints: dict[str, str] = Field(default_factory=dict, description="user pins: node_key -> adapter_id")


class LockScope(SpecModel):
    scene_keys: list[SceneKey] | None = None
    character_keys: list[CharacterKey] | None = None
    shot_keys: list[ShotKey] | None = None


class Lock(SpecModel):
    group: Token = Field(description="a lock group from config/vocab/edit_vocabulary.yaml")
    scope: LockScope = Field(default_factory=LockScope)
    set_by: Literal["user", "system"] = "user"


# ---------------------------------------------------------------------- the spec


class VideoSpec(SpecModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    vocab_version: NonEmptyStr
    tokenizer_version: NonEmptyStr
    video_id: UUID
    version_id: UUID
    parent_version_id: UUID | None = None
    meta: Meta
    brief: CreativeBrief
    research: ResearchDossier | None = None
    intent: VideoIntentBlock = Field(default_factory=VideoIntentBlock)
    memory: MemoryPins = Field(default_factory=MemoryPins)
    cast: list[CastMember] = Field(min_length=1)
    products: list[ProductRef] = Field(default_factory=list)
    script: Script
    scenes: list[Scene] = Field(min_length=1)
    audio: Audio = Field(default_factory=Audio)
    captions: Captions = Field(default_factory=Captions)
    brand: Brand = Field(default_factory=Brand)
    render: Render = Field(default_factory=Render)
    provenance: Provenance = Field(default_factory=Provenance)
    assets: list[AssetRef] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    generation: Generation
    locks: list[Lock] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self) -> VideoSpec:
        duplicates = sorted(key for key, n in Counter(self.iter_keys()).items() if n > 1)
        if duplicates:
            raise ValueError(f"duplicate keys: {duplicates}")
        return self

    # ------------------------------------------------------------------ helpers
    def iter_keys(self) -> Iterator[str]:
        """Every element key in the spec (cast, segments, annotations, scenes, shots, states, events…)."""
        yield from (h.key for h in self.brief.hook_candidates)
        if self.research:
            yield from (c.key for c in self.research.claims)
        yield from (c.key for c in self.cast)
        yield from (p.key for p in self.products)
        for seg in self.script.segments:
            yield seg.key
            yield from (a.key for a in seg.annotations)
        for scene in self.scenes:
            yield scene.key
            if scene.acting:
                yield from (s.key for s in scene.acting.states)
                yield from (e.key for e in scene.acting.events)
            for shot in scene.shots:
                yield shot.key
                yield from (m.key for m in shot.camera.moves)
                if shot.screen:
                    yield from (z.key for z in shot.screen.zooms)
        yield from (c.key for c in self.audio.music.cues)
        yield from (s.key for s in self.audio.sfx)
        yield from (a.key for a in self.assets)
        yield from (e.key for e in self.effects)

    @property
    def wording_locked(self) -> bool:
        """`script.wording_locked`: true while the `script` lock group is active (§11)."""
        return any(lock.group == "script" for lock in self.locks)

    def scene(self, key: str) -> Scene:
        for scene in self.scenes:
            if scene.key == key:
                return scene
        raise KeyError(key)

    def shots(self) -> Iterator[tuple[Scene, Shot]]:
        for scene in self.scenes:
            for shot in scene.shots:
                yield scene, shot

    def content_dict(self) -> dict[str, Any]:
        """The spec without `video_id`, `version_id` and `parent_version_id` (§12.2)."""
        return self.model_dump(mode="json", exclude={"video_id", "version_id", "parent_version_id"})

    def content_digest(self) -> str:
        return content_digest(self.content_dict())

    def to_api(self) -> dict[str, Any]:
        """The API representation: adds the computed, read-only `script.wording_locked`."""
        data = self.model_dump(mode="json")
        data["script"]["wording_locked"] = self.wording_locked
        return data
