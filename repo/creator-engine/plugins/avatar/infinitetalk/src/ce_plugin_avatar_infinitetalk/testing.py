"""CPU stand-in for InfiniteTalk's forward pass (manifest `test_backend`, ADR 0051).

It returns what the real backend returns — an MP4 at 25 fps in the size bucket's resolution with
`max_frames` frames (4n+1) and the 16 kHz audio muxed — so the adapter's own code (parameters,
continuation, conforming, artifact bookkeeping) runs for real in the contract suite. Never used
by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_kit.media import probe
from ce_plugin_kit.testing import fake_video

from ce_plugin_avatar_infinitetalk.params import InfiniteTalkParams

__all__ = ["FakeInfiniteTalkBackend", "fake_backend"]

_BUCKET_SHORT_SIDE = {"infinitetalk-480": 480, "infinitetalk-720": 720}


class FakeInfiniteTalkBackend:
    def __init__(self) -> None:
        self.calls: list[InfiniteTalkParams] = []

    def generate(self, params: InfiniteTalkParams, image: Path, audio: Path, out_dir: Path) -> Path:
        self.calls.append(params)
        info = probe(image)
        short = _BUCKET_SHORT_SIDE.get(params.size_bucket, 480)
        w, h = (info.width or 480), (info.height or 832)
        scale = short / min(w, h)
        width, height = (round(w * scale / 16) * 16, round(h * scale / 16) * 16)
        return fake_video(
            out_dir / "infinitetalk_raw.mp4", width, height, params.fps, params.max_frames / params.fps, audio=audio
        )


def fake_backend(manifest: Any) -> FakeInfiniteTalkBackend:
    return FakeInfiniteTalkBackend()
