"""Body measurement with MediaPipe Pose (lite) and Hand Landmarkers (VIDEO mode).

- shoulder_width_ratio: distance between the shoulders (pose 11, 12) over the take's median —
  leaning towards the camera makes it grow (the lean proxy, §16.2);
- gesture_energy: mean displacement of hand landmarks between samples (wrists 15/16 from the pose
  when no hand is detected), scaled to 0..1;
- head_motion_energy: nose (pose 0) displacement between samples, same scale.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import BodyAnalyzer
from ce_contracts.models import BodyLandmarksResult, MediaAnalysisRequest, TrackEventOut

from ce_plugin_mediapipe.frames import sample_frames

__all__ = ["MediaPipeBody"]


def _xy(landmarks: Any, index: int) -> np.ndarray:
    point = landmarks[index]
    return np.array([float(point.x), float(point.y)])


class MediaPipeBody(BodyAnalyzer):
    seconds_per_unit = 0.2

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self.root = Path(".")
        self._lock = asyncio.Lock()

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        self.root = Path(ctx.model_cache_dir)
        for key in ("pose_file", "hand_file"):
            if not (self.root / str(self.config[key])).is_file():
                raise FileNotFoundError(f"{self.root / str(self.config[key])} is missing: run `make fetch-cpu-assets`")

    def _measure(self, path: Path, hz: float) -> BodyLandmarksResult:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        sampled = sample_frames(path, hz, max_side=int(self.config["max_side"]))
        pose_options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.root / str(self.config["pose_file"]))),
            running_mode=vision.RunningMode.VIDEO,
        )
        hand_options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.root / str(self.config["hand_file"]))),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=2,
        )
        widths: list[float | None] = []
        hands: list[np.ndarray | None] = []
        noses: list[np.ndarray | None] = []
        detected = 0
        with (
            vision.PoseLandmarker.create_from_options(pose_options) as pose,
            vision.HandLandmarker.create_from_options(hand_options) as hand,
        ):
            for i, frame in enumerate(sampled.frames):
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame))
                ts = round(i * 1000 / hz)
                body = pose.detect_for_video(image, ts)
                found = hand.detect_for_video(image, ts)
                if body.pose_landmarks:
                    detected += 1
                    lm = body.pose_landmarks[0]
                    widths.append(float(np.linalg.norm(_xy(lm, 11) - _xy(lm, 12))))
                    noses.append(_xy(lm, 0))
                    wrists = np.stack([_xy(lm, 15), _xy(lm, 16)])
                else:
                    widths.append(None)
                    noses.append(None)
                    wrists = None
                if found.hand_landmarks:  # per-hand centroids, ordered left to right
                    centroids = np.array(
                        [[np.mean([p.x for p in h]), np.mean([p.y for p in h])] for h in found.hand_landmarks]
                    )
                    hands.append(centroids[np.argsort(centroids[:, 0])])
                else:
                    hands.append(wrists)
        n = len(sampled.frames)
        ratio = detected / n if n else 0.0
        if not detected:
            return BodyLandmarksResult(sample_hz=hz, body_detected_ratio=0.0)
        valid = [w for w in widths if w is not None]
        median = float(np.median(valid)) if valid else 1.0
        scale = float(self.config["gesture_scale"])
        shoulder: list[float] = []
        last = 1.0
        for w in widths:
            last = (w / median) if w is not None and median > 0 else last
            shoulder.append(round(last, 4))

        def energy(points: list[np.ndarray | None]) -> list[float]:
            out = [0.0]
            for prev, cur in itertools.pairwise(points):
                if prev is None or cur is None or prev.shape != cur.shape:
                    out.append(0.0)
                else:
                    out.append(round(float(min(1.0, np.linalg.norm(cur - prev, axis=-1).mean() * scale)), 4))
            return out[:n]

        gesture = energy(hands)
        head = energy(noses)
        events: list[TrackEventOut] = []
        start: int | None = None
        for i, v in enumerate([*gesture, 0.0]):
            if v >= 0.35 and start is None:
                start = i
            elif v < 0.35 and start is not None:
                events.append(
                    TrackEventOut(
                        type="gesture",
                        start_s=round(start / hz, 3),
                        end_s=round(i / hz, 3),
                        value=round(max(gesture[start:i]), 3),
                        confidence=0.6,
                    )
                )
                start = None
        return BodyLandmarksResult(
            sample_hz=hz,
            series={"shoulder_width_ratio": shoulder, "gesture_energy": gesture, "head_motion_energy": head},
            events=events,
            body_detected_ratio=round(min(1.0, ratio), 4),
        )

    async def run_body_landmarks(self, request: MediaAnalysisRequest, ctx: RunContext) -> BodyLandmarksResult:
        path = await ctx.read_artifact(request.media)
        async with self._lock:
            return await asyncio.to_thread(self._measure, path, request.sample_hz)
