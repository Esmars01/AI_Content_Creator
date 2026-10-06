"""`behavior_track.json`: ground truth of what a mock avatar engine performed (§16.7).

The mock engines write it next to each clip, following their declared behavior matrix and the
compiled directives, with configurable failure injection. The mock observer reads it with noise,
so the requested → compiled → observed triad runs end to end on CPU. Real engines never write it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SCHEMA", "BehaviorTrack", "TrackItem"]

SCHEMA = "ce.mock.behavior_track/1"


class TrackItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    label: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    intensity: float = Field(default=0.5, ge=0, le=1)
    item_ref: str | None = None
    source: Literal["native", "text", "keyframe", "audio_coupled", "global_prompt", "emergent"] = "native"


class BehaviorTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: str = Field(default=SCHEMA, alias="schema")
    adapter_id: str
    character_key: str | None = None
    duration_s: float = Field(ge=0)
    fps: float
    seed: int
    global_emotion: str | None = None
    global_intensity: float | None = None
    motion_energy: float = Field(default=0.5, ge=0)
    performed: list[TrackItem] = Field(default_factory=list)
    missed: list[dict[str, str]] = Field(default_factory=list, description="[{item_ref, dimension, reason}]")
    emergent: list[TrackItem] = Field(default_factory=list)
