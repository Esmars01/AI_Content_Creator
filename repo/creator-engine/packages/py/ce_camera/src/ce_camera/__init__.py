"""Camera simulation (§22): seeded procedural motion, autofocus hunts, exposure drift (`motion`) and
subject-aware reframing with One-Euro smoothing, crop windows and crop loss (`reframe`).

Status: implemented and tested in Phase 7. `ce_render.video.camera_post` applies them with FFmpeg.
"""

from ce_camera.motion import CameraMotion, FocusHunt, Wave, exposure_expression, focus_hunts, motion_for
from ce_camera.reframe import (
    CropPlan,
    OneEuro,
    SubjectTrack,
    crop_loss,
    piecewise_expression,
    plan_crop,
    smooth_track,
    track_from_detections,
)

__all__ = [
    "CameraMotion",
    "CropPlan",
    "FocusHunt",
    "OneEuro",
    "SubjectTrack",
    "Wave",
    "crop_loss",
    "exposure_expression",
    "focus_hunts",
    "motion_for",
    "piecewise_expression",
    "plan_crop",
    "smooth_track",
    "track_from_detections",
]

__version__ = "0.7.0"
IMPLEMENTED_IN_PHASE = 7
