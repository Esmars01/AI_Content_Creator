"""CPU stand-in for the Wan 2.2 forward passes (manifest `test_backend`): an MP4 of the planned
bucket size, frame count and rate. Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_kit.testing import fake_video

from ce_plugin_video_wan22.common import WanParams

__all__ = ["FakeWanBackend", "fake_backend"]


class FakeWanBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[WanParams, Path | None]] = []

    def generate(self, params: WanParams, image: Path | None, out: Path) -> Path:
        self.calls.append((params, image))
        return fake_video(out, params.width, params.height, params.fps, params.num_frames / params.fps)


def fake_backend(manifest: Any) -> FakeWanBackend:
    return FakeWanBackend()
