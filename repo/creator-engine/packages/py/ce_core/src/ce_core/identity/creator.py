"""Creator DNA (§17.1), Appearance DNA (§17.2), Voice (§21) and Wardrobe models.

DNA describes tendencies, targets and bounds — never engine controls (I1). Control-like
fields use vocabulary tokens; `validate_creator_dna` checks them against a Vocabulary.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from ce_core.errors import Issue
from ce_core.scalars import LanguageTag, NonEmptyStr, Token, Unit
from ce_core.spec.base import SpecModel
from ce_core.vocab import Vocabulary

__all__ = [
    "AppearanceDNA",
    "BehaviorBaseline",
    "CanonFact",
    "CreatorDNA",
    "ReactionHabit",
    "VoiceDNA",
    "VoiceReference",
    "WardrobeSpec",
    "validate_creator_dna",
]

REACTION_KINDS = ("surprise", "laughter", "hesitation", "skepticism", "excitement", "frustration")


class CanonFact(SpecModel):
    """A fact the contradiction checker compares scripts against (§18.7)."""

    key: Token
    subject: NonEmptyStr
    predicate: NonEmptyStr
    object: NonEmptyStr
    pinned: bool = True


class IdentitySection(SpecModel):
    display_name: NonEmptyStr
    bio: str = ""
    canon: list[CanonFact] = Field(default_factory=list)


class Traits(SpecModel):
    openness: Unit = 0.5
    conscientiousness: Unit = 0.5
    extraversion: Unit = 0.5
    agreeableness: Unit = 0.5
    neuroticism: Unit = 0.5


class Personality(SpecModel):
    traits: Traits = Field(default_factory=Traits)
    values: list[str] = Field(default_factory=list)
    humor_style: Token = "none"
    directness: Unit = 0.5
    warmth: Unit = 0.5
    sarcasm: Unit = 0.0
    seriousness: Unit = 0.5
    conversational_style: Token = "casual"


class SignaturePhrase(SpecModel):
    text: NonEmptyStr
    max_per_video: PositiveInt = 1


class FillerTendency(SpecModel):
    fillers: list[str] = Field(default_factory=list)
    rate: Unit = 0.0


class PronunciationNote(SpecModel):
    term: NonEmptyStr
    respelling: NonEmptyStr


class Speech(SpecModel):
    vocabulary_level: Token = "conversational"
    signature_phrases: list[SignaturePhrase] = Field(default_factory=list)
    sentence_structure: Token = "balanced"
    filler_tendency: FillerTendency = Field(default_factory=FillerTendency)
    transition_phrases: list[str] = Field(default_factory=list)
    pronunciation_notes: list[PronunciationNote] = Field(default_factory=list)
    speech_rate_preference: Token = "average"


class BehaviorBaseline(SpecModel):
    energy: Unit = 0.5
    confidence: Unit = 0.5
    warmth: Unit = 0.5
    expressivity: Unit = 0.5


class ReactionHabit(SpecModel):
    expression: Token = Field(description="an event type")
    intensity: Unit
    frequency: Token = "sometimes"


class BehaviorSection(SpecModel):
    baseline: BehaviorBaseline = Field(default_factory=BehaviorBaseline)
    emotion_ranges: dict[Token, tuple[Unit, Unit]] = Field(default_factory=dict)
    escalation_style: Token = "gradual"
    deescalation_style: Token = "gradual"
    masking_tendency: Unit = 0.3
    reaction_habits: dict[
        Literal["surprise", "laughter", "hesitation", "skepticism", "excitement", "frustration"], ReactionHabit
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ranges_ordered(self) -> BehaviorSection:
        for label, (low, high) in self.emotion_ranges.items():
            if low > high:
                raise ValueError(f"emotion range {label}: min {low} > max {high}")
        return self


class PreferredGesture(SpecModel):
    gesture: Token
    frequency: Token = "sometimes"


class GestureSection(SpecModel):
    preferred_gestures: list[PreferredGesture] = Field(default_factory=list)
    gesture_density: Unit = 0.5
    preferred_hand: Token = "right"
    head_movement_amplitude: Unit = 0.5
    default_posture: Token = "seated_upright"
    fidgets: list[str] = Field(default_factory=list)


class GazeSection(SpecModel):
    eye_contact_ratio: Unit = 0.7
    look_away_directions: list[Token] = Field(default_factory=list)
    look_away_ms: PositiveInt = 600
    thinking_gaze: Token | None = None


class CameraSection(SpecModel):
    framing_preference: Token = "medium_close_up"
    camera_distance: Token = "medium"
    selfie_behavior: Token = "propped_phone"
    movement_preference: Token = "subtle_handheld"
    preferred_camera_profiles: list[Token] = Field(default_factory=list)


class FashionSection(SpecModel):
    style_descriptors: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class WorldSection(SpecModel):
    world_kind_preferences: list[Token] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class EditingSection(SpecModel):
    cut_cadence: Literal["slow", "medium", "fast"] = "medium"
    caption_style_preference: Token | None = None
    music_taste: list[str] = Field(default_factory=list)


class EmotionCap(SpecModel):
    label: Token
    max_intensity: Unit


class Avoidances(SpecModel):
    gestures: list[Token] = Field(default_factory=list)
    emotions: list[EmotionCap] = Field(default_factory=list)
    camera_behaviors: list[str] = Field(default_factory=list)
    wardrobe: list[str] = Field(default_factory=list)
    worlds: list[str] = Field(default_factory=list)
    phrases: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class CreatorDNA(SpecModel):
    """`CreatorVersion.dna`. The default appearance, voice, worlds and wardrobes are columns, not DNA."""

    vocab_version: NonEmptyStr
    identity: IdentitySection
    personality: Personality = Field(default_factory=Personality)
    speech: Speech = Field(default_factory=Speech)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
    gesture: GestureSection = Field(default_factory=GestureSection)
    gaze: GazeSection = Field(default_factory=GazeSection)
    camera: CameraSection = Field(default_factory=CameraSection)
    fashion: FashionSection = Field(default_factory=FashionSection)
    world: WorldSection = Field(default_factory=WorldSection)
    editing: EditingSection = Field(default_factory=EditingSection)
    avoidances: Avoidances = Field(default_factory=Avoidances)

    def behavior_fields(self) -> dict[str, object]:
        """The behavior-relevant DNA subset the CBS reads (its digest is `dna_behavior_digest`, §15.6)."""
        return self.model_dump(mode="json", include={"behavior", "gesture", "gaze", "personality", "avoidances"})


def validate_creator_dna(dna: CreatorDNA, vocab: Vocabulary) -> list[Issue]:
    issues: list[Issue] = []
    if dna.vocab_version != vocab.version:
        issues.append(Issue("vocab_version", f"DNA validated against {dna.vocab_version}, current is {vocab.version}"))
    checks: list[tuple[str, str | None, str]] = [
        ("persona.humor_style", dna.personality.humor_style, "/personality/humor_style"),
        ("persona.conversational_style", dna.personality.conversational_style, "/personality/conversational_style"),
        ("persona.vocabulary_level", dna.speech.vocabulary_level, "/speech/vocabulary_level"),
        ("persona.sentence_structure", dna.speech.sentence_structure, "/speech/sentence_structure"),
        ("persona.speech_rate_preference", dna.speech.speech_rate_preference, "/speech/speech_rate_preference"),
        ("persona.escalation_style", dna.behavior.escalation_style, "/behavior/escalation_style"),
        ("persona.escalation_style", dna.behavior.deescalation_style, "/behavior/deescalation_style"),
        ("persona.hand", dna.gesture.preferred_hand, "/gesture/preferred_hand"),
        ("strategy.posture", dna.gesture.default_posture, "/gesture/default_posture"),
        ("direction", dna.gaze.thinking_gaze, "/gaze/thinking_gaze"),
        ("camera.framing", dna.camera.framing_preference, "/camera/framing_preference"),
        ("persona.camera_distance", dna.camera.camera_distance, "/camera/camera_distance"),
        ("persona.selfie_behavior", dna.camera.selfie_behavior, "/camera/selfie_behavior"),
        ("persona.movement_preference", dna.camera.movement_preference, "/camera/movement_preference"),
    ]
    for label in dna.behavior.emotion_ranges:
        checks.append(("emotion", label, f"/behavior/emotion_ranges[{label}]"))
    for kind, habit in dna.behavior.reaction_habits.items():
        checks.append(("event_type", habit.expression, f"/behavior/reaction_habits[{kind}]/expression"))
        checks.append(("persona.frequency", habit.frequency, f"/behavior/reaction_habits[{kind}]/frequency"))
    for g in dna.gesture.preferred_gestures:
        checks.append(("event_type", g.gesture, f"/gesture/preferred_gestures[{g.gesture}]"))
        checks.append(("persona.frequency", g.frequency, f"/gesture/preferred_gestures[{g.gesture}]/frequency"))
    checks += [("direction", d, f"/gaze/look_away_directions[{d}]") for d in dna.gaze.look_away_directions]
    checks += [("world_kind", k, f"/world/world_kind_preferences[{k}]") for k in dna.world.world_kind_preferences]
    checks += [("event_type", g, f"/avoidances/gestures[{g}]") for g in dna.avoidances.gestures]
    checks += [("emotion", e.label, f"/avoidances/emotions[{e.label}]") for e in dna.avoidances.emotions]
    for category, token, path in checks:
        issues += vocab.check(category, token, path=path)
    for g in dna.gesture.preferred_gestures:
        event = vocab.events.get(g.gesture)
        if event is not None and event.dimension != "gesture":
            issues.append(Issue("not_a_gesture", f"{g.gesture} is a {event.dimension} event, not a gesture"))
    return issues


class AppearanceDNA(SpecModel):
    """Visual identity (§17.2). `age_appearance` must be at least 18 (§17.3)."""

    face_shape: str = ""
    skin: str = ""
    hair: str = ""
    eyes: str = ""
    distinctive_features: list[str] = Field(default_factory=list)
    body_type: str = ""
    grooming_style: str = ""
    age_appearance: int = Field(ge=18, le=100)


class VoiceReference(SpecModel):
    language: LanguageTag
    asset_id: UUID
    transcript: NonEmptyStr


class LexiconEntry(SpecModel):
    term: NonEmptyStr
    respelling: str | None = None
    phonemes: str | None = None

    @model_validator(mode="after")
    def _one_form(self) -> LexiconEntry:
        if (self.respelling is None) == (self.phonemes is None):
            raise ValueError(f"lexicon entry {self.term!r} needs exactly one of respelling or phonemes")
        return self


class DefaultProsody(SpecModel):
    accent: str | None = None
    pitch_semitones: float = Field(default=0.0, ge=-12, le=12)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    energy: Unit = 0.5
    expressivity: Unit = 0.5


class VoiceDNA(SpecModel):
    """The content of a `VoiceVersion` (§21). Engine conditioning is a `voice_conditioning` artifact, not here."""

    kind: Literal["designed", "cloned", "preset"] = "designed"
    description: str = ""
    references: list[VoiceReference] = Field(default_factory=list)
    wpm: dict[LanguageTag, PositiveFloat] = Field(default_factory=dict, description="measured words per minute")
    lexicon: list[LexiconEntry] = Field(default_factory=list)
    default_prosody: DefaultProsody = Field(default_factory=DefaultProsody)
    consent_id: UUID | None = None

    @model_validator(mode="after")
    def _cloned_needs_consent(self) -> VoiceDNA:
        if self.kind == "cloned" and self.consent_id is None:
            raise ValueError("a cloned voice needs a consent_id (§21, §32)")
        return self

    def wpm_for(self, language: str, default: float = 150.0) -> float:
        if language in self.wpm:
            return self.wpm[language]
        primary = language.split("-")[0]
        for tag, value in self.wpm.items():
            if tag.split("-")[0] == primary:
                return value
        return default


class WardrobeSpec(SpecModel):
    """One outfit of a creator (`WardrobeVersion`)."""

    name: NonEmptyStr
    description: str = ""
    reference_asset_ids: list[UUID] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
