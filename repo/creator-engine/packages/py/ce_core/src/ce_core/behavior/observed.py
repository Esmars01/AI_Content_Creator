"""ObservedBehavior (§16.3): measurements of one raw take, plus the judgement models."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from ce_core.enums import ObservationVerdict
from ce_core.keys import CharacterKey
from ce_core.scalars import Digest, NonEmptyStr, Token, Unit
from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPathStr

__all__ = ["AnalyzerRef", "BehaviorSignature", "CharacterTracks", "ItemObservation", "ObservedBehavior", "TrackEvent"]


class AnalyzerRef(SpecModel):
    capability: NonEmptyStr
    adapter_id: NonEmptyStr
    revision: NonEmptyStr


class TrackEvent(SpecModel):
    type: NonEmptyStr
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    value: float | None = None
    confidence: Unit = 1.0


class CharacterTracks(SpecModel):
    """Summarized tracks per visible character (series sampled at `sample_hz`)."""

    character_key: CharacterKey
    sample_hz: float = Field(default=5.0, gt=0)
    series: dict[str, list[float]] = Field(
        default_factory=dict, description="gaze_deviation_deg, smile, head_motion, gesture_energy, speech_rate, pitch…"
    )
    events: list[TrackEvent] = Field(default_factory=list, description="blinks, look-aways, smiles, pauses…")
    face_detected_ratio: Unit = 0.0


class BehaviorSignature(SpecModel):
    """Compact fingerprint used by the repetition guard and take ranking (§15.8)."""

    gesture_energy_profile: list[float] = Field(default_factory=list)
    head_motion_pattern: list[float] = Field(default_factory=list)
    expression_sequence: list[str] = Field(default_factory=list)
    arc_shape: list[float] = Field(default_factory=list)


class ObservedBehavior(SpecModel):
    """Measurement half only: keyed by the take artifact (its content hash), reused while the take
    is unchanged."""

    take_sha256: Digest | None = None
    take_artifact_id: UUID | None = None
    analyzers: list[AnalyzerRef] = Field(default_factory=list)
    tracks: list[CharacterTracks] = Field(default_factory=list)
    signature: BehaviorSignature = Field(default_factory=BehaviorSignature)


class ItemObservation(SpecModel):
    """Judgement of one CBS item against measured tracks (written by qc.shot and behavior.coverage)."""

    item_ref: SpecPathStr
    character_key: CharacterKey
    dimension: Token
    verdict: ObservationVerdict
    measures: dict[str, float] = Field(default_factory=dict)
    confidence: Unit = 0.0
    method: NonEmptyStr = Field(description="the proxy key or 'vlm_window' used")
