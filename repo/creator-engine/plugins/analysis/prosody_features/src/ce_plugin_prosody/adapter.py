"""Prosody measurement (librosa).

- `pitch_hz`: pYIN f0 per 100 ms (median of voiced frames; 0 = unvoiced) and `voiced` ratio;
- `energy`: RMS per 100 ms (the series the audio judgement reads, §16.4);
- `speech_rate_wpm`: words over the span of the word timings; without timings, onsets over voiced
  time (a rough estimate, labelled `estimated` in the series names);
- pauses: gaps ≥ `min_pause_s` between timed words, or silence runs inside the speech.
"""

from __future__ import annotations

import asyncio
import itertools
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import AudioAnalyzer
from ce_contracts.models import AudioAnalysisRequest, ProsodyFeaturesResult, TrackEventOut

__all__ = ["ProsodyFeatures", "decode"]


def decode(path: Path, sample_rate: int) -> np.ndarray:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is not installed")
    raw = subprocess.run(  # noqa: S603 - fixed argv over a downloaded artifact
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
            str(sample_rate),
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=300,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


class ProsodyFeatures(AudioAnalyzer):
    seconds_per_unit = 0.1

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)

    def _measure(self, path: Path, request: AudioAnalysisRequest) -> ProsodyFeaturesResult:
        import librosa

        sr = int(self.config["sample_rate"])
        hz = float(self.config["series_hz"])
        y = decode(path, sr)
        hop = int(sr / hz)
        if y.size < hop:
            return ProsodyFeaturesResult(sample_hz=hz, series={"energy": []}, speech_rate_wpm=None, pauses=[])
        rms = librosa.feature.rms(y=y, frame_length=hop * 2, hop_length=hop, center=True)[0]
        f0, voiced, _ = librosa.pyin(
            y,
            fmin=float(self.config["fmin_hz"]),
            fmax=float(self.config["fmax_hz"]),
            sr=sr,
            frame_length=1024,
            hop_length=sr // 100,
        )
        per = max(1, round(100 / hz))  # pYIN runs at 100 frames/s: `per` frames per series sample
        n = len(rms)
        pitch: list[float] = []
        voiced_ratio: list[float] = []
        for i in range(n):
            window = f0[i * per : (i + 1) * per]
            flags = voiced[i * per : (i + 1) * per]
            values = window[np.isfinite(window)]
            pitch.append(round(float(np.median(values)), 2) if values.size else 0.0)
            voiced_ratio.append(round(float(np.mean(flags)) if flags.size else 0.0, 3))
        words = request.word_timings
        pauses: list[TrackEventOut] = []
        min_pause = float(self.config["min_pause_s"])
        wpm: float | None = None
        if len(words) >= 2:
            for a, b in itertools.pairwise(words):
                gap = b.start_s - a.end_s
                if gap >= min_pause:
                    pauses.append(
                        TrackEventOut(
                            type="pause", start_s=a.end_s, end_s=b.start_s, value=round(gap * 1000), confidence=0.9
                        )
                    )
            span = words[-1].end_s - words[0].start_s
            wpm = round(len(words) / span * 60.0, 1) if span > 0 else None
        else:
            db = librosa.amplitude_to_db(rms, ref=float(rms.max()) or 1.0)
            silent = db < float(self.config["silence_db"])
            speaking = np.flatnonzero(~silent)
            if speaking.size:
                first, last = int(speaking[0]), int(speaking[-1])
                start: int | None = None
                for i in range(first, last + 2):
                    if i <= last and silent[i]:
                        start = i if start is None else start
                    elif start is not None:
                        if (i - start) / hz >= min_pause:
                            pauses.append(
                                TrackEventOut(
                                    type="pause",
                                    start_s=round(start / hz, 3),
                                    end_s=round(i / hz, 3),
                                    value=round((i - start) / hz * 1000),
                                    confidence=0.6,
                                )
                            )
                        start = None
                onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
                voiced_s = float(np.sum(voiced)) / 100.0
                if voiced_s > 0.5 and len(onsets) >= 2:
                    wpm = round(len(onsets) / 1.5 / voiced_s * 60.0, 1)  # ~1.5 onsets per word [RV]
        return ProsodyFeaturesResult(
            sample_hz=hz,
            series={"energy": [round(float(x), 5) for x in rms], "pitch_hz": pitch, "voiced": voiced_ratio},
            speech_rate_wpm=wpm,
            pauses=pauses,
        )

    async def run_audio_prosody(self, request: AudioAnalysisRequest, ctx: RunContext) -> ProsodyFeaturesResult:
        path = await ctx.read_artifact(request.audio)
        return await asyncio.to_thread(self._measure, path, request)
