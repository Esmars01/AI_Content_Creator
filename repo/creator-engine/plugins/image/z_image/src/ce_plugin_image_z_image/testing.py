"""CPU stand-in for Z-Image-Turbo's forward pass (manifest `test_backend`): a seeded PNG of the
planned generation size. Never used by a worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_plugin_kit.testing import fake_image

__all__ = ["FakeZImageBackend", "fake_backend"]


class FakeZImageBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, args: dict[str, Any], out: Path) -> Path:
        self.calls.append(dict(args))
        return fake_image(
            out, int(args["width"]), int(args["height"]), seed=int(args["seed"]), label=str(args["prompt"])
        )


def fake_backend(manifest: Any) -> FakeZImageBackend:
    return FakeZImageBackend()
