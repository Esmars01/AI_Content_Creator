"""Realism post (§22): picture (`video`: LUT, white-balance drift, grain, vignette, lens distortion,
platform-like compression) and sound (`audio`: mic EQ and compression, world room acoustics,
gentle de-essing, room tone and ambient beds).

Status: implemented and tested in Phase 7.
"""

from ce_realism.audio import ambient_bed, biquad, deess, mic_chain, process_dialogue, room
from ce_realism.video import RealismPlan, realism_plan

__all__ = ["RealismPlan", "ambient_bed", "biquad", "deess", "mic_chain", "process_dialogue", "realism_plan", "room"]

__version__ = "0.7.0"
IMPLEMENTED_IN_PHASE = 7
