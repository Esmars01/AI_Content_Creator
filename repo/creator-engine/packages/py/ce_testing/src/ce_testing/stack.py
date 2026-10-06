"""The whole execution stack in one process, for end-to-end tests on the Compose infrastructure.

- the scheduler app (served in-process; workers reach it through an ASGI transport);
- one or more `worker-cpu` runtimes with the mock adapters (killable mid-task);
- the orchestrator and render Temporal workers on a task-queue prefix unique to the stack;
- Postgres (a migrated, seeded test database), Valkey, Temporal and SeaweedFS from Compose, with
  buckets unique to the stack.

Presigned URLs point at SeaweedFS, so artifact I/O is the production path.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from ce_config.settings import EffectiveConfig, load_effective

__all__ = ["Stack", "drop_s3_buckets", "stack_env"]

ROOT = Path(__file__).resolve().parents[5]


def stack_env(database_url: str, **extra: str) -> dict[str, str]:
    tag = secrets.token_hex(4)
    return {
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        "TEMPORAL_TASK_QUEUE_PREFIX": f"e2e-{tag}-",
        "SECRET_KEY": "test-secret-key-for-signing-only",
        "STORAGE_PROVIDER": "s3",
        "S3_ENDPOINT_URL": os.environ.get("S3_ENDPOINT_URL", "http://localhost:8333"),
        "S3_ACCESS_KEY_ID": os.environ.get("S3_ACCESS_KEY_ID", ""),
        "S3_SECRET_ACCESS_KEY": os.environ.get("S3_SECRET_ACCESS_KEY", ""),
        "S3_BUCKET_ASSETS": f"ce-e2e-{tag}-assets",
        "S3_BUCKET_ARTIFACTS": f"ce-e2e-{tag}-artifacts",
        "MOCK_GPU": "true",
        # Mocks by default (deterministic suites); real-engine tests pass CPU_REAL_ENGINES=auto
        # and MODEL_CACHE_DIR explicitly (`real_engine_env`).
        "CPU_REAL_ENGINES": "off",
        "MODEL_CACHE_DIR": str(ROOT / ".data" / "test-model-cache"),
        "PROVENANCE_MODE": "",
        "COOKIE_SECURE": "",
        **extra,
    }


REAL_MODEL_CACHE = ROOT / ".cache" / "models"


def real_engine_env() -> dict[str, str]:
    """Extra `stack_env` settings for tests that run the real CPU engines (they skip without assets)."""
    return {"CPU_REAL_ENGINES": "auto", "MODEL_CACHE_DIR": str(REAL_MODEL_CACHE)}


def drop_s3_buckets(settings: Any, names: list[str] | None = None, *, prefix: str | None = None) -> list[str]:
    """Empties and deletes test buckets on the S3 endpoint (SeaweedFS frees their volumes; every
    bucket is a collection that holds volumes, so leaked test buckets eventually exhaust the
    server, DECISIONS D23). Test plumbing only: production code never deletes buckets."""
    import boto3

    client: Any = boto3.client(  # untyped on purpose: test plumbing over boto3's stubs
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value() or None,
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value() or None,
        region_name=settings.s3_region or "us-east-1",
    )
    targets = list(names or [])
    if prefix:
        targets += [b["Name"] for b in client.list_buckets().get("Buckets", []) if b["Name"].startswith(prefix)]
    dropped = []
    for bucket in dict.fromkeys(targets):
        for attempt in range(5):  # late writers (a finishing upload) can refill a bucket once
            try:
                for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                    keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if keys:
                        client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
                client.delete_bucket(Bucket=bucket)
                dropped.append(bucket)
                break
            except client.exceptions.NoSuchBucket:
                break
            except Exception as exc:
                if attempt == 4:
                    print(f"could not drop test bucket {bucket}: {exc}", file=sys.stderr)
                time.sleep(0.5)
    return dropped


@dataclass
class Stack:
    effective: EffectiveConfig
    exec: Any = None
    temporal: Any = None
    scheduler_app: Any = None
    workers: list[tuple[Any, asyncio.Task[Any]]] = field(default_factory=list)
    _temporal_workers: list[tuple[Any, asyncio.Task[Any]]] = field(default_factory=list)
    _lifespan: Any = None

    @classmethod
    @contextlib.asynccontextmanager
    async def run(cls, env: dict[str, str], *, workers: int = 1) -> AsyncIterator[Stack]:
        from ce_exec.context import build_services
        from ce_orchestrator.worker import connect, make_worker, queues
        from ce_scheduler.app import create_app

        effective = load_effective(ROOT / "config", env)
        stack = cls(effective)
        stack.exec = build_services(effective, pool_size=4)
        for bucket in (effective.settings.s3_bucket_assets, effective.settings.s3_bucket_artifacts):
            await stack.exec.storage.ensure_bucket(bucket)
        stack.temporal = await connect(effective, attempts=3)
        for role in ("orchestrator", "render"):
            worker = make_worker(stack.temporal, stack.exec, role, queues(effective))
            stack._temporal_workers.append((worker, asyncio.create_task(worker.run())))
        stack.scheduler_app = create_app(effective)
        stack._lifespan = stack.scheduler_app.router.lifespan_context(stack.scheduler_app)
        await stack._lifespan.__aenter__()
        try:
            for _ in range(workers):
                await stack.start_worker()
            yield stack
        finally:
            for runtime, task in stack.workers:
                runtime.stopping.set()
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                with contextlib.suppress(Exception):
                    await runtime.aclose()
            await stack._lifespan.__aexit__(None, None, None)
            for worker, task in stack._temporal_workers:
                await worker.shutdown()
                with contextlib.suppress(BaseException):
                    await task
            await stack.exec.close()
            if effective.settings.storage_provider == "s3" and not os.environ.get("CE_E2E_KEEP_BUCKETS"):
                buckets = [effective.settings.s3_bucket_assets, effective.settings.s3_bucket_artifacts]
                with contextlib.suppress(Exception):  # cleanup must never mask the test's own result
                    await asyncio.to_thread(drop_s3_buckets, effective.settings, buckets)

    def scheduler_client(self) -> Any:
        from ce_worker import SchedulerClient

        transport = httpx.ASGITransport(app=self.scheduler_app)
        http = httpx.AsyncClient(transport=transport, base_url="http://scheduler.test", timeout=60)
        return SchedulerClient("http://scheduler.test", http=http)

    async def start_worker(self, name: str | None = None) -> Any:
        from ce_contracts.plugins import discover
        from ce_scheduler.app import registration_token
        from ce_worker import WorkerConfig, WorkerRuntime, usable_registry

        settings = self.effective.settings
        registry = usable_registry(
            discover(app_env=settings.app_env, include_mocks=True, families=["cpu_model"]),
            settings.cpu_real_engines,
            settings.model_cache_dir,
        )
        config = WorkerConfig(
            scheduler_url="http://scheduler.test",
            registration_token=registration_token(self.effective),
            name=name or f"worker-{len(self.workers) + 1}",
            runtime_family="cpu_model",
            provider="mock",
            gpu_type="mock_gpu",
            vram_gb=96,
            app_env=settings.app_env,
            model_cache_dir=settings.model_cache_dir,
        )
        runtime = WorkerRuntime(config, registry, client=self.scheduler_client())
        await runtime.register()
        task = asyncio.create_task(runtime.run_forever())
        self.workers.append((runtime, task))
        return runtime

    async def kill_worker(self, runtime: Any) -> None:
        """Simulates a crash: the worker stops without reporting (its lease must expire)."""

        async def unreachable(*_: Any, **__: Any) -> Any:
            raise ConnectionError("worker crashed")

        runtime.client._post = unreachable
        for index, (candidate, task) in enumerate(self.workers):
            if candidate is runtime:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                self.workers.pop(index)
                return

    async def generate(self, org_id: UUID, project_id: UUID, spec_data: dict[str, Any], **kwargs: Any) -> Any:
        """Submits a spec as an approved version and runs `GenerateVersionWorkflow` to its end."""
        from ce_exec.submit import submit_spec
        from ce_orchestrator.models import BuildResult
        from ce_orchestrator.worker import build_input, queues

        submitted = await submit_spec(self.exec, org_id=org_id, project_id=project_id, spec_data=spec_data, **kwargs)
        handle = await self.temporal.start_workflow(
            "GenerateVersionWorkflow",
            build_input(
                self.effective, org_id=str(org_id), job_id=str(submitted.job_id), version_id=str(submitted.version_id)
            ),
            id=submitted.workflow_id,
            task_queue=queues(self.effective).orchestrator,
            result_type=BuildResult,
        )
        return submitted, handle
