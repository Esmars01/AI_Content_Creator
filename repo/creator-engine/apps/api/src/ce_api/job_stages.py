"""Where a job's GPU work is (production cutover §5): one honest stage per node and per job.

A model node's GPU task, the worker it waits for or runs on, and what that worker reported decide the
stage:

- `held` — the budget holds it (daily, project or video budget);
- `waiting_for_gpu` — queued, and no worker of its runtime family is up or on its way;
- `provisioning` — queued while an instance of its family is being rented;
- `booting` — the instance runs but its worker has not registered yet;
- `queued` — a worker of its family is up; it is next in line;
- `downloading_model`, `verifying`, `loading_model`, `generating`, `uploading` — what the worker said
  in its last heartbeat (`gpu_tasks.phase`); `running` when it said nothing more precise;
- `completed`, `failed`, `cancelled` — the node's outcome.

Nothing is inferred beyond those records: a stage the system cannot tell is never shown.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_db.models.assets import ExecutionNode, GpuTask
from ce_db.models.platform import GpuWorker, Plugin
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["STAGE_LABELS", "job_stage", "node_stages"]

STAGE_LABELS = {
    "queued": "Queued",
    "held": "Held by a budget",
    "waiting_for_gpu": "Waiting for a GPU",
    "provisioning": "Renting a GPU",
    "booting": "GPU booting",
    "downloading_model": "Downloading the model",
    "verifying": "Verifying the model",
    "loading_model": "Loading the model into GPU memory",
    "generating": "Generating",
    "uploading": "Encoding and uploading",
    "running": "Running",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
_PHASES = {
    "fetching_model": "downloading_model",
    "verifying_model": "verifying",
    "loading_model": "loading_model",
    "generating": "generating",
    "uploading": "uploading",
}
# what explains a wait first: the job's stage is its most blocking active node's
_ORDER = [
    "held", "waiting_for_gpu", "provisioning", "booting", "downloading_model", "verifying", "loading_model",
    "queued", "generating", "uploading", "running",
]  # fmt: skip
_NODE_DONE = {"succeeded": "completed", "cached": "completed", "skipped": "completed", "failed": "failed",
              "cancelled": "cancelled"}  # fmt: skip


async def node_stages(
    session: AsyncSession, org_id: UUID, nodes: Iterable[ExecutionNode]
) -> dict[UUID, dict[str, Any]]:
    nodes = list(nodes)
    if not nodes:
        return {}
    rows = (
        await session.execute(
            sa.select(GpuTask)
            .where(GpuTask.org_id == org_id, GpuTask.node_id.in_([n.id for n in nodes]))
            .order_by(GpuTask.created_at.desc())
        )
    ).scalars()
    latest: dict[UUID, GpuTask] = {}
    for row in rows:
        latest.setdefault(row.node_id, row)
    adapters = {str((t.constraints or {}).get("adapter_id") or "") for t in latest.values()} - {""}
    families: dict[str, str] = {}
    if adapters:
        families = dict(
            (
                await session.execute(
                    sa.select(Plugin.plugin_key, Plugin.runtime_family).where(Plugin.plugin_key.in_(adapters))
                )
            ).all()
        )
    fleet: dict[str, dict[str, int]] = {}
    for family, state, registered, provider_status in (
        await session.execute(
            sa.select(
                GpuWorker.runtime_family, GpuWorker.state, GpuWorker.registered_at, GpuWorker.provider_status
            ).where(GpuWorker.state.in_(("provisioning", "idle", "busy")))
        )
    ).all():
        counts = fleet.setdefault(str(family), {"provisioning": 0, "booting": 0, "up": 0})
        if state == "provisioning" and registered is None:
            booting = str((provider_status or {}).get("state") or "") == "running"
            counts["booting" if booting else "provisioning"] += 1
        else:
            counts["up"] += 1
    out: dict[UUID, dict[str, Any]] = {}
    for node in nodes:
        task = latest.get(node.id)
        if node.status in _NODE_DONE:
            out[node.id] = {"stage": _NODE_DONE[node.status]}
            continue
        if task is None:
            continue  # a CPU node, or one that has not reached the GPU queue
        detail: dict[str, Any] = {"task_state": task.state}
        family = families.get(str((task.constraints or {}).get("adapter_id") or ""))
        if family:
            detail["runtime_family"] = family
        if task.state == "queued":
            counts = fleet.get(family or "", {"provisioning": 0, "booting": 0, "up": 0})
            if task.held_reason:
                stage = "held"
                detail["held_reason"] = task.held_reason
            elif counts["up"]:
                stage = "queued"
            elif counts["booting"]:
                stage = "booting"
            elif counts["provisioning"]:
                stage = "provisioning"
            else:
                stage = "waiting_for_gpu"
        elif task.state in ("leased", "running"):
            stage = _PHASES.get(task.phase or "", "running")
            detail["worker_id"] = str(task.lease_worker_id) if task.lease_worker_id else None
            if task.progress is not None:
                detail["progress"] = task.progress
            if task.progress_message:
                detail["message"] = task.progress_message
            for key in ("bytes_done", "bytes_total", "speed_mbps", "eta_s"):
                value = (task.progress_detail or {}).get(key)
                if value is not None:
                    detail[key] = value
        else:
            stage = _NODE_DONE.get(task.state, "running") if task.state != "succeeded" else "completed"
        out[node.id] = {"stage": stage, "label": STAGE_LABELS.get(stage, stage), **detail}
    return out


def job_stage(status: str, stages: Iterable[dict[str, Any]]) -> str | None:
    """The job's stage: its outcome when it ended, else its most blocking active GPU node's; None when
    no GPU node is active (CPU work, planning: the job's status says enough)."""
    if status in ("succeeded", "partial"):
        return "completed"
    if status in ("failed", "cancelled"):
        return status
    active = [s["stage"] for s in stages if s.get("stage") in _ORDER]
    return min(active, key=_ORDER.index) if active else None
