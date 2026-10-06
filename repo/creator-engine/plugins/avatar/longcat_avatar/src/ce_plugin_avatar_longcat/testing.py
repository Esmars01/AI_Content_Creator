"""CPU stand-in for LongCat-Video-Avatar's forward pass (manifest `test_backend`): an MP4 at 25 fps in
the bucket's resolution with every window's frames, so the adapter's own code runs for real in the
contract suite. Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_kit.testing import fake_video

from ce_plugin_avatar_longcat.params import LongCatParams

__all__ = ["FakeLongCatBackend", "fake_backend"]


class FakeLongCatBackend:
    def __init__(self) -> None:
        self.calls: list[LongCatParams] = []

    def generate(self, params: LongCatParams, image: Path, audio: Path, out_dir: Path) -> Path:
        self.calls.append(params)
        return fake_video(
            out_dir / "longcat_raw.mp4",
            params.width,
            params.height,
            params.fps,
            params.total_frames / params.fps,
            audio=audio,
        )


def fake_backend(manifest: Any) -> FakeLongCatBackend:
    return FakeLongCatBackend()
