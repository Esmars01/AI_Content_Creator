"""`python -m ce_gpu_worker`: register with the scheduler and serve leases until stopped."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys

from ce_config.settings import load_effective, startup_issues
from ce_contracts.plugins import cpu_engine_errors, discover
from ce_obs import configure_logging, configure_tracing, get_logger
from ce_obs.metrics import WORKER_TASKS, serve_metrics
from ce_worker import WorkerConfig, WorkerRuntime, usable_registry

__all__ = ["main", "registration_token"]


def registration_token(worker_token: str | None, secret_key: str) -> str:
    """WORKER_TOKEN, or in dev/test a token derived from SECRET_KEY (scheduler and workers share it)."""
    if worker_token:
        return worker_token
    return hmac.new(secret_key.encode(), b"ce-worker-registration", hashlib.sha256).hexdigest()


async def _run() -> int:
    effective = load_effective(os.environ.get("CE_CONFIG_ROOT", "config"))
    settings = effective.settings
    configure_logging("gpu-worker", settings.log_level)
    log = get_logger("ce.gpu_worker")
    errors = [i for i in startup_issues(effective) if i.severity == "error"]
    if errors:
        for issue in errors:
            log.error("refusing to start", code=issue.code, error=issue.message)
        return 2
    family = os.environ.get("WORKER_RUNTIME_FAMILY", "cpu_model")
    found = discover(app_env=settings.app_env, include_mocks=settings.mock_gpu, families=[family])
    missing = cpu_engine_errors(found, settings.cpu_real_engines, settings.model_cache_dir)
    if missing:
        for problem in missing:
            log.error("refusing to start: CPU_REAL_ENGINES=on but assets are missing", detail=problem)
        return 2
    registry = usable_registry(found, settings.cpu_real_engines, settings.model_cache_dir)
    if not registry.plugins:
        log.error("no plugins for this runtime family", family=family)
        return 2
    token = registration_token(
        settings.worker_token.get_secret_value() if settings.worker_token else None,
        settings.secret_key.get_secret_value(),
    )
    config = WorkerConfig(
        scheduler_url=os.environ.get("SCHEDULER_URL", settings.scheduler_public_url),
        registration_token=token,
        name=os.environ.get("WORKER_NAME", "") or os.uname().nodename,
        runtime_family=family,
        provider=os.environ.get("WORKER_PROVIDER", "mock" if settings.mock_gpu else None),
        gpu_type=os.environ.get("WORKER_GPU_TYPE", "mock_gpu" if settings.mock_gpu else "cpu"),
        vram_gb=float(os.environ.get("WORKER_VRAM_GB", "96" if settings.mock_gpu else "0")),
        app_env=settings.app_env,
        model_cache_dir=settings.model_cache_dir,
        adapter_defaults=json.loads(os.environ.get("CE_ADAPTER_DEFAULTS", "{}") or "{}"),
        # tasks at once: 1 unless set (GPU workers); Compose and native mode set it for worker-cpu
        concurrency=max(1, int(os.environ.get("WORKER_CONCURRENCY", "1") or 1)),
    )
    # tracing only once the startup checks passed (a refused start leaves no global state behind)
    configure_tracing("gpu-worker", settings.otel_exporter_otlp_endpoint, environment=settings.app_env)
    serve_metrics(int(os.environ.get("METRICS_PORT", "0")))
    runtime = WorkerRuntime(config, registry)
    runtime.on_task_done = lambda capability, outcome: WORKER_TASKS.labels(capability, outcome).inc()
    runtime.install_signal_handlers()
    while True:
        try:
            await runtime.register()
            break
        except Exception as exc:
            log.warning("registration failed; retrying", error=str(exc)[:300])
            await asyncio.sleep(2.0)
            if runtime.stopping.is_set():
                return 0
    log.info("worker ready", family=family, adapters=runtime.adapters)
    try:
        await runtime.run_forever()
    finally:
        await runtime.aclose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
