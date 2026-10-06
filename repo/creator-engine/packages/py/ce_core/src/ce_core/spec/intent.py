"""Director Intent (§14): what a video or scene is trying to achieve. Values are vocabulary tokens."""

from __future__ import annotations

from pydantic import Field

from ce_core.scalars import Token, Unit
from ce_core.spec.base import SpecModel

__all__ = ["SceneIntent", "VideoIntent", "VideoIntentBlock"]


class VideoIntent(SpecModel):
    narrative_goal: Token | None = None
    audience_effect: Token | None = None
    persuasion_goal: Token | None = None
    information_goal: Token | None = None
    emotional_arc: list[Token] = Field(default_factory=list, description="ordered emotion labels (emotions.yaml)")
    attention_goal: Token | None = None
    cta_goal: Token | None = None


class VideoIntentBlock(SpecModel):
    video: VideoIntent = Field(default_factory=VideoIntent)


class SceneIntent(SpecModel):
    narrative_goal: Token | None = None
    emotional_goal: Token | None = None
    audience_effect: Token | None = None
    persuasion_goal: Token | None = None
    information_goal: Token | None = None
    attention_goal: Token | None = None
    reveal_strategy: Token | None = None
    tension_level: Unit | None = None
    curiosity_level: Unit | None = None
    performance_strategy: Token | None = None
    notes: str = Field(default="", description="free text; never drives control (I13)")
