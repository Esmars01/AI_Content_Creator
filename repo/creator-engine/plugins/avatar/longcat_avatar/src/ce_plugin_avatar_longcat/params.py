"""Request → LongCat-Video-Avatar 1.5 generation parameters (pure, golden-tested): resolution bucket,
the number of 93-frame windows (13 conditioning frames each after the first) needed to cover the
audio, and the distilled sampling settings."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["LongCatParams", "build_params", "segments_for"]


@dataclass(frozen=True)
class LongCatParams:
    resolution: str  # "480p" | "720p"
    height: int
    width: int
    segments: int
    window_frames: int
    cond_frames: int
    fps: int
    steps: int
    text_guidance_scale: float
    audio_guidance_scale: float
    use_distill: bool
    use_int8: bool
    ref_img_index: int
    mask_frame_range: int
    seed: int
    prompt: str
    negative: str

    @property
    def total_frames(self) -> int:
        return self.window_frames + (self.segments - 1) * (self.window_frames - self.cond_frames)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total_frames": self.total_frames}


def segments_for(seconds: float, fps: int, window: int, cond: int) -> int:
    needed = max(1, math.ceil(seconds * fps))
    if needed <= window:
        return 1
    return 1 + math.ceil((needed - window) / (window - cond))


def build_params(
    *, width: int, height: int, audio_seconds: float, seed: int, prompt: str, defaults: dict[str, Any]
) -> LongCatParams:
    resolutions = {str(k): v for k, v in dict(defaults.get("resolutions") or {"480": [480, 832]}).items()}
    short_side = min(width, height)
    key = next((k for k in sorted(resolutions, key=int) if short_side <= int(k)), max(resolutions, key=int))
    short, long = (int(x) for x in resolutions[key])
    portrait = height >= width
    window, cond, fps = (
        int(defaults.get("window_frames", 93)),
        int(defaults.get("cond_frames", 13)),
        int(defaults.get("fps", 25)),
    )
    return LongCatParams(
        resolution=f"{key}p",
        height=long if portrait else short,
        width=short if portrait else long,
        segments=segments_for(audio_seconds, fps, window, cond),
        window_frames=window,
        cond_frames=cond,
        fps=fps,
        steps=int(defaults.get("steps", 8)),
        text_guidance_scale=float(defaults.get("text_guidance_scale", 1.0)),
        audio_guidance_scale=float(defaults.get("audio_guidance_scale", 1.0)),
        use_distill=bool(defaults.get("use_distill", True)),
        use_int8=bool(defaults.get("use_int8", True)),
        ref_img_index=int(defaults.get("ref_img_index", 10)),
        mask_frame_range=int(defaults.get("mask_frame_range", 3)),
        seed=int(seed) % (2**31),
        prompt=prompt,
        negative=str(defaults.get("negative", "")),
    )
