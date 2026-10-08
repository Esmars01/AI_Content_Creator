"""The internal worker protocol (§25, `/internal/v1/worker/*`): request and response bodies.

Shared by the scheduler (server) and the worker runtime (client), so it stays importable on
Python 3.10 like the rest of `ce_worker`.
"""

from __future__ import annotations

from typing import Any

from ce_contracts.common import ContractModel, HardwareInfo
from pydantic import Field

__all__ = [
    "PROTOCOL_VERSION",
    "TASK_PHASES",
    "CompleteBody",
    "FailBody",
    "HeartbeatBody",
    "HeartbeatReply",
    "LeaseBody",
    "LeaseReply",
    "LeasedTask",
    "OutputDescriptor",
    "RegisterBody",
    "RegisterReply",
    "UploadBody",
    "UploadReply",
    "UploadSlot",
]

PROTOCOL_VERSION = "1"
ERROR_CLASSES = ("retryable", "fatal", "oom", "timeout", "cancelled")


class RegisterBody(ContractModel):
    name: str
    runtime_family: str
    adapters: list[str]
    hardware: HardwareInfo = Field(default_factory=HardwareInfo)
    provider: str | None = None
    external_id: str | None = None
    region: str | None = None
    gpu_type: str = "cpu"
    price_per_hour_usd: float = 0.0
    protocol: str = PROTOCOL_VERSION


class RegisterReply(ContractModel):
    worker_id: str
    token: str
    heartbeat_s: float
    lease_s: float
    long_poll_s: float
    adapters: list[str] = Field(description="the adapters the scheduler will lease to this worker")


class UploadSlot(ContractModel):
    key: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    method: str = "PUT"


class LeasedTask(ContractModel):
    task_id: str
    attempt_id: str
    capability: str
    adapter_id: str
    model_key: str
    seed: int
    request: dict[str, Any]
    inputs: dict[str, str] = Field(default_factory=dict, description="sha256 → presigned GET URL")
    uploads: list[UploadSlot] = Field(default_factory=list)
    lease_expires_at: str
    labels: dict[str, str] = Field(default_factory=dict)


class LeaseBody(ContractModel):
    adapters: list[str]
    resident_models: list[str] = Field(default_factory=list)
    cached_models: list[str] = Field(default_factory=list)
    free_vram_gb: float = 0.0
    max_tasks: int = Field(default=1, ge=1, le=16)
    wait_s: float | None = Field(default=None, ge=0, le=60)
    # Optional (older workers send neither): the worker's measured state, and per model key its
    # preparation state. Values it cannot measure are absent, never 0.
    telemetry: dict[str, Any] | None = None
    model_states: dict[str, dict[str, Any]] | None = None


class LeaseReply(ContractModel):
    tasks: list[LeasedTask] = Field(default_factory=list)


TASK_PHASES = ("fetching_model", "verifying_model", "loading_model", "generating", "uploading")


class HeartbeatBody(ContractModel):
    task_id: str
    progress: float = Field(default=0.0, ge=0, le=1)
    message: str = ""
    resident_models: list[str] = Field(default_factory=list)
    # Optional: what the task is doing (TASK_PHASES), its details (bytes done and total, speed, ETA
    # while fetching a model) and the worker's telemetry.
    phase: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    telemetry: dict[str, Any] | None = None
    model_states: dict[str, dict[str, Any]] | None = None


class HeartbeatReply(ContractModel):
    state: str
    cancel: bool = False
    lease_expires_at: str | None = None


class UploadBody(ContractModel):
    task_id: str
    count: int = Field(default=1, ge=1, le=32)


class UploadReply(ContractModel):
    uploads: list[UploadSlot]


class OutputDescriptor(ContractModel):
    slot_key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    mime: str
    kind: str
    role: str = ""


class CompleteBody(ContractModel):
    task_id: str
    result: dict[str, Any]
    outputs: list[OutputDescriptor] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    busy_seconds: float = Field(default=0.0, ge=0)


class FailBody(ContractModel):
    task_id: str
    error_class: str = Field(pattern=r"^(retryable|fatal|oom|timeout|cancelled)$")
    message: str = ""
    logs_uri: str | None = None
