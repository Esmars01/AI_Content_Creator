"""Shared contract types (§23): artifact references, hardware, estimates, health, run context.

This package must stay importable on Python 3.10 (GPU workers, §7): no `StrEnum`, no PEP 695
syntax, no `typing.Self`. It does not depend on `ce_core` (Python ≥ 3.12); the few closed sets
it shares with `ce_core` (artifact kinds, runtime families, statuses) are duplicated here and a
test keeps the two in sync.
"""

from __future__ import annotations

import asyncio
import enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ARTIFACT_KINDS",
    "PLUGIN_STATUSES",
    "RUNTIME_FAMILIES",
    "VALIDATIONS",
    "ArtifactRef",
    "CancellationToken",
    "Cancelled",
    "ContractModel",
    "Estimate",
    "HardwareInfo",
    "HealthStatus",
    "LoadContext",
    "RunContext",
    "StrEnumCompat",
]


class StrEnumCompat(str, enum.Enum):
    """`enum.StrEnum` for Python 3.10: members compare and format as their values."""

    def __str__(self) -> str:
        return str(self.value)


class ContractModel(BaseModel):
    """Base of every request, result and manifest model: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# Closed sets shared with ce_core.enums (kept in sync by tests/phase2/test_contracts_sync.py).
ARTIFACT_KINDS = (
    "video",
    "audio",
    "image",
    "captions",
    "alignment",
    "cbs",
    "compiled_behavior",
    "keyframe_state",
    "observed_behavior",
    "coverage_report",
    "plan_report",
    "screen_analysis",
    "world_fingerprints",
    "voice_conditioning",
    "logs",
    "other",
)
RUNTIME_FAMILIES = (
    "image",
    "wan",
    "tts",
    "asr",
    "audio",
    "lipsync",
    "vision",
    "post",
    "vllm",
    "cpu_model",
    "cpu_inproc",
)
PLUGIN_STATUSES = ("production", "sandbox", "experimental", "research_only", "disabled")
VALIDATIONS = ("untested_on_gpu", "smoke_passed", "bench_passed", "failed")


class ArtifactRef(ContractModel):
    """A content-addressed artifact. `artifact_id` is null for outputs a worker has uploaded but
    the scheduler has not registered yet; it fills the id in when the attempt completes."""

    artifact_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str
    mime: str = "application/octet-stream"
    bytes: int = Field(default=0, ge=0)
    role: str = Field(default="", description="what the output is to the caller (video, behavior_track, …)")
    meta: dict[str, Any] = Field(default_factory=dict)


class HardwareInfo(ContractModel):
    gpu_type: str = "none"
    gpu_count: int = 0
    vram_gb: float = 0.0
    free_vram_gb: float = 0.0
    cpu_count: int = 1
    ram_gb: float = 0.0


class Estimate(ContractModel):
    seconds: float = Field(ge=0)
    vram_gb: float = Field(default=0.0, ge=0)
    notes: str = ""


class HealthStatus(ContractModel):
    ok: bool
    detail: str = ""


class LoadContext(ContractModel):
    """What an adapter receives when it loads: where weights and scratch space live, and its config."""

    model_cache_dir: str
    scratch_dir: str
    app_env: str
    config: dict[str, Any] = Field(default_factory=dict)
    hardware: HardwareInfo = Field(default_factory=HardwareInfo)


class Cancelled(Exception):
    """Raised inside an adapter when its task was cancelled (user cancel, lease lost)."""


class CancellationToken:
    """Cooperative cancellation: the runtime sets it; adapters check it between steps."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled(self.reason or "cancelled")

    async def wait(self) -> None:
        await self._event.wait()


@runtime_checkable
class RunContext(Protocol):
    """What `Adapter.run` gets (§23). Implemented by the worker runtime (presigned URLs) and by the
    in-process runner for `cpu_inproc` adapters (direct storage access)."""

    seed: int
    scratch_dir: Path
    logger: Any
    cancel: CancellationToken

    async def read_artifact(self, ref: ArtifactRef) -> Path: ...

    async def write_artifact(
        self, path: Path, kind: str, meta: dict[str, Any] | None = None, *, role: str = "", mime: str = ""
    ) -> ArtifactRef: ...

    async def progress(self, fraction: float, message: str = "") -> None: ...
