"""Temporal workers: `orchestrator` (workflows, CPU activities, GPU dispatch) and `render`."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Sequence
from typing import Any

from ce_config.settings import EffectiveConfig, startup_issues
from ce_exec.context import ExecServices, build_services
from ce_obs import get_logger
from ce_router.router import PRODUCTION_VALIDATIONS
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from ce_orchestrator.activities import Activities
from ce_orchestrator.models import BuildInput, TaskQueues
from ce_orchestrator.workflows import WORKFLOWS

__all__ = ["build_input", "connect", "make_worker", "queues", "refuse_if_misconfigured", "serve"]

_log = get_logger("ce.orchestrator")


def queues(effective: EffectiveConfig) -> TaskQueues:
    return TaskQueues.with_prefix(effective.settings.temporal_task_queue_prefix)


def build_input(
    effective: EffectiveConfig, *, org_id: str, job_id: str, version_id: str, preset_ids: Sequence[str] = ()
) -> BuildInput:
    cfg = effective.bundle.app.build
    return BuildInput(
        org_id=org_id,
        job_id=job_id,
        version_id=version_id,
        queues=queues(effective),
        max_parallel=cfg.max_parallel_nodes,
        preset_ids=list(preset_ids),
        model_timeout_s=cfg.model_node_timeout_s,
        cpu_timeout_s=cfg.cpu_node_timeout_s,
        render_timeout_s=cfg.render_node_timeout_s,
    )


def refuse_if_misconfigured(effective: EffectiveConfig, services: ExecServices | None = None) -> None:
    """§35 production checks and I11: production never runs mock provenance or mock adapters."""
    errors = [f"{i.code}: {i.message}" for i in startup_issues(effective) if i.severity == "error"]
    if services is not None and effective.settings.app_env == "prod":
        # The catalog carries the registry overlay (DB promotions, recorded validation, disabled ids),
        # so a sandbox watermarker that was never validated cannot satisfy I11 (Phase 8 adapters are
        # `sandbox` / `untested_on_gpu` until a smoke run promotes them).
        manifests = services.catalog.manifests
        for capability in ("provenance.watermark_video", "provenance.watermark_audio", "provenance.sign"):
            plugins = services.registry.by_capability(capability)
            for plugin in plugins:
                if plugin.manifest.mock:
                    errors.append(f"provenance: mock adapter {plugin.id} registered in production (I11)")
            usable = [
                p.id
                for p in plugins
                if not p.manifest.mock
                and p.id not in services.catalog.disabled
                and manifests.get(p.id, p.manifest).status == "production"
                and manifests.get(p.id, p.manifest).validation in PRODUCTION_VALIDATIONS
            ]
            if not usable:
                errors.append(
                    f"provenance: no adapter for {capability} is production-routable "
                    "(non-mock, status production, smoke- or bench-validated) (I11)"
                )
    if errors:
        raise RuntimeError("refusing to start: " + "; ".join(errors))


async def connect(effective: EffectiveConfig, *, attempts: int = 30) -> Client:
    settings = effective.settings
    for attempt in range(attempts):
        try:
            return await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace, data_converter=pydantic_data_converter
            )
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            _log.warning("temporal not reachable yet", error=str(exc)[:200])
            await asyncio.sleep(2.0)
    raise RuntimeError("unreachable")


def make_worker(
    client: Client, services: ExecServices, role: str, task_queues: TaskQueues, *, max_concurrent: int = 8
) -> Worker:
    activities = Activities(services)
    if role == "render":
        return Worker(
            client,
            task_queue=task_queues.render,
            activities=activities.render(),
            max_concurrent_activities=max(1, max_concurrent // 4),
        )
    return Worker(
        client,
        task_queue=task_queues.orchestrator,
        workflows=WORKFLOWS,
        activities=activities.orchestrator(),
        max_concurrent_activities=max_concurrent,
    )


async def serve(effective: EffectiveConfig, role: str) -> None:
    refuse_if_misconfigured(effective)
    services = build_services(effective)
    await services.sync_registry()  # manifests → `models`; promotions and calibrations → the catalog
    refuse_if_misconfigured(effective, services)  # after the sync: DB promotions count (I11)
    client = await connect(effective)
    worker = make_worker(client, services, role, queues(effective))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    _log.info("worker started", role=role, task_queue=worker.task_queue)
    run: Any = asyncio.create_task(worker.run())
    await stop.wait()
    await worker.shutdown()
    with contextlib.suppress(BaseException):
        await run
    await services.close()
