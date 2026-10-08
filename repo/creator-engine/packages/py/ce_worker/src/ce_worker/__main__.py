"""`python -m ce_worker`: the worker entrypoint of the GPU runtime-family images (§36, Phase 8).

The control-plane worker app (`apps/gpu-worker`, Python 3.12) reads the layered config through
`ce_config`; GPU family images run the upstream model code on its own Python (3.10–3.12) with only
`ce_contracts`, `ce_plugin_kit`, `ce_worker` and the family's plugins installed (ADR 0032). This
entrypoint therefore reads plain environment variables:

- `SCHEDULER_URL`: the scheduler's public URL (workers dial out);
- `WORKER_TOKEN`: the registration token (from an enrollment-token exchange on self-managed hosts, §30);
- `WORKER_RUNTIME_FAMILY`: the family this image serves (`wan`, `tts`, …); `WORKER_ADAPTERS` narrows it
  to one build variant's adapters;
- `WORKER_NAME`, `WORKER_PROVIDER`, `WORKER_EXTERNAL_ID`, `WORKER_REGION`, `WORKER_GPU_TYPE`,
  `WORKER_VRAM_GB`, `WORKER_PRICE_PER_HOUR_USD`: what the worker reports when it registers;
- `APP_ENV`, `MODEL_CACHE_DIR`, `HF_TOKEN`, `CE_ADAPTER_DEFAULTS`: as in the control plane (§35);
- `WORKER_PREPARE`: `all` or comma-separated model keys to fetch, verify and (unless
  `WORKER_PREPARE_WARM=0`) load right after registering, so the first task finds them ready
  (boot → register → check the cache → prepare the models → ready).

It refuses to start in production with mock adapters (`MOCK_GPU=true`) and when no plugin of the
family is installed, so a misbuilt image fails loudly instead of registering with nothing to run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping

from ce_contracts.plugins import PluginRegistry, discover

from ce_worker.runtime import WorkerConfig, WorkerRuntime

__all__ = ["config_from_env", "main", "prepare_from_env", "startup_errors"]

_log = logging.getLogger("ce.worker")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def detect_vram_gb() -> float:
    """Total memory of the first GPU from `nvidia-smi`, in GB; 0 without one. Used when
    `WORKER_VRAM_GB` is not set, so a GPU worker never advertises 0 GB and leases nothing."""
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return 0.0
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, the binary resolved by shutil.which
            [binary, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        return round(float(out.strip().splitlines()[0]) / 1024, 1)  # MiB → GiB
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0.0


def config_from_env(env: Mapping[str, str]) -> WorkerConfig:
    return WorkerConfig(
        scheduler_url=env.get("SCHEDULER_URL", "http://localhost:8100"),
        registration_token=env.get("WORKER_TOKEN", ""),
        name=env.get("WORKER_NAME") or platform.node(),
        runtime_family=env.get("WORKER_RUNTIME_FAMILY", "cpu_model"),
        provider=env.get("WORKER_PROVIDER") or None,
        external_id=env.get("WORKER_EXTERNAL_ID") or None,
        region=env.get("WORKER_REGION") or None,
        gpu_type=env.get("WORKER_GPU_TYPE", "cpu"),
        vram_gb=float(env.get("WORKER_VRAM_GB", "0") or 0) or detect_vram_gb(),
        price_per_hour_usd=float(env.get("WORKER_PRICE_PER_HOUR_USD", "0") or 0),
        concurrency=max(1, int(env.get("WORKER_CONCURRENCY", "1") or 1)),
        app_env=env.get("APP_ENV", "prod"),
        model_cache_dir=env.get("MODEL_CACHE_DIR", "/models"),
        adapter_defaults=json.loads(env.get("CE_ADAPTER_DEFAULTS", "{}") or "{}"),
    )


def prepare_from_env(env: Mapping[str, str]) -> tuple[list[str] | None, bool] | None:
    """`WORKER_PREPARE` → (model keys or None for all, warm); None when unset."""
    value = (env.get("WORKER_PREPARE") or "").strip()
    if not value:
        return None
    models = None if value.lower() == "all" else [k.strip() for k in value.split(",") if k.strip()]
    warm = (env.get("WORKER_PREPARE_WARM") or "1").strip().lower() not in ("0", "false", "no", "off")
    return models, warm


def registry_from_env(env: Mapping[str, str]) -> PluginRegistry:
    family = env.get("WORKER_RUNTIME_FAMILY", "cpu_model")
    found = discover(app_env=env.get("APP_ENV", "prod"), include_mocks=_truthy(env.get("MOCK_GPU")), families=[family])
    wanted = {a.strip() for a in env.get("WORKER_ADAPTERS", "").split(",") if a.strip()}
    if not wanted:
        return found
    narrowed = PluginRegistry()
    for plugin in found.plugins.values():
        if plugin.id in wanted:
            narrowed.add(plugin)
    narrowed.rejected = list(found.rejected)
    return narrowed


def startup_errors(env: Mapping[str, str], registry: PluginRegistry) -> list[str]:
    errors = []
    app_env = env.get("APP_ENV", "prod")
    if app_env == "prod" and _truthy(env.get("MOCK_GPU")):
        errors.append("MOCK_GPU=true in production: GPU workers serve real adapters only")
    if app_env == "prod" and not env.get("WORKER_TOKEN"):
        errors.append("WORKER_TOKEN is required in production")
    if not registry.plugins:
        errors.append(f"no plugin of family {env.get('WORKER_RUNTIME_FAMILY', 'cpu_model')!r} is installed")
    mocks = [p.id for p in registry.plugins.values() if p.manifest.mock]
    if app_env == "prod" and mocks:
        errors.append(f"mock adapters in production: {mocks}")
    return errors


async def _run(env: Mapping[str, str]) -> int:
    registry = registry_from_env(env)
    errors = startup_errors(env, registry)
    if errors:
        for error in errors:
            _log.error("refusing to start: %s", error)
        return 2
    runtime = WorkerRuntime(config_from_env(env), registry)
    runtime.install_signal_handlers()
    while True:
        try:
            await runtime.register()
            break
        except Exception as exc:
            _log.warning("registration failed; retrying: %s", str(exc)[:300])
            await asyncio.sleep(2.0)
            if runtime.stopping.is_set():
                return 0
    removed = await asyncio.to_thread(runtime.cache.clean_staging)
    if removed:
        _log.info("removed %d abandoned staging directories", len(removed))
    states = runtime.check_cache()
    _log.info("model cache: %s", {k: v["state"] for k, v in states.items()} or "no fetchable models")
    prepare = prepare_from_env(env)
    if prepare is not None:
        models, warm = prepare
        runtime.prepare_seen = "boot"
        runtime._prepare_task = asyncio.ensure_future(runtime._run_prepare(models, warm))
    _log.info("worker ready: family %s, adapters %s", env.get("WORKER_RUNTIME_FAMILY"), runtime.adapters)
    try:
        await runtime.run_forever()
    finally:
        await runtime.aclose()
    return 0


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s"
    )
    sys.exit(asyncio.run(_run(os.environ)))


if __name__ == "__main__":
    main()
