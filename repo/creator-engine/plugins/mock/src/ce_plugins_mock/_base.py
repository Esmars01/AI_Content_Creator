"""Shared base for the mock adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ce_contracts.common import ArtifactRef, LoadContext, RunContext
from ce_contracts.interfaces import AdapterBase

__all__ = ["MockAdapter", "env_float"]


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "")
    try:
        return float(value) if value else default
    except ValueError:
        return default


class MockAdapter(AdapterBase):
    """Defaults come from the manifest `defaults`, overridden by the load context config."""

    seconds_per_unit = 0.15

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)

    @property
    def model_key(self) -> str:
        model = self.manifest.primary_model
        return str(model.key) if model else str(self.manifest.id)

    def workdir(self, ctx: RunContext, name: str) -> Path:
        path = Path(ctx.scratch_dir) / f"{self.manifest.id}-{name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def write(self, ctx: RunContext, path: Path, kind: str, *, role: str, mime: str, **meta: Any) -> ArtifactRef:
        return await ctx.write_artifact(
            path, kind, {"mock": True, "adapter_id": self.manifest.id, **meta}, role=role, mime=mime
        )
