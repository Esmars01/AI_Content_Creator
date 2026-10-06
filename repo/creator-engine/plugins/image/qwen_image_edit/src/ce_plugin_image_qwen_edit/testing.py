"""CPU stand-in for Qwen-Image-Edit's forward pass (manifest `test_backend`): a seeded PNG of the
planned size, recording how many images it was conditioned on. Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_kit.testing import fake_image

__all__ = ["FakeQwenEditBackend", "fake_backend"]


class FakeQwenEditBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], int]] = []

    def edit(self, args: dict[str, Any], images: list[Path], out: Path) -> Path:
        self.calls.append((dict(args), len(images)))
        return fake_image(
            out, int(args["width"]), int(args["height"]), seed=int(args["seed"]), label=str(args["prompt"])
        )


def fake_backend(manifest: Any) -> FakeQwenEditBackend:
    return FakeQwenEditBackend()
