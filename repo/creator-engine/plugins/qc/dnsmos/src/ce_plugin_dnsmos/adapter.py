"""DNSMOS P.835: 16 kHz mono, 9.01 s windows (shorter audio is looped to the window, as in the
reference `dnsmos_local.py`), raw SIG/BAK/OVRL mapped with the non-personalized polynomials, averaged
over windows. Media without an audio stream returns score 0 with `passed: None` (not measurable)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import QCMetric
from ce_contracts.models import QCMetricRequest, QCMetricResult

__all__ = ["Dnsmos", "map_scores"]

SAMPLE_RATE = 16_000
P_SIG = np.poly1d([-0.08397278, 1.22083953, 0.0052439])
P_BAK = np.poly1d([-0.13166888, 1.60915514, -0.39604546])
P_OVR = np.poly1d([-0.06766283, 1.11546468, 0.04602535])


def map_scores(raw: np.ndarray) -> tuple[float, float, float]:
    sig, bak, ovr = (float(x) for x in raw)
    return float(P_SIG(sig)), float(P_BAK(bak)), float(P_OVR(ovr))


def _decode(path: Path) -> np.ndarray | None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is not installed")
    proc = subprocess.run(  # noqa: S603 - fixed argv over a downloaded artifact
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vn",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


class Dnsmos(QCMetric):
    seconds_per_unit = 0.05

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self._session: Any = None

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        model = Path(ctx.model_cache_dir) / str(self.config["model_file"])
        if not model.is_file():
            raise FileNotFoundError(f"{model} is missing: run `make fetch-cpu-assets`")

        def open_session() -> Any:
            import onnxruntime as ort

            return ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])

        self._session = await asyncio.to_thread(open_session)

    def score(self, audio: np.ndarray) -> dict[str, float]:
        length = int(float(self.config["window_s"]) * SAMPLE_RATE)
        if audio.size == 0:
            raise ValueError("empty audio")
        while audio.size < length:
            audio = np.concatenate([audio, audio])
        hop = int(float(self.config["hop_s"]) * SAMPLE_RATE)
        starts = list(range(0, max(1, audio.size - length + 1), hop)) or [0]
        name = self._session.get_inputs()[0].name
        results = []
        for start in starts:
            window = audio[start : start + length].astype(np.float32)[None]
            (raw,) = self._session.run(None, {name: window})
            results.append(map_scores(np.asarray(raw)[0]))
        sig, bak, ovr = (float(np.mean(col)) for col in zip(*results, strict=True))
        return {"sig_mos": round(sig, 4), "bak_mos": round(bak, 4), "ovrl_mos": round(ovr, 4)}

    async def run_qc_speech_quality(self, request: QCMetricRequest, ctx: RunContext) -> QCMetricResult:
        if self._session is None:
            raise RuntimeError("dnsmos is not loaded")
        audio = _decode(await ctx.read_artifact(request.media))
        if audio is None or audio.size < SAMPLE_RATE // 4:
            return QCMetricResult(
                metric="dnsmos_ovrl", score=0.0, metrics={"no_audio": 1.0}, passed=None, advisory=True
            )
        scores = await asyncio.to_thread(self.score, audio)
        ovrl = scores["ovrl_mos"]
        return QCMetricResult(
            metric="dnsmos_ovrl",
            score=ovrl,
            metrics=scores,
            passed=ovrl >= float(self.config["min_score"]),
            advisory=False,
        )
