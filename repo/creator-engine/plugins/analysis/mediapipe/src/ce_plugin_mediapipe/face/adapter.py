"""Face measurement with MediaPipe Tasks (VIDEO running mode, one face).

- head yaw/pitch/roll: Euler angles of the facial transformation matrix (degrees, 0 = frontal);
- gaze deviation: the angle between the gaze proxy and the camera axis, combining head pose with
  the eye-look blendshapes (eye-in-head rotation scaled by `eye_yaw_deg` / `eye_pitch_deg`);
- smile = mean(mouthSmileLeft/Right); brow_raise = max(browInnerUp, mean(browOuterUp*));
  brow_down = mean(browDown*); jaw_open; eye_closure = mean(eyeBlink*).
Frames without a face carry the previous value (or 0) and lower `face_detected_ratio`; judgement
reports NOT_MEASURABLE below its minimum ratio (§16.8). Events (blink, look_away, smile) are
measured runs for display; verdicts come from the series.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import FaceAnalyzer
from ce_contracts.models import FaceDetectResult, FaceLandmarksResult, MediaAnalysisRequest, TrackEventOut

from ce_plugin_mediapipe.frames import sample_frames

__all__ = ["MediaPipeFace", "euler_from_matrix", "face_series"]


def euler_from_matrix(matrix: np.ndarray) -> tuple[float, float, float]:
    """(yaw, pitch, roll) in degrees from a 4x4 (or 3x3) rotation matrix."""
    r = np.asarray(matrix, dtype=float)[:3, :3]
    sy = math.hypot(r[0, 0], r[1, 0])
    if sy > 1e-6:
        pitch = math.atan2(r[2, 1], r[2, 2])
        yaw = math.atan2(-r[2, 0], sy)
        roll = math.atan2(r[1, 0], r[0, 0])
    else:  # pragma: no cover - gimbal lock
        pitch, yaw, roll = math.atan2(-r[1, 2], r[1, 1]), math.atan2(-r[2, 0], sy), 0.0
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def _bs(scores: dict[str, float], *names: str) -> float:
    return float(np.mean([scores.get(n, 0.0) for n in names]))


def face_series(
    scores: dict[str, float], matrix: np.ndarray | None, eye_yaw: float, eye_pitch: float
) -> dict[str, float]:
    yaw, pitch, roll = euler_from_matrix(matrix) if matrix is not None else (0.0, 0.0, 0.0)
    # eye-in-head: positive = towards the subject's left / up (the sign does not matter for deviation)
    eye_h = (
        _bs(scores, "eyeLookOutLeft", "eyeLookInRight") - _bs(scores, "eyeLookInLeft", "eyeLookOutRight")
    ) * eye_yaw
    eye_v = (
        _bs(scores, "eyeLookUpLeft", "eyeLookUpRight") - _bs(scores, "eyeLookDownLeft", "eyeLookDownRight")
    ) * eye_pitch
    gaze = math.hypot(yaw + eye_h, pitch + eye_v)
    return {
        "gaze_deviation_deg": min(90.0, gaze),
        "smile": _bs(scores, "mouthSmileLeft", "mouthSmileRight"),
        "brow_raise": max(scores.get("browInnerUp", 0.0), _bs(scores, "browOuterUpLeft", "browOuterUpRight")),
        "brow_down": _bs(scores, "browDownLeft", "browDownRight"),
        "jaw_open": scores.get("jawOpen", 0.0),
        "eye_closure": _bs(scores, "eyeBlinkLeft", "eyeBlinkRight"),
        "head_yaw_deg": yaw,
        "head_pitch_deg": pitch,
        "head_roll_deg": roll,
    }


def _runs(values: list[float], hz: float, threshold: float, kind: str, min_s: float) -> list[TrackEventOut]:
    events: list[TrackEventOut] = []
    start: int | None = None
    for i, v in enumerate([*values, -math.inf]):
        if v >= threshold and start is None:
            start = i
        elif v < threshold and start is not None:
            if (i - start) / hz >= min_s:
                peak = max(values[start:i])
                events.append(
                    TrackEventOut(
                        type=kind,
                        start_s=round(start / hz, 3),
                        end_s=round(i / hz, 3),
                        value=round(peak, 3),
                        confidence=0.7,
                    )
                )
            start = None
    return events


class MediaPipeFace(FaceAnalyzer):
    seconds_per_unit = 0.15

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self.root = Path(".")
        self._lock = asyncio.Lock()

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        self.root = Path(ctx.model_cache_dir)
        for key in ("landmarker_file", "detector_file"):
            if not (self.root / str(self.config[key])).is_file():
                raise FileNotFoundError(f"{self.root / str(self.config[key])} is missing: run `make fetch-cpu-assets`")

    def _detect(self, path: Path, hz: float) -> list[dict[str, Any]]:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        sampled = sample_frames(path, hz, max_side=int(self.config["max_side"]))
        options = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(self.root / str(self.config["detector_file"]))),
            running_mode=vision.RunningMode.VIDEO,
            min_detection_confidence=0.5,
        )
        out: list[dict[str, Any]] = []
        with vision.FaceDetector.create_from_options(options) as detector:
            for i, frame in enumerate(sampled.frames):
                result = detector.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame)), round(i * 1000 / hz)
                )
                boxes = []
                for det in result.detections:
                    b = det.bounding_box
                    score = float(det.categories[0].score) if det.categories else 0.0
                    boxes.append(
                        [
                            round(b.origin_x / sampled.width, 4),
                            round(b.origin_y / sampled.height, 4),
                            round(b.width / sampled.width, 4),
                            round(b.height / sampled.height, 4),
                            round(score, 4),
                        ]
                    )
                out.append({"t_s": round(i / hz, 3), "boxes": boxes})
        return out

    def _landmarks(self, path: Path, hz: float) -> FaceLandmarksResult:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        sampled = sample_frames(path, hz, max_side=int(self.config["max_side"]))
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.root / str(self.config["landmarker_file"]))),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        names = ("gaze_deviation_deg", "smile", "brow_raise", "brow_down", "jaw_open", "eye_closure",
                 "head_yaw_deg", "head_pitch_deg", "head_roll_deg")  # fmt: skip
        series: dict[str, list[float]] = {n: [] for n in names}
        detected = 0
        last = dict.fromkeys(names, 0.0)
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            for i, frame in enumerate(sampled.frames):
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame)), round(i * 1000 / hz)
                )
                if result.face_blendshapes:
                    detected += 1
                    scores = {c.category_name: float(c.score) for c in result.face_blendshapes[0]}
                    matrix = (
                        np.asarray(result.facial_transformation_matrixes[0])
                        if result.facial_transformation_matrixes
                        else None
                    )
                    last = face_series(
                        scores, matrix, float(self.config["eye_yaw_deg"]), float(self.config["eye_pitch_deg"])
                    )
                for name in names:
                    series[name].append(round(float(last[name]), 4))
        n = len(sampled.frames)
        ratio = detected / n if n else 0.0
        events: list[TrackEventOut] = []
        if detected:
            events += _runs(series["eye_closure"], hz, float(self.config["blink_closure"]), "blink", 0.0)
            events += _runs(series["gaze_deviation_deg"], hz, float(self.config["look_away_deg"]), "look_away", 0.25)
            events += _runs(series["smile"], hz, 0.5, "smile", 0.2)
        return FaceLandmarksResult(
            sample_hz=hz,
            series=series if detected else {},
            events=sorted(events, key=lambda e: (e.start_s, e.type)),
            face_detected_ratio=round(min(1.0, ratio), 4),
        )

    async def run_face_detect(self, request: MediaAnalysisRequest, ctx: RunContext) -> FaceDetectResult:
        path = await ctx.read_artifact(request.media)
        async with self._lock:
            frames = await asyncio.to_thread(self._detect, path, request.sample_hz)
        return FaceDetectResult(frames=frames)

    async def run_face_landmarks(self, request: MediaAnalysisRequest, ctx: RunContext) -> FaceLandmarksResult:
        path = await ctx.read_artifact(request.media)
        async with self._lock:
            return await asyncio.to_thread(self._landmarks, path, request.sample_hz)
