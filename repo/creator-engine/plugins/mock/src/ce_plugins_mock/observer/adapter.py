"""Mock observer (§16.7): the analyzers read `behavior_track.json` sidecars with noise.

Measurement only: tracks and events, never verdicts (judgement happens in `qc.shot` and
`behavior.coverage`, §16.3). Without a sidecar the mock sees nothing — `face_detected_ratio` is 0,
so the judgement reports NOT_MEASURABLE honestly. Embeddings are deterministic hashes.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path

import numpy as np
from ce_contracts.common import RunContext
from ce_contracts.interfaces import AudioAnalyzer, BodyAnalyzer, FaceAnalyzer, ImageEmbedder
from ce_contracts.models import (
    AudioAnalysisRequest,
    AudioEmotionResult,
    BodyLandmarksResult,
    EmbeddingResult,
    FaceDetectResult,
    FaceLandmarksResult,
    ImageEmbedRequest,
    MediaAnalysisRequest,
    ProsodyFeaturesResult,
    TrackEventOut,
)

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import read_wav, seed_int
from ce_plugins_mock.behavior_track import BehaviorTrack, TrackItem

__all__ = ["MockObserver", "hash_vector"]

GAZE_AWAY = ("look_away", "glance_away", "side_glance", "avoid_camera", "look_down", "glance")
SMILES = ("small_smile", "broad_smile", "smirk", "amused", "warm", "playful", "smile")
BROWS = ("eyebrow_raise", "surprised", "skeptical", "confused")
LEANS = ("lean_in", "seated_lean_in")


def hash_vector(basis: str, dim: int = 64) -> list[float]:
    rng = np.random.default_rng(int(hashlib.sha256(basis.encode()).hexdigest()[:8], 16))
    v = rng.normal(0, 1, dim)
    return [round(float(x), 6) for x in v / (np.linalg.norm(v) or 1.0)]


class MockObserver(MockAdapter, FaceAnalyzer, BodyAnalyzer, AudioAnalyzer, ImageEmbedder):
    seconds_per_unit = 0.02

    async def _track(self, request: MediaAnalysisRequest, ctx: RunContext) -> BehaviorTrack | None:
        for sidecar in request.sidecars:
            if sidecar.role == "behavior_track" or sidecar.kind == "other":
                data = json.loads((await ctx.read_artifact(sidecar)).read_text(encoding="utf-8"))
                return BehaviorTrack.model_validate(data)
        return None

    def _rng(self, request: MediaAnalysisRequest, salt: str) -> np.random.Generator:
        return np.random.default_rng(seed_int(request.media.sha256, salt))

    def _jitter(self, rng: np.random.Generator, item: TrackItem, duration: float) -> tuple[float, float]:
        noise = float(self.config.get("noise_ms", 60)) / 1000.0
        start = float(np.clip(item.start_s + rng.normal(0, noise), 0, duration))
        end = float(np.clip(item.end_s + rng.normal(0, noise), start, duration))
        return round(start, 3), round(end, 3)

    @staticmethod
    def _items(track: BehaviorTrack) -> list[TrackItem]:
        return [*track.performed, *track.emergent]

    async def run_face_detect(self, request: MediaAnalysisRequest, ctx: RunContext) -> FaceDetectResult:
        track = await self._track(request, ctx)
        if track is None:
            return FaceDetectResult(frames=[])
        n = max(1, math.ceil(track.duration_s * request.sample_hz))
        return FaceDetectResult(
            frames=[
                {"t_s": round(i / request.sample_hz, 3), "boxes": [[0.37, 0.29, 0.26, 0.34, 0.95]]} for i in range(n)
            ]
        )

    async def run_face_landmarks(self, request: MediaAnalysisRequest, ctx: RunContext) -> FaceLandmarksResult:
        track = await self._track(request, ctx)
        if track is None:
            return FaceLandmarksResult(sample_hz=request.sample_hz, face_detected_ratio=0.0)
        rng = self._rng(request, "face")
        level = float(self.config.get("noise_level", 0.05))
        hz, duration = request.sample_hz, track.duration_s
        n = max(1, math.ceil(duration * hz))
        t = np.arange(n) / hz
        gaze = 3.0 + rng.normal(0, 1.0, n)
        smile_base = 0.35 if (track.global_emotion or "") in SMILES else 0.1
        smile = smile_base + rng.normal(0, level, n)
        brow = 0.1 + rng.normal(0, level, n)
        closure = 0.05 + np.abs(rng.normal(0, level, n))
        energy = track.motion_energy
        yaw = np.cumsum(rng.normal(0, 0.6 * energy, n))
        pitch = np.cumsum(rng.normal(0, 0.4 * energy, n))
        events: list[TrackEventOut] = []
        for item in self._items(track):
            start, end = self._jitter(rng, item, duration)
            mask = (t >= start) & (t <= max(end, start + 1.0 / hz))
            label = item.label.split(":")[0]
            if item.dimension == "gaze" and label in GAZE_AWAY:
                gaze[mask] = 10.0 + 14.0 * item.intensity + rng.normal(0, 1.0, int(mask.sum()))
                events.append(
                    TrackEventOut(
                        type="look_away",
                        start_s=start,
                        end_s=end,
                        value=round(float(gaze[mask].max(initial=0)), 2),
                        confidence=0.85,
                    )
                )
            elif item.dimension == "blink":
                closure[mask] = 0.9
                events.append(TrackEventOut(type="blink", start_s=start, end_s=end, value=0.9, confidence=0.8))
            elif label in SMILES and item.dimension in ("facial_expression", "emotion_visual", "reaction"):
                smile[mask] = 0.3 + 0.5 * item.intensity
                events.append(
                    TrackEventOut(
                        type="smile",
                        start_s=start,
                        end_s=end,
                        value=round(0.3 + 0.5 * item.intensity, 3),
                        confidence=0.85,
                    )
                )
            elif label in BROWS and item.dimension in ("facial_expression", "emotion_visual"):
                brow[mask] = 0.3 + 0.5 * item.intensity
                events.append(
                    TrackEventOut(
                        type="brow_raise",
                        start_s=start,
                        end_s=end,
                        value=round(0.3 + 0.5 * item.intensity, 3),
                        confidence=0.7,
                    )
                )
            elif item.dimension == "head_motion" and label == "nod":
                pitch[mask] += 6.0 * np.sin(np.linspace(0, 4 * math.pi, int(mask.sum())))
                events.append(TrackEventOut(type="nod", start_s=start, end_s=end, value=6.0, confidence=0.6))
        clip = lambda a: [round(float(x), 4) for x in np.clip(a, 0.0, 1.0)]  # noqa: E731
        return FaceLandmarksResult(
            sample_hz=hz,
            series={
                "gaze_deviation_deg": [round(float(x), 3) for x in np.clip(gaze, 0, 90)],
                "smile": clip(smile),
                "brow_raise": clip(brow),
                "eye_closure": clip(closure),
                "head_yaw_deg": [round(float(x), 3) for x in yaw],
                "head_pitch_deg": [round(float(x), 3) for x in pitch],
            },
            events=sorted(events, key=lambda e: (e.start_s, e.type)),
            face_detected_ratio=1.0,
        )

    async def run_body_landmarks(self, request: MediaAnalysisRequest, ctx: RunContext) -> BodyLandmarksResult:
        track = await self._track(request, ctx)
        if track is None:
            return BodyLandmarksResult(sample_hz=request.sample_hz, body_detected_ratio=0.0)
        rng = self._rng(request, "body")
        hz, duration = request.sample_hz, track.duration_s
        n = max(1, math.ceil(duration * hz))
        t = np.arange(n) / hz
        gesture = 0.15 * track.motion_energy + np.abs(rng.normal(0, 0.03, n))
        shoulder = 1.0 + rng.normal(0, 0.01, n)
        events: list[TrackEventOut] = []
        for item in self._items(track):
            start, end = self._jitter(rng, item, duration)
            mask = (t >= start) & (t <= max(end, start + 1.0 / hz))
            if item.dimension == "gesture":
                gesture[mask] += 0.4 * item.intensity + 0.2
                events.append(
                    TrackEventOut(
                        type="gesture",
                        start_s=start,
                        end_s=end,
                        value=round(0.4 * item.intensity + 0.2, 3),
                        confidence=0.7,
                    )
                )
            elif item.dimension == "posture" and item.label.split(":")[0] in LEANS:
                shoulder[mask] += 0.06 + 0.06 * item.intensity
                events.append(
                    TrackEventOut(
                        type="lean_in",
                        start_s=start,
                        end_s=end,
                        value=round(0.06 + 0.06 * item.intensity, 3),
                        confidence=0.6,
                    )
                )
        return BodyLandmarksResult(
            sample_hz=hz,
            series={
                "gesture_energy": [round(float(x), 4) for x in np.clip(gesture, 0, 1)],
                "shoulder_width_ratio": [round(float(x), 4) for x in shoulder],
            },
            events=events,
            body_detected_ratio=1.0,
        )

    async def run_face_embed(self, request: MediaAnalysisRequest, ctx: RunContext) -> EmbeddingResult:
        basis = request.labels.get("identity") or request.media.sha256
        return EmbeddingResult(vectors=[hash_vector("face:" + basis)], dim=64)

    async def run_image_embed(self, request: ImageEmbedRequest, ctx: RunContext) -> EmbeddingResult:
        return EmbeddingResult(vectors=[hash_vector("image:" + ref.sha256) for ref in request.images], dim=64)

    async def run_voice_embed(self, request: AudioAnalysisRequest, ctx: RunContext) -> EmbeddingResult:
        basis = request.labels.get("identity") or request.audio.sha256
        return EmbeddingResult(vectors=[hash_vector("voice:" + basis)], dim=64)

    async def run_audio_prosody(self, request: AudioAnalysisRequest, ctx: RunContext) -> ProsodyFeaturesResult:
        """Energy from the samples; speech rate and pauses from the word timings."""
        path: Path = await ctx.read_artifact(request.audio)
        hz = 10.0
        try:
            samples, rate = read_wav(path)
            hop = int(rate / hz)
            frames = [samples[i : i + hop] for i in range(0, len(samples), hop)]
            energy = [round(float(np.sqrt(np.mean(f**2))) if len(f) else 0.0, 5) for f in frames]
        except Exception:
            energy = []
        words = request.word_timings
        pauses: list[TrackEventOut] = []
        for a, b in itertools.pairwise(words):
            gap = b.start_s - a.end_s
            if gap >= 0.2:
                pauses.append(
                    TrackEventOut(
                        type="pause", start_s=a.end_s, end_s=b.start_s, value=round(gap * 1000), confidence=0.9
                    )
                )
        wpm = None
        if len(words) >= 2:
            span = words[-1].end_s - words[0].start_s
            wpm = round(len(words) / span * 60.0, 1) if span > 0 else None
        return ProsodyFeaturesResult(sample_hz=hz, series={"energy": energy}, speech_rate_wpm=wpm, pauses=pauses)

    async def run_audio_emotion(self, request: AudioAnalysisRequest, ctx: RunContext) -> AudioEmotionResult:
        rng = np.random.default_rng(seed_int(request.audio.sha256, "emotion"))
        raw = rng.dirichlet(np.ones(4))
        return AudioEmotionResult(
            classes={k: round(float(v), 4) for k, v in zip(("neutral", "happy", "sad", "angry"), raw, strict=True)}
        )
