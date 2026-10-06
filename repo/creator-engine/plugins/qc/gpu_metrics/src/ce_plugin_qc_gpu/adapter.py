"""Adapters of the model-based QC metrics (`qc.lipsync`, `qc.vqa`, `qc.speech_quality`; §26).

They measure and report; thresholds and decisions live in `config/qc/*.yaml` and the Phase 11 QC
gate, so `passed` stays None (SyncNet is also `advisory`, §39.7: weights license unspecified).
Media a metric cannot read (no face, no speech, too short) return `passed: None` with the reason
in `metrics`, never a fabricated score of 0 that a gate could misread."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import extract_frames, probe, read_audio
from PIL import Image

from ce_plugin_qc_gpu.faces import crop_face, fill_gaps, median_filter

__all__ = ["SyncNetAdapter", "UTMOSAdapter", "UVQAdapter"]


def _unmeasurable(metric: str, reason: str, **extra: float) -> m.QCMetricResult:
    """`measurable: 0` plus a flag naming why (`unmeasurable_no_face: 1`); metrics hold numbers only."""
    flag = "unmeasurable_" + reason.replace(" ", "_")
    return m.QCMetricResult(
        metric=metric, score=0.0, metrics={"measurable": 0.0, flag: 1.0, **extra}, passed=None, advisory=True
    )


class SyncNetAdapter(EngineAdapter):
    """SyncNet AV-sync confidence and offset on the speaker's face track (25 fps, 16 kHz)."""

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_qc_gpu.backend import SyncNetBackend

        assert self.paths is not None
        return SyncNetBackend(
            weights=self.paths.model("syncnet-v2") / "syncnet_v2.model",
            landmarker=self.paths.dependency("face_landmarker") / "face_landmarker.task",
            code_dir=str(self.defaults.get("code_dir", "/opt/upstream/syncnet_python")),
        )

    async def run_qc_lipsync(self, request: m.QCMetricRequest, ctx: RunContext) -> m.QCMetricResult:
        backend = self.require_backend()
        d = self.defaults
        work = self.scratch(ctx, "syncnet")
        source = await ctx.read_artifact(request.media)
        info = probe(source)
        if not info.has_audio or not info.has_video:
            return _unmeasurable("lipsync", "no audio or video")
        frames = extract_frames(source, work / "frames", fps=25)
        detected = await self.call(ctx, backend.face_boxes, [str(p) for p in frames])
        ratio = sum(1 for b in detected if b is not None) / max(1, len(detected))
        filled = fill_gaps(detected)
        if filled is None or ratio < float(d.get("min_face_ratio", 0.5)):
            return _unmeasurable("lipsync", "no face", face_ratio=round(ratio, 4))
        kernel = int(d.get("smoothing_kernel", 13))
        size = median_filter([max(b[3] - b[1], b[2] - b[0]) / 2 for b in filled], kernel)
        cy = median_filter([(b[1] + b[3]) / 2 for b in filled], kernel)
        cx = median_filter([(b[0] + b[2]) / 2 for b in filled], kernel)
        crops = np.stack(
            [
                crop_face(
                    np.asarray(Image.open(p).convert("RGB")), cx[i], cy[i], size[i], float(d.get("crop_scale", 0.4))
                )
                for i, p in enumerate(frames)
            ]
        )
        audio = read_audio(source, 16_000)
        if len(audio) < 16_000 * 0.5 or len(crops) < 10:
            return _unmeasurable("lipsync", "too short", face_ratio=round(ratio, 4))
        offset, confidence, min_dist = await self.call(ctx, backend.evaluate, crops, audio, int(d.get("vshift", 15)))
        return m.QCMetricResult(
            metric="lipsync",
            score=round(float(confidence), 4),
            metrics={
                "measurable": 1.0,
                "confidence": round(float(confidence), 4),
                "offset_frames": float(offset),
                "min_dist": round(float(min_dist), 4),
                "face_ratio": round(ratio, 4),
            },
            passed=None,
            advisory=True,
        )


class UVQAdapter(EngineAdapter):
    """UVQ 1.5 no-reference video quality (MOS-like, 1–5), sampled at `fps` frames per second."""

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_qc_gpu.backend import UVQBackend

        return UVQBackend(code_dir=str(self.defaults.get("code_dir", "/opt/upstream/uvq")))

    async def run_qc_vqa(self, request: m.QCMetricRequest, ctx: RunContext) -> m.QCMetricResult:
        backend = self.require_backend()
        source = await ctx.read_artifact(request.media)
        info = probe(source)
        if not info.has_video or info.duration_s <= 0:
            return _unmeasurable("vqa", "no video")
        transpose = info.height > info.width  # upstream's portrait handling
        fps = int(self.defaults.get("fps", 1))
        result = await self.call(ctx, backend.score, str(source), max(1, math.ceil(info.duration_s)), transpose, fps)
        frames = np.asarray(result["per_frame_scores"], dtype=np.float64)
        score = float(result["uvq1p5_score"])
        return m.QCMetricResult(
            metric="vqa",
            score=round(score, 4),
            metrics={
                "measurable": 1.0,
                "uvq1p5": round(score, 4),
                "frames": float(len(frames)),
                "min_frame": round(float(frames.min()), 4) if len(frames) else 0.0,
                "p10_frame": round(float(np.percentile(frames, 10)), 4) if len(frames) else 0.0,
            },
            passed=None,
            advisory=False,
        )


class UTMOSAdapter(EngineAdapter):
    """UTMOSv2 predicted naturalness MOS (1–5) of speech; the media's audio at 16 kHz mono."""

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_qc_gpu.backend import UTMOSBackend

        assert self.paths is not None
        return UTMOSBackend(
            checkpoint=self.paths.model("utmosv2") / "fold0_s42_best_model.pth",
            ssl_dir=self.paths.dependency("ssl"),
            repetitions=int(self.defaults.get("num_repetitions", 1)),
        )

    async def run_qc_speech_quality(self, request: m.QCMetricRequest, ctx: RunContext) -> m.QCMetricResult:
        backend = self.require_backend()
        source = await ctx.read_artifact(request.media)
        if not probe(source).has_audio:
            return _unmeasurable("speech_quality", "no audio")
        samples = read_audio(source, 16_000)
        if (
            len(samples) < 16_000 * float(self.defaults.get("min_seconds", 1.0))
            or float(np.abs(samples).max(initial=0)) < 1e-3
        ):
            return _unmeasurable("speech_quality", "too short or silent")
        mos = float(await self.call(ctx, backend.predict, samples))
        return m.QCMetricResult(
            metric="speech_quality",
            score=round(mos, 4),
            metrics={"measurable": 1.0, "utmos": round(mos, 4)},
            passed=None,
        )
