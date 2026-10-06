"""Request → InfiniteTalk generation parameters (pure, golden-tested).

Everything engine-specific about *how* a clip is generated lives here: the size bucket, the frame
count (4n+1 at 25 fps), clip vs streaming mode, step count and guidance scales with the distillation
LoRA, quantization, and the low-memory settings an OOM retry uses (§25).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["InfiniteTalkParams", "build_params", "frames_for"]


@dataclass(frozen=True)
class InfiniteTalkParams:
    size_bucket: str
    mode: str  # "clip" (one window) or "streaming" (windows chained by motion frames)
    frame_num: int  # frames per window, 4n+1
    max_frames: int  # frames for the whole request, 4n+1
    motion_frame: int
    fps: int
    steps: int
    shift: float
    text_guide_scale: float
    audio_guide_scale: float
    lora_scale: float
    quant: str | None
    num_persistent_param_in_dit: int | None
    t5_cpu: bool
    color_correction_strength: float
    seed: int
    prompt: str
    negative: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def frames_for(seconds: float, fps: int) -> int:
    """The smallest 4n+1 frame count covering `seconds` at `fps` (Wan's temporal VAE stride is 4)."""
    needed = max(1, math.ceil(seconds * fps))
    return ((needed - 1 + 3) // 4) * 4 + 1


def _bucket(height: int, width: int, buckets: dict[str, str]) -> str:
    """480p for outputs up to 480 px on the short side, 720p above (the engine follows the keyframe's aspect)."""
    short = min(height, width)
    keys = sorted(int(k) for k in buckets)
    for key in keys:
        if short <= key:
            return buckets[str(key)]
    return buckets[str(keys[-1])]


def build_params(
    *,
    width: int,
    height: int,
    audio_seconds: float,
    seed: int,
    prompt: str,
    defaults: dict[str, Any],
    engine: dict[str, Any] | None = None,
    low_memory: bool = False,
) -> InfiniteTalkParams:
    engine = engine or {}
    fps = int(defaults.get("fps", 25))
    clip = int(defaults.get("clip_frames", 81))
    total = frames_for(audio_seconds, fps)
    memory = dict(defaults.get("low_memory") or {}) if low_memory else {}
    persistent = memory.get("num_persistent_param_in_dit", defaults.get("num_persistent_param_in_dit"))
    audio_cfg = engine.get("audio_guide_scale")
    return InfiniteTalkParams(
        size_bucket=_bucket(height, width, dict(defaults.get("size_buckets") or {"480": "infinitetalk-480"})),
        mode="clip" if total <= clip else "streaming",
        frame_num=min(clip, total),
        max_frames=total,
        motion_frame=int(defaults.get("motion_frame", 9)),
        fps=fps,
        steps=int(defaults.get("steps", 4)),
        shift=float(defaults.get("shift", 2.0)),
        text_guide_scale=float(defaults.get("text_guide_scale", 1.0)),
        audio_guide_scale=float(audio_cfg if audio_cfg is not None else defaults.get("audio_guide_scale", 2.0)),
        lora_scale=float(defaults.get("lora_scale", 1.0)),
        quant=defaults.get("quant"),
        num_persistent_param_in_dit=int(persistent) if persistent is not None else None,
        t5_cpu=bool(memory.get("t5_cpu", False)),
        color_correction_strength=float(defaults.get("color_correction_strength", 1.0)),
        seed=int(seed) % (2**31),
        prompt=prompt,
        negative=str(defaults.get("negative", "")),
    )
