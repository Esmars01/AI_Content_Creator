"""Behavior matrix schema (§23) and the behavior directives a translator receives (§15.7).

`BehaviorMatrix` is the manifest field `behavior_matrix`: per requestable dimension (names from
`config/vocab/behavior_dimensions.yaml`) the declared control and temporal precision, plus the
engine-property dimensions. Declared values are claims until measured (ADR 0027).

`BehaviorDirectives` is the wire form of `ce_core.behavior.compiled.CompiledBehavior` for one
engine request, with anchors already resolved to seconds relative to the request's audio. It is
defined here (not imported from ce_core) because this package must stay Python 3.10-compatible;
`tests/phase2/test_contracts_sync.py` checks that a CompiledBehavior dump validates as directives
(ADR 0032).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ce_contracts.common import ContractModel

__all__ = [
    "CONTROL_KINDS",
    "ENGINE_PROPERTIES",
    "TEMPORAL_PRECISIONS",
    "BehaviorDirectives",
    "BehaviorMatrix",
    "DimensionControl",
    "DirectiveRealization",
    "DirectiveSubSpan",
    "EditorialDirective",
    "KnobSpec",
    "ProsodyDirectives",
    "VisualDirectives",
    "precision_rank",
]

CONTROL_KINDS = (
    "none",
    "emergent",
    "text_global",
    "text_segment",
    "native_segment",
    "parametric",
    "keyframe",
    "pose_guided",
)
TEMPORAL_PRECISIONS = ("none", "clip", "segment", "word", "frame")
ENGINE_PROPERTIES = (
    "segment_control",
    "prosody_coupling",
    "continuity",
    "object_interaction",
    "multi_person",
    "temporal_control",
)

Control = Literal[
    "none", "emergent", "text_global", "text_segment", "native_segment", "parametric", "keyframe", "pose_guided"
]
Precision = Literal["none", "clip", "segment", "word", "frame"]
Strength = Literal["none", "weak", "medium", "strong"]


def precision_rank(value: str) -> int:
    """Orders temporal precisions: none < clip < segment < word < frame."""
    return TEMPORAL_PRECISIONS.index(value)


class DimensionControl(ContractModel):
    control: Control
    temporal_precision: Precision = "none"
    vocabulary: str | list[str] | None = Field(
        default=None, description="any_text, or the closed labels the engine accepts natively"
    )
    knobs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _precision_matches_control(self) -> DimensionControl:
        if self.control in ("none", "emergent") and self.temporal_precision != "none":
            raise ValueError(f"control {self.control!r} cannot declare temporal precision {self.temporal_precision!r}")
        return self


class SegmentControl(ContractModel):
    supported: bool = False
    syntax: str | None = None


class ProsodyCoupling(ContractModel):
    lip_sync_from_audio: Strength = "none"
    expression_from_audio: Strength = "none"


class Continuity(ContractModel):
    first_frame_conditioning: bool = False
    chunk_continuation: bool = False
    reference_identity: Strength = "none"
    background_preservation: Strength = "none"


class ObjectInteraction(ContractModel):
    control: Control = "none"


class MultiPerson(ContractModel):
    max_persons: int = Field(default=1, ge=1)


class TemporalControl(ContractModel):
    max_clip_s: float = Field(default=10.0, gt=0)
    recommended_chunk_s: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _chunk_within_clip(self) -> TemporalControl:
        if self.recommended_chunk_s > self.max_clip_s:
            raise ValueError("recommended_chunk_s exceeds max_clip_s")
        return self


class BehaviorMatrix(ContractModel):
    """The flat YAML form (`emotion_visual: {...}`, `segment_control: {...}`) is accepted and split
    into requestable `dimensions` and the engine-property fields."""

    dimensions: dict[str, DimensionControl] = Field(default_factory=dict)
    segment_control: SegmentControl = Field(default_factory=SegmentControl)
    prosody_coupling: ProsodyCoupling = Field(default_factory=ProsodyCoupling)
    continuity: Continuity = Field(default_factory=Continuity)
    object_interaction: ObjectInteraction = Field(default_factory=ObjectInteraction)
    multi_person: MultiPerson = Field(default_factory=MultiPerson)
    temporal_control: TemporalControl = Field(default_factory=TemporalControl)

    @model_validator(mode="before")
    @classmethod
    def _split_flat_form(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "dimensions" in data:
            return data
        out: dict[str, Any] = {"dimensions": {}}
        for key, value in data.items():
            if key in ENGINE_PROPERTIES:
                out[key] = value
            else:
                out["dimensions"][key] = value
        return out

    def control(self, dimension: str) -> DimensionControl:
        """The declared control for a dimension; undeclared dimensions are `none`."""
        return self.dimensions.get(dimension, DimensionControl(control="none"))


class KnobSpec(ContractModel):
    """Maps an abstract knob (§15.7) to an engine parameter. Unused until calibrated."""

    param: str
    range: tuple[float, float]
    monotonic: Literal["true", "false", "unverified"] = "unverified"
    calibrated: bool = False

    @model_validator(mode="before")
    @classmethod
    def _yaml_booleans(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("monotonic"), bool):
            data = {**data, "monotonic": "true" if data["monotonic"] else "false"}
        return data


# ---------------------------------------------------------------------- directives (wire form)


class DirectiveRealization(ContractModel):
    item_ref: str
    dimension: str
    level: Literal["HONORED", "APPROXIMATED", "UNSUPPORTED"]
    method: str
    detail: str = ""


class DirectiveSubSpan(ContractModel):
    """A part of a shot or chunk with vocabulary descriptors and abstract knob values.
    `start_s`/`end_s` are relative to the start of the request's audio."""

    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    descriptors: list[str] = Field(default_factory=list)
    knobs: dict[str, float] = Field(default_factory=dict)
    item_refs: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(
        default_factory=dict, description="resolved vocabulary labels per dimension (emotion_visual: serious, …)"
    )
    descriptions: dict[str, str] = Field(
        default_factory=dict,
        description="model-agnostic text of each label (config/vocab/descriptions.yaml) for text-prompted translators; "
        "never engine syntax (I1)",
    )


class VisualDirectives(ContractModel):
    shot_key: str
    chunk: int | None = None
    character_key: str
    sub_spans: list[DirectiveSubSpan] = Field(default_factory=list)


class ProsodyDirectives(ContractModel):
    character_key: str
    segment_key: str
    strategy: str
    emotion: str | None = None
    emotion_intensity: float | None = None
    rate: float = 1.0
    energy: float = 0.5
    pitch_variation: float = 0.5
    emphasis_words: list[int] = Field(default_factory=list)
    pauses: list[dict[str, int]] = Field(default_factory=list, description="[{after_word, ms}]")
    nonverbal: list[dict[str, Any]] = Field(default_factory=list)
    delivery: str | None = None
    descriptions: dict[str, str] = Field(
        default_factory=dict,
        description="model-agnostic text of `emotion`, `strategy` and `delivery` (config/vocab/descriptions.yaml) for "
        "instruction-prompted voice translators",
    )


class EditorialDirective(ContractModel):
    kind: str
    item_ref: str
    start_s: float | None = None
    end_s: float | None = None
    detail: str = ""


class BehaviorDirectives(ContractModel):
    """What a `BehaviorTranslator` translates into engine syntax. Items the engine cannot express
    stay in `realizations` with their level; translators never drop them silently."""

    cbs_content_digest: str
    route_digest: str | None = None
    target_key: str
    realizations: list[DirectiveRealization] = Field(default_factory=list)
    prosody: list[ProsodyDirectives] = Field(default_factory=list)
    visual: list[VisualDirectives] = Field(default_factory=list)
    editorial: list[EditorialDirective] = Field(default_factory=list)
