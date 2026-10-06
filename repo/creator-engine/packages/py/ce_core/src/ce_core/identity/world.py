"""World DNA (§19.1): an independent, versioned recurring environment (ADR 0021).

Approved plates live in `world_versions.plates`, never inside the DNA. Overrides in scenes
never mutate it (I6). `validate_world_dna` checks vocabulary tokens and internal references.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from ce_core.enums import CameraPositionStatus, ElementMutability, TimeOfDay, Weather
from ce_core.errors import Issue
from ce_core.keys import CameraPositionKey, ElementKey, ZoneKey
from ce_core.scalars import NonEmptyStr, Position3, Token, Unit
from ce_core.spec.base import SpecModel
from ce_core.vocab import Vocabulary

__all__ = [
    "CameraPosition",
    "LightingSpec",
    "WorldDNA",
    "WorldElement",
    "Zone",
    "behavior_digest_fields",
    "validate_world_dna",
]

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class Geometry(SpecModel):
    dimensions_m: tuple[float, float, float] = Field(description="width, depth, height in metres")
    ceiling_height_m: float | None = None
    shape: str = "rectangular"
    layout: str = ""


class Zone(SpecModel):
    key: ZoneKey
    label: NonEmptyStr
    position: Position3
    allowed_postures: list[Token] = Field(min_length=1)


class WorldElement(SpecModel):
    key: ElementKey
    kind: Token
    label: str = ""
    description: str = ""
    position: Position3
    orientation_deg: float | None = None
    size_m: tuple[float, float, float] | None = None
    material: str = ""
    color: str = ""
    signature: bool = False
    mutability: ElementMutability = ElementMutability.FIXED
    states: list[Token] = Field(default_factory=list)
    default_state: Token | None = None

    @model_validator(mode="after")
    def _stateful_needs_default(self) -> WorldElement:
        if self.mutability == ElementMutability.STATEFUL:
            if not self.states:
                raise ValueError(f"element {self.key}: stateful elements need states")
            if self.default_state is None:
                raise ValueError(f"element {self.key}: default_state is required when stateful")
        if self.default_state is not None and self.default_state not in self.states:
            raise ValueError(f"element {self.key}: default_state {self.default_state!r} is not one of its states")
        return self


class BackgroundLayout(SpecModel):
    visible_elements: list[ElementKey] = Field(default_factory=list, description="in depth order")
    composition: str = ""


class KeyLight(SpecModel):
    azimuth_deg: float = Field(ge=-180, le=180)
    elevation_deg: float = Field(ge=-90, le=90)
    intensity: Unit
    color_temp_k: float = Field(ge=1000, le=12000)


class Practical(SpecModel):
    element: ElementKey
    color_temp_k: float = Field(ge=1000, le=12000)


class WindowLight(SpecModel):
    direction_deg: float | None = None
    strength_by_time: dict[TimeOfDay, Unit] = Field(default_factory=dict)


class LightingTolerance(SpecModel):
    color_temp_k: float = Field(default=400, ge=0)
    luminance: Unit = 0.12


class LightingSpec(SpecModel):
    key: KeyLight
    fill_ratio: Unit = 0.5
    practicals: list[Practical] = Field(default_factory=list)
    window_light: WindowLight | None = None
    contrast: Unit | None = None
    tolerance: LightingTolerance = Field(default_factory=LightingTolerance)


class TimeAndWeather(SpecModel):
    default_time_of_day: TimeOfDay
    allowed_times: list[TimeOfDay] = Field(min_length=1)
    default_weather: Weather = Weather.CLEAR
    allowed_weather: list[Weather] = Field(default_factory=lambda: [Weather.CLEAR])
    window_visible: bool = True

    @model_validator(mode="after")
    def _defaults_allowed(self) -> TimeAndWeather:
        if self.default_time_of_day not in self.allowed_times:
            raise ValueError("default_time_of_day must be one of allowed_times")
        if self.default_weather not in self.allowed_weather:
            raise ValueError("default_weather must be one of allowed_weather")
        return self


class WorldAcoustics(SpecModel):
    room_profile: Token
    rt60_s: float = Field(gt=0, le=5)
    ambient: list[Token] = Field(default_factory=list)
    noise_floor_db: float = Field(default=-60, le=0)


class CameraPosition(SpecModel):
    key: CameraPositionKey
    position: Position3 | None = None
    target: Position3 | None = None
    height_m: float = Field(gt=0, le=10)
    distance_m: float = Field(gt=0, le=50)
    lens_equiv_mm: float = Field(gt=5, le=400)
    default_framing: Token
    allowed_camera_profiles: list[Token] = Field(min_length=1)
    status: CameraPositionStatus = CameraPositionStatus.PERMITTED


class Continuity(SpecModel):
    must_show_from: dict[CameraPositionKey, list[ElementKey]] = Field(default_factory=dict)
    position_tolerance_m: float = Field(default=0.15, ge=0)
    forbidden_views: list[str] = Field(default_factory=list)


class WorldReference(SpecModel):
    asset_id: UUID
    role: Annotated[str, StringConstraints(pattern=r"^(establishing|position:cam_[a-z0-9_]+|element:el_[a-z0-9_]+)$")]


class WorldDNA(SpecModel):
    vocab_version: NonEmptyStr
    name: NonEmptyStr
    kind: Token
    style_tags: list[str] = Field(default_factory=list)
    palette: list[HexColor] = Field(default_factory=list)
    geometry: Geometry
    zones: list[Zone] = Field(default_factory=list)
    elements: list[WorldElement] = Field(default_factory=list)
    background_layouts: dict[CameraPositionKey, BackgroundLayout] = Field(default_factory=dict)
    lighting: LightingSpec
    time_and_weather: TimeAndWeather
    acoustics: WorldAcoustics
    camera_positions: list[CameraPosition] = Field(min_length=1)
    continuity: Continuity = Field(default_factory=Continuity)
    references: list[WorldReference] = Field(default_factory=list)
    fingerprints_artifact_id: UUID | None = Field(default=None, description="computed; set when plates are chosen")

    @model_validator(mode="after")
    def _internal_references(self) -> WorldDNA:
        keys = [z.key for z in self.zones] + [e.key for e in self.elements] + [c.key for c in self.camera_positions]
        duplicates = sorted(k for k, n in Counter(keys).items() if n > 1)
        if duplicates:
            raise ValueError(f"duplicate world keys: {duplicates}")
        elements = {e.key for e in self.elements}
        positions = {c.key for c in self.camera_positions}
        problems: list[str] = []
        for practical in self.lighting.practicals:
            if practical.element not in elements:
                problems.append(f"practical light {practical.element} is not an element")
        for cam, layout in self.background_layouts.items():
            if cam not in positions:
                problems.append(f"background layout for unknown camera position {cam}")
            problems += [f"layout {cam}: unknown element {el}" for el in layout.visible_elements if el not in elements]
        for cam, required in self.continuity.must_show_from.items():
            if cam not in positions:
                problems.append(f"continuity.must_show_from: unknown camera position {cam}")
            problems += [f"must_show_from {cam}: unknown element {el}" for el in required if el not in elements]
        for ref in self.references:
            target = ref.role.split(":", 1)[1] if ":" in ref.role else None
            if ref.role.startswith("position:") and target not in positions:
                problems.append(f"reference role {ref.role}: unknown camera position")
            if ref.role.startswith("element:") and target not in elements:
                problems.append(f"reference role {ref.role}: unknown element")
        if not any(c.status == CameraPositionStatus.PERMITTED for c in self.camera_positions):
            problems.append("at least one camera position must be permitted")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ lookups
    def element(self, key: str) -> WorldElement | None:
        return next((e for e in self.elements if e.key == key), None)

    def zone(self, key: str) -> Zone | None:
        return next((z for z in self.zones if z.key == key), None)

    def camera_position(self, key: str) -> CameraPosition | None:
        return next((c for c in self.camera_positions if c.key == key), None)


def behavior_digest_fields(dna: WorldDNA) -> dict[str, object]:
    """The behavior-relevant World DNA subset (§15.6): element keys, kinds, attention targets,
    seating and allowed postures, hand space. Lighting and acoustics are excluded so they never
    invalidate behavior."""
    elements = [{"key": e.key, "kind": e.kind} for e in sorted(dna.elements, key=lambda e: e.key)]
    zones = [
        {"key": z.key, "allowed_postures": sorted(z.allowed_postures)} for z in sorted(dna.zones, key=lambda z: z.key)
    ]
    return {"elements": elements, "zones": zones}


def validate_world_dna(dna: WorldDNA, vocab: Vocabulary) -> list[Issue]:
    issues: list[Issue] = []
    if dna.vocab_version != vocab.version:
        issues.append(
            Issue("vocab_version", f"World DNA validated against {dna.vocab_version}, current is {vocab.version}")
        )
    issues += vocab.check("world_kind", dna.kind, path="/kind")
    for zone in dna.zones:
        for posture in zone.allowed_postures:
            issues += vocab.check("strategy.posture", posture, path=f"/zones[{zone.key}]/allowed_postures")
    for element in dna.elements:
        issues += vocab.check("element_kind", element.kind, path=f"/elements[{element.key}]/kind")
        for state in element.states:
            issues += vocab.check("element_state", state, path=f"/elements[{element.key}]/states")
    for ambient in dna.acoustics.ambient:
        issues += vocab.check("ambient_profile", ambient, path="/acoustics/ambient")
    for cam in dna.camera_positions:
        issues += vocab.check(
            "camera.framing", cam.default_framing, path=f"/camera_positions[{cam.key}]/default_framing"
        )
    return issues
