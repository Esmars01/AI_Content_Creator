"""Workflow and activity payloads (ids and small summaries only, §7)."""

from __future__ import annotations

from typing import Any

from ce_exec.runtime import BeginResult, LocalInput, NodeInfo, NodeRef, PlanResult
from pydantic import BaseModel, Field

__all__ = [
    "AssetValidationInput",
    "BeginResult",
    "BuildInput",
    "BuildResult",
    "CompleteInput",
    "DeletionInput",
    "DispatchInput",
    "FailInput",
    "FinalizeInput",
    "LocalInput",
    "NodeInfo",
    "NodeRef",
    "PlanInput",
    "PlanResult",
    "PlanVideoInput",
    "PlanVideoResult",
    "PrevizCompleteInput",
    "SceneInput",
    "TaskQueues",
]


class TaskQueues(BaseModel):
    orchestrator: str = "orchestrator"
    render: str = "render"

    @classmethod
    def with_prefix(cls, prefix: str) -> TaskQueues:
        return cls(orchestrator=f"{prefix}orchestrator", render=f"{prefix}render")


class BuildInput(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    queues: TaskQueues = Field(default_factory=TaskQueues)
    max_parallel: int = 6
    preset_ids: list[str] = Field(default_factory=list, description="RenderWorkflow: extra presets")
    model_timeout_s: float = 7200
    cpu_timeout_s: float = 600
    render_timeout_s: float = 1800
    plan_timeout_s: float = 900  # PlanVideoWorkflow: every Director stage, LLM calls included
    qc_retries_per_node: int = 1  # exact-script loop budget, set from the plan (quality tier, §26)


class PlanInput(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    preset_ids: list[str] = Field(default_factory=list)
    previz: bool = False


class PlanVideoInput(BaseModel):
    org_id: str
    job_id: str


class PlanVideoResult(BaseModel):
    status: str
    version_id: str | None = None
    previz_job_id: str | None = None


class PrevizCompleteInput(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    statuses: dict[str, str]
    outputs: dict[str, str] = Field(default_factory=dict)
    cancelled: bool = False


class SceneInput(BaseModel):
    build: BuildInput
    graph_sha: str
    scene_key: str
    nodes: list[NodeInfo]
    upstream: dict[str, str]
    manifest: bool = True


class BuildResult(BaseModel):
    state: str
    job_status: str
    outputs: dict[str, str] = Field(default_factory=dict)
    statuses: dict[str, str] = Field(default_factory=dict)
    failed: list[str] = Field(default_factory=list)


class DispatchInput(BaseModel):
    ref: NodeRef
    begin: BeginResult


class FinalizeInput(BaseModel):
    ref: NodeRef
    begin: BeginResult
    worker: dict[str, Any]


class FailInput(BaseModel):
    ref: NodeRef
    node_id: str | None = None
    error: str
    status: str = "failed"


class CompleteInput(BaseModel):
    org_id: str
    job_id: str
    version_id: str
    statuses: dict[str, str]
    cancelled: bool = False
    render_only: bool = False


class FailJobInput(BaseModel):
    """A job whose workflow could not finish it: ends it as failed with the reason (audit PLAN-FAIL)."""

    org_id: str
    job_id: str
    code: str
    message: str


class AssetValidationInput(BaseModel):
    org_id: str
    job_id: str
    asset_id: str


class ScreenAnalysisInput(BaseModel):
    org_id: str
    job_id: str
    asset_id: str


class CalibrationInput(BaseModel):
    org_id: str
    job_id: str
    model_id: str


class DeletionInput(BaseModel):
    org_id: str
    job_id: str
    target_type: str
    target_id: str


class MemoryEnqueueInput(BaseModel):
    """`enqueue_memory_update`: a `memory_update` job row for a trigger (§18.4, Phase 12)."""

    org_id: str
    trigger: str
    target_type: str
    target_id: str
    version_id: str | None = None
    state: str | None = None  # the build's final state (trigger `ready`)


class EditJobInput(BaseModel):
    """`propose_edit` / `apply_edit` activities: the job row carries the rest (§28)."""

    org_id: str
    job_id: str


class EditJobResult(BaseModel):
    status: str
    edit_proposal_id: str | None = None
    version_id: str | None = None
    next_job_id: str | None = None
    next_kind: str | None = None  # generate | previz: the child workflow to run
