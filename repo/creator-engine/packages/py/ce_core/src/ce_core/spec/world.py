"""Scene world binding and non-mutating overrides (§19.3, I6)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from ce_core.enums import TimeOfDay, Weather
from ce_core.keys import CameraPositionKey, ElementKey, ShotKey
from ce_core.scalars import NonEmptyStr, Position3, Token
from ce_core.spec.base import SpecModel

__all__ = [
    "AcousticsOverride",
    "AddedElement",
    "AssetContinuityRef",
    "ContinuityRef",
    "LightingOverride",
    "MovedElement",
    "PlateContinuityRef",
    "ShotContinuityRef",
    "WorldBinding",
    "WorldOverrides",
]

SceneLocalElementKey = Annotated[str, StringConstraints(pattern=r"^el_x_[a-z0-9]+(?:_[a-z0-9]+)*$", max_length=64)]


class AddedElement(SpecModel):
    """A scene-local element (key prefixed `el_x_`); it never enters World DNA."""

    key: SceneLocalElementKey
    kind: Token
    label: NonEmptyStr
    description: str = ""
    position: Position3


class MovedElement(SpecModel):
    key: ElementKey
    position: Position3


class LightingOverride(SpecModel):
    """Lighting deltas. They must stay within the world tolerance unless `deviation_declared` (previz flags it)."""

    color_temp_k_delta: float = 0.0
    luminance_delta: float = 0.0
    key_azimuth_deg_delta: float = 0.0
    deviation_declared: bool = False


class AcousticsOverride(SpecModel):
    ambient_additions: list[Token] = Field(default_factory=list)


class WorldOverrides(SpecModel):
    element_states: dict[ElementKey, Token] = Field(default_factory=dict)
    hide_elements: list[ElementKey] = Field(default_factory=list)
    add_elements: list[AddedElement] = Field(default_factory=list)
    move_elements: list[MovedElement] = Field(default_factory=list)
    lighting: LightingOverride | None = None
    acoustics: AcousticsOverride | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.element_states
            or self.hide_elements
            or self.add_elements
            or self.move_elements
            or self.lighting
            or self.acoustics
        )


class PlateContinuityRef(SpecModel):
    kind: Literal["world_plate"] = "world_plate"
    camera_position_key: CameraPositionKey


class ShotContinuityRef(SpecModel):
    """Match a previous video's look. Enters cache keys as the referenced artifact's hash, never as ids (§12.2)."""

    kind: Literal["shot"] = "shot"
    video_id: UUID
    version_id: UUID
    shot_key: ShotKey


class AssetContinuityRef(SpecModel):
    kind: Literal["asset"] = "asset"
    asset_id: UUID


ContinuityRef = Annotated[PlateContinuityRef | ShotContinuityRef | AssetContinuityRef, Field(discriminator="kind")]


class WorldBinding(SpecModel):
    world_version_id: UUID
    camera_position_key: CameraPositionKey
    time_of_day: TimeOfDay
    weather: Weather = Weather.CLEAR
    overrides: WorldOverrides = Field(default_factory=WorldOverrides)
    continuity_ref: ContinuityRef | None = None

    @model_validator(mode="after")
    def _no_conflicting_overrides(self) -> WorldBinding:
        hidden = set(self.overrides.hide_elements)
        moved = {m.key for m in self.overrides.move_elements}
        stated = set(self.overrides.element_states)
        if hidden & (moved | stated):
            raise ValueError(f"elements both hidden and changed: {sorted(hidden & (moved | stated))}")
        return self
