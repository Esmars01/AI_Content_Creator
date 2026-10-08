"""The worker runtime (§9 `gpu-worker`, §25): register, long-poll for leases, run adapters,
heartbeat, honor cancellation, upload through presigned URLs, report completion or failure.

One task runs at a time per worker process (GPU memory is the constraint; scale out with more
workers). While a task runs a heartbeat loop extends its lease and watches for the cancel flag;
losing the lease (the scheduler requeued the task) also cancels it. Errors are classified for
the scheduler: `retryable` (infrastructure, retried with the same seed), `oom`, `timeout`,
`cancelled` and `fatal` (the request itself is wrong).

Behavior-capable adapters receive engine syntax from their own translator: when a request carries
compiled `behavior` directives and no `engine` parameters, the runtime applies the plugin's
`BehaviorTranslator` before `run` (§15.7, I1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import signal
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from ce_contracts.capabilities import capability as capability_spec
from ce_contracts.common import CancellationToken, Cancelled, HardwareInfo, LoadContext
from ce_contracts.plugins import PluginRegistry

from ce_worker.context import ArtifactIOError, PresignedRunContext
from ce_worker.fetchers import huggingface_fetcher, url_fetcher
from ce_worker.model_cache import FetchProgress, ModelCache, ModelFetchError
from ce_worker.models import ensure_plugin_models, fetch_plan
from ce_worker.protocol import (
    CompleteBody,
    FailBody,
    HeartbeatBody,
    HeartbeatReply,
    LeaseBody,
    LeasedTask,
    LeaseReply,
    RegisterBody,
    RegisterReply,
    StatusBody,
    StatusReply,
    UploadBody,
    UploadReply,
    UploadSlot,
    WorkerCommand,
)
from ce_worker.telemetry import TelemetryCollector

__all__ = [
    "SchedulerClient",
    "WorkerConfig",
    "WorkerRuntime",
    "classify",
    "default_cache",
    "needs_model_cache",
    "usable_registry",
]

_log = logging.getLogger("ce.worker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerConfig:
    scheduler_url: str
    registration_token: str
    name: str = field(default_factory=platform.node)
    runtime_family: str = "cpu_model"
    provider: str | None = None
    external_id: str | None = None
    region: str | None = None
    gpu_type: str = "cpu"
    vram_gb: float = 0.0
    price_per_hour_usd: float = 0.0
    app_env: str = "dev"
    model_cache_dir: str = "/models"
    scratch_root: str | None = None
    # Tasks run at once (`WORKER_CONCURRENCY`). 1 for GPU workers: the scheduler places by the VRAM a
    # worker reports, and one model per GPU is the safe default. CPU workers running small mock or CPU
    # engines set more, or every model node of a build queues behind one task at a time.
    concurrency: int = 1
    request_timeout_s: float = 30.0
    # per-adapter manifest default overrides (`CE_ADAPTER_DEFAULTS`, JSON): low-memory switches on
    # small GPUs, smoke settings — reach the adapter as `LoadContext.config["defaults"]`
    adapter_defaults: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetch_models: bool = True  # fetch real engines' weights into the model cache before loading


class SchedulerClient:
    """HTTP client of `/internal/v1/worker/*`."""

    def __init__(self, base_url: str, *, http: httpx.AsyncClient | None = None, timeout_s: float = 30.0) -> None:
        self.base = base_url.rstrip("/") + "/internal/v1/worker"
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, read=90.0))
        self.token: str | None = None

    def _headers(self, token: str | None = None) -> dict[str, str]:
        value = token or self.token
        return {"Authorization": f"Bearer {value}"} if value else {}

    async def _post(
        self, path: str, body: Any, *, token: str | None = None, timeout: httpx.Timeout | None = None
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        response = await self.http.post(
            f"{self.base}/{path}", json=body.model_dump(mode="json"), headers=self._headers(token), **extra
        )
        if response.status_code == 204:
            return {}
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{path}: {response.status_code} {response.text[:300]}", request=response.request, response=response
            )
        data: dict[str, Any] = response.json()
        return data

    async def register(self, body: RegisterBody, registration_token: str) -> RegisterReply:
        reply = RegisterReply.model_validate(await self._post("register", body, token=registration_token))
        self.token = reply.token
        return reply

    async def lease(self, body: LeaseBody) -> LeaseReply:
        return LeaseReply.model_validate(await self._post("lease", body))

    async def heartbeat(self, body: HeartbeatBody) -> HeartbeatReply:
        return HeartbeatReply.model_validate(await self._post("heartbeat", body))

    async def status(self, body: StatusBody) -> StatusReply:
        return StatusReply.model_validate(await self._post("status", body))

    async def uploads(self, task_id: str, count: int) -> list[UploadSlot]:
        return UploadReply.model_validate(await self._post("upload", UploadBody(task_id=task_id, count=count))).uploads

    async def complete(self, body: CompleteBody) -> None:
        await self._post("complete", body, timeout=httpx.Timeout(30.0, read=COMPLETE_READ_TIMEOUT_S))

    async def fail(self, body: FailBody) -> None:
        await self._post("fail", body)

    async def aclose(self) -> None:
        await self.http.aclose()


def usable_registry(registry: PluginRegistry, cpu_real_engines: str, model_cache_dir: str | None) -> PluginRegistry:
    """The plugins this worker may advertise: real CPU engines only when `CPU_REAL_ENGINES` and
    their assets allow it (§35); everything else unchanged."""
    from ce_contracts.plugins import cpu_unavailable

    off = cpu_unavailable(registry, cpu_real_engines, model_cache_dir)
    out = PluginRegistry()
    for plugin in registry.plugins.values():
        if plugin.id in off:
            _log.info("not advertising %s: %s", plugin.id, off[plugin.id])
        else:
            out.add(plugin)
    out.rejected = list(registry.rejected)
    return out


def needs_model_cache(manifest: Any) -> bool:
    """Real engine adapters (Phase 8, those with a CPU stand-in for tests) fetch their weights
    through the model cache; mocks and Phase 7 CPU engines (`assets`, `make fetch-cpu-assets`) do not."""
    return not manifest.mock and manifest.test_backend is not None and bool(fetch_plan(manifest))


def default_cache(root: str) -> ModelCache:
    """The worker's cache with the Hugging Face and pinned-URL fetchers (`HF_TOKEN`, `HF_ENDPOINT`)."""
    import os

    cache = ModelCache(
        root,
        max_gb=float(os.environ.get("MODEL_CACHE_MAX_GB", "200")),
        # another worker sharing the root may be loading an entry this one would evict
        evict_grace_s=float(os.environ.get("MODEL_CACHE_EVICT_GRACE_S", "600")),
        reserve_gb=float(os.environ.get("MODEL_CACHE_RESERVE_GB", "5")),  # kept free for scratch and outputs
    )
    cache.register_fetcher(
        "hf",
        huggingface_fetcher(
            endpoint=os.environ.get("HF_ENDPOINT", "https://huggingface.co"),
            token=os.environ.get("HF_TOKEN") or None,
            concurrency=int(os.environ.get("MODEL_FETCH_CONCURRENCY", "4") or 4),
        ),
    )
    cache.register_fetcher("url", url_fetcher())
    return cache


def classify(exc: BaseException) -> str:
    """The §25 error class of an adapter failure."""
    if isinstance(exc, (Cancelled, asyncio.CancelledError)):
        return "cancelled"
    if isinstance(exc, MemoryError) or "out of memory" in str(exc).lower():
        return "oom"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, (ArtifactIOError, ModelFetchError, httpx.TransportError, ConnectionError, OSError)):
        return "retryable"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return "retryable"  # the scheduler or the store had a transient problem, not the adapter
    return "fatal"


# The scheduler answers /complete only after it re-downloaded and re-hashed every output, which
# for large video outputs takes far longer than an ordinary request.
COMPLETE_READ_TIMEOUT_S = 900.0
SHUTDOWN_REASON = "worker shutting down"


class WorkerRuntime:
    def __init__(
        self,
        config: WorkerConfig,
        registry: PluginRegistry,
        *,
        client: SchedulerClient | None = None,
        cache: ModelCache | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.client = client or SchedulerClient(config.scheduler_url, timeout_s=config.request_timeout_s)
        self.cache = cache or default_cache(config.model_cache_dir)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0))
        self.stopping = asyncio.Event()
        self.registered: RegisterReply | None = None
        self.loaded: set[str] = set()
        self._loading: dict[str, asyncio.Lock] = {}
        self.running: set[CancellationToken] = set()
        self.completed = 0
        # per model key: its preparation state (not_installed, downloading, verifying, installed,
        # loading, ready, failed) with revision, bytes and errors — reported to the scheduler
        self.model_states: dict[str, dict[str, Any]] = {}
        self.telemetry = TelemetryCollector(config.model_cache_dir)
        # live fetches: adapter id → cache key → its progress (bytes, speed, state)
        self._fetching: dict[str, dict[str, FetchProgress]] = {}
        self.prepare_seen: str | None = None  # the last operator prepare request taken up
        self._prepare_task: asyncio.Task[None] | None = None
        # (capability, outcome) after each task; the service sets it to its metrics (this package
        # stays free of the metrics stack: GPU images import it on Python 3.10)
        self.on_task_done: Callable[[str, str], None] | None = None

    @property
    def adapters(self) -> list[str]:
        return sorted(self.registry.plugins)

    def hardware(self) -> HardwareInfo:
        import os

        return HardwareInfo(
            gpu_type=self.config.gpu_type,
            gpu_count=0 if self.config.gpu_type == "cpu" else 1,
            vram_gb=self.config.vram_gb,
            free_vram_gb=self.config.vram_gb,
            cpu_count=os.cpu_count() or 1,
        )

    async def register(self) -> RegisterReply:
        body = RegisterBody(
            name=self.config.name,
            runtime_family=self.config.runtime_family,
            adapters=self.adapters,
            hardware=self.hardware(),
            provider=self.config.provider,
            external_id=self.config.external_id,
            region=self.config.region,
            gpu_type=self.config.gpu_type,
            price_per_hour_usd=self.config.price_per_hour_usd,
        )
        self.registered = await self.client.register(body, self.config.registration_token)
        _log.info("registered as %s with %d adapters", self.registered.worker_id, len(self.registered.adapters))
        return self.registered

    def resident_models(self) -> list[str]:
        """What is loaded, as the scheduler places by it: the loaded adapters' model keys (a task's
        `model_key`) and the adapter ids themselves."""
        keys = set(self.loaded)
        for adapter_id in self.loaded:
            with contextlib.suppress(KeyError):
                keys.update(m.key for m in self.registry.get(adapter_id).manifest.models)
        return sorted(keys)

    def check_cache(self) -> dict[str, dict[str, Any]]:
        """At boot (and after a restart, when the disk survived): what the cache already holds for
        every adapter's models. Models the image carries (no fetchable source) are `installed`."""
        cached = set(self.cache.cached_models())
        for plugin in self.registry.plugins.values():
            manifest = plugin.manifest
            if manifest.mock:
                continue
            plan = fetch_plan(manifest) if needs_model_cache(manifest) else []
            for decl in manifest.models:
                if decl.key in self.model_states:
                    continue
                entries = [f for f in plan if f.cache_key == decl.key or f.cache_key.startswith(f"{decl.key}:")]
                installed = all(f.cache_key in cached for f in entries)
                source = decl.source
                self.model_states[decl.key] = {
                    "state": "installed" if installed else "not_installed",
                    "adapter": plugin.id,
                    "revision": getattr(source, "revision", None),
                    "declared_gb": decl.size_gb or None,
                    "at": _now(),
                }
        return self.model_states

    def model_report(self) -> dict[str, dict[str, Any]]:
        """Model states with the live progress of running fetches merged in (bytes, speed, ETA)."""
        out = {k: dict(v) for k, v in self.model_states.items()}
        rank = {"failed": 4, "downloading": 3, "verifying": 2, "installed": 1}
        for adapter_id, fetches in self._fetching.items():
            with contextlib.suppress(KeyError):
                keys = [m.key for m in self.registry.get(adapter_id).manifest.models]
                for key in keys:
                    parts = [p for ck, p in fetches.items() if ck == key or ck.startswith(f"{key}:")]
                    if not parts or out.get(key, {}).get("state") not in ("downloading", "verifying", None):
                        continue
                    state = max((p.state for p in parts), key=lambda st: rank.get(st, 0))
                    entry = out.setdefault(key, {})
                    entry["state"] = (
                        "downloading"
                        if state == "installed" and any(p.state == "downloading" for p in parts)
                        else state
                    )
                    entry["bytes_done"] = sum(p.bytes_done for p in parts)
                    totals = [p.bytes_total for p in parts]
                    if all(t is not None for t in totals):
                        entry["bytes_total"] = sum(t for t in totals if t is not None)
                    speeds = [p.as_dict().get("speed_mbps") for p in parts if p.state == "downloading"]
                    if any(speeds):
                        entry["speed_mbps"] = round(sum(x for x in speeds if x), 1)
                    etas = [p.as_dict().get("eta_s") for p in parts]
                    if any(e is not None for e in etas):
                        entry["eta_s"] = max(e for e in etas if e is not None)
                    errors = [p.error for p in parts if p.error]
                    if errors:
                        entry["error"] = errors[0]
        return out

    async def _ensure_models(
        self, plugin: Any, on_phase: Callable[[str], None] | None = None, *, pin: bool = False
    ) -> dict[str, str]:
        """Fetches and verifies a plugin's weights (once per cache), with progress for status reports."""
        model_keys = [m.key for m in plugin.manifest.models]
        cached = set(self.cache.cached_models())
        missing = [f.cache_key for f in fetch_plan(plugin.manifest) if f.cache_key not in cached]
        if missing and on_phase is not None:
            on_phase("fetching_model")
        self._set_model_state(model_keys, "downloading" if missing else "installed")
        fetches = self._fetching.setdefault(plugin.id, {})

        def progress(item: Any) -> FetchProgress:
            fetches[item.cache_key] = FetchProgress(item.cache_key)
            return fetches[item.cache_key]

        try:
            paths = await ensure_plugin_models(self.cache, plugin.manifest, progress=progress)
        except asyncio.CancelledError:
            self._set_model_state(model_keys, "not_installed", note="download cancelled")
            raise
        except BaseException as exc:
            self._set_model_state(model_keys, "failed", error=str(exc)[:300])
            raise
        finally:
            self._fetching.pop(plugin.id, None)
        verified: dict[str, int] = {}
        for item in fetch_plan(plugin.manifest):
            entry = self.cache.entries.get(item.cache_key)
            if entry is not None:
                if pin:
                    entry.pinned = True
                for how, count in (entry.verification or {}).items():
                    verified[how] = verified.get(how, 0) + count
        size = sum(
            {
                e.path: e.size_bytes
                for k, e in self.cache.entries.items()
                if k in {f.cache_key for f in fetch_plan(plugin.manifest)}
            }.values()
        )
        self._set_model_state(model_keys, "installed", size_bytes=size or None, verified=verified or None)
        for key in model_keys:  # where it lives: the cache key and local path of the model itself
            if key in paths:
                self.model_states[key].update({"cache_key": key, "path": paths[key]})
        return paths

    async def prepare(self, models: list[str] | None = None, *, warm: bool = True) -> None:
        """Fetches and verifies the requested models (every model of this worker's adapters when
        None) and, with `warm`, loads them into GPU memory, so the first task does not wait. Failures
        are recorded per model; the others still prepare."""
        wanted = set(models) if models else None
        failures: list[str] = []
        for plugin in sorted(self.registry.plugins.values(), key=lambda p: p.id):
            keys = [m.key for m in plugin.manifest.models]
            if plugin.manifest.mock or not keys or (wanted is not None and not wanted & set(keys)):
                continue
            try:
                if warm:
                    await self._load(plugin.id)
                elif self.config.fetch_models and needs_model_cache(plugin.manifest):
                    lock = self._loading.setdefault(plugin.id, asyncio.Lock())
                    async with lock:
                        await self._ensure_models(plugin)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.append(f"{plugin.id}: {str(exc)[:200]}")
                _log.warning("prepare of %s failed: %s", plugin.id, exc)
        if failures:
            _log.warning("prepare finished with failures: %s", "; ".join(failures))

    def _set_model_state(self, keys: list[str], state: str, **detail: Any) -> None:
        for key in keys:
            entry = dict(self.model_states.get(key, {}))
            entry.update({"state": state, "at": _now(), **{k: v for k, v in detail.items() if v is not None}})
            if state not in ("failed",):
                entry.pop("error", None)
            self.model_states[key] = entry

    async def report(self) -> dict[str, Any]:
        """The telemetry sent with leases and heartbeats (measured values only)."""
        stats = self.cache.stats()
        return await self.telemetry.snapshot(
            {
                "cache": {"entries": stats["entries"], "size_gb": round(stats["bytes"] / 1024**3, 2),
                          "max_gb": round(stats["max_bytes"] / 1024**3, 2), "dir": self.config.model_cache_dir},
                "loaded_adapters": sorted(self.loaded),
                "running_tasks": len(self.running),
                "concurrency": self.config.concurrency,
            }
        )  # fmt: skip

    async def _load(
        self,
        adapter_id: str,
        metrics: dict[str, Any] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> Any:
        """Loads the adapter once; `metrics` receives `fetch_seconds` (model cache) and
        `load_seconds` (the adapter's own load) when this call loaded it (Phase 9 load-time metrics).
        `on_phase` hears `fetching_model` and `loading_model` (the task's status line)."""
        plugin = self.registry.get(adapter_id)
        adapter = plugin.adapter()
        lock = self._loading.setdefault(adapter_id, asyncio.Lock())
        async with lock:  # two tasks of one adapter (concurrency > 1) load it once
            return await self._load_locked(plugin, adapter, adapter_id, metrics, on_phase)

    async def _load_locked(
        self,
        plugin: Any,
        adapter: Any,
        adapter_id: str,
        metrics: dict[str, Any] | None,
        on_phase: Callable[[str], None] | None = None,
    ) -> Any:
        if adapter_id not in self.loaded:
            scratch = Path(self.config.scratch_root or tempfile.gettempdir()) / "ce-worker" / adapter_id
            config: dict[str, Any] = {}
            model_keys = [m.key for m in plugin.manifest.models]
            fetch_started = time.monotonic()
            if self.config.fetch_models and needs_model_cache(plugin.manifest):
                config["model_paths"] = await self._ensure_models(plugin, on_phase)
                if metrics is not None:
                    metrics["fetch_seconds"] = round(time.monotonic() - fetch_started, 3)
            if on_phase is not None:
                on_phase("loading_model")
            self._set_model_state(model_keys, "loading")
            load_started = time.monotonic()
            try:
                await self._load_adapter(plugin, adapter, adapter_id, scratch, config)
            except BaseException as exc:
                self._set_model_state(model_keys, "failed", error=f"load failed: {str(exc)[:280]}")
                raise
            self.loaded.add(adapter_id)
            # loaded into GPU memory: the next task of these models starts without a cold load
            self._set_model_state(model_keys, "ready", load_seconds=round(time.monotonic() - load_started, 3))
            if metrics is not None:
                metrics["load_seconds"] = round(time.monotonic() - load_started, 3)
        return adapter

    async def _load_adapter(
        self, plugin: Any, adapter: Any, adapter_id: str, scratch: Path, config: dict[str, Any]
    ) -> None:
        if adapter_id in self.config.adapter_defaults:
            config["defaults"] = dict(self.config.adapter_defaults[adapter_id])
        await adapter.load(
            LoadContext(
                model_cache_dir=self.config.model_cache_dir,
                scratch_dir=str(scratch),
                app_env=self.config.app_env,
                hardware=self.hardware(),
                config=config,
            )
        )

    def _typed_request(self, task: LeasedTask) -> Any:
        spec = capability_spec(task.capability)
        request = spec.request.model_validate(task.request)
        behavior = getattr(request, "behavior", None)
        engine = getattr(request, "engine", None)
        if behavior is not None and not engine:
            translator = self.registry.get(task.adapter_id).translator()
            if translator is not None:
                request = translator.translate(behavior, request)
        return request

    async def run_task(self, task: LeasedTask) -> None:
        cancel = CancellationToken()
        self.running.add(cancel)
        started = time.monotonic()
        started_at = _now()
        scratch = Path(tempfile.mkdtemp(prefix=f"ce-task-{task.task_id[:8]}-", dir=self.config.scratch_root))
        ctx = PresignedRunContext(
            http=self.http,
            inputs=task.inputs,
            slots=task.uploads,
            more_slots=lambda n: self.client.uploads(task.task_id, n),
            scratch_dir=scratch,
            seed=task.seed,
            logger=_log,
            cancel=cancel,
        )
        progress: dict[str, Any] = {"value": 0.0, "message": "", "phase": None}

        async def on_progress(fraction: float, message: str) -> None:
            progress["value"], progress["message"] = fraction, message

        def on_phase(phase: str) -> None:
            progress["phase"] = phase

        ctx.on_progress = on_progress
        beat = asyncio.ensure_future(self._heartbeat_loop(task, cancel, progress))
        try:
            metrics: dict[str, Any] = {}
            adapter = await self._load(task.adapter_id, metrics, on_phase)
            on_phase("generating")
            request = self._typed_request(task)
            run = asyncio.ensure_future(adapter.run(task.capability, request, ctx))
            waiter = asyncio.ensure_future(cancel.wait())
            done, _ = await asyncio.wait({run, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if run not in done:
                run.cancel()
                with contextlib.suppress(BaseException):
                    await run
                raise Cancelled(cancel.reason or "cancelled")
            waiter.cancel()
            result = run.result()
            on_phase("uploading")  # the scheduler verifies every output before it answers
            await self.client.complete(
                CompleteBody(
                    task_id=task.task_id,
                    result=result.model_dump(mode="json"),
                    outputs=ctx.outputs,
                    started_at=started_at,
                    ended_at=_now(),
                    busy_seconds=round(time.monotonic() - started, 3),
                    metrics=metrics,
                )
            )
            self.completed += 1
            self._observe(task.capability, "succeeded")
        except BaseException as exc:
            error_class = classify(exc)
            if error_class == "cancelled" and cancel.reason == SHUTDOWN_REASON:
                # A deploy, a scale-down or a preemption stopped this worker: infrastructure, so the
                # scheduler re-runs the task (same seed) elsewhere instead of failing the user's node.
                error_class = "retryable"
            self._observe(task.capability, error_class)
            _log.warning("task %s failed (%s): %s", task.task_id, error_class, exc)
            with contextlib.suppress(Exception):
                await self.client.fail(FailBody(task_id=task.task_id, error_class=error_class, message=str(exc)[:2000]))
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
        finally:
            beat.cancel()
            with contextlib.suppress(BaseException):
                await beat
            self.running.discard(cancel)
            ctx.cleanup()

    def _observe(self, capability: str, outcome: str) -> None:
        if self.on_task_done is not None:
            with contextlib.suppress(Exception):
                self.on_task_done(capability, outcome)

    async def _heartbeat_loop(self, task: LeasedTask, cancel: CancellationToken, progress: dict[str, Any]) -> None:
        interval = self.registered.heartbeat_s if self.registered else 5.0
        while True:
            await asyncio.sleep(interval)
            try:
                reply = await self.client.heartbeat(
                    HeartbeatBody(
                        task_id=task.task_id,
                        progress=float(progress["value"]),
                        message=str(progress["message"]),
                        resident_models=self.resident_models(),
                        phase=progress.get("phase"),
                        detail=dict(progress.get("detail") or {}),
                        telemetry=await self._report_safely(),
                        model_states=self.model_report(),
                    )
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 409):  # the lease was lost (reaped)
                    cancel.cancel("lease lost")
                    return
                continue
            except httpx.TransportError:
                continue
            if reply.cancel:
                cancel.cancel("cancelled by the scheduler")
                return

    def handle_commands(self, commands: list[WorkerCommand]) -> None:
        """An operator's prepare (fetch, verify and with `warm` load models) runs beside the lease
        loop; its progress goes out through status reports. A cancel stops the download (its staging
        is removed) and the models report `not_installed` again."""
        for command in commands:
            if command.kind == "prepare" and command.id != self.prepare_seen:
                self.prepare_seen = command.id
                if self._prepare_task is not None and not self._prepare_task.done():
                    self._prepare_task.cancel()
                self._prepare_task = asyncio.ensure_future(self._run_prepare(command.models, command.warm))
            elif command.kind == "cancel_prepare" and self._prepare_task is not None:
                if not self._prepare_task.done():
                    self._prepare_task.cancel()

    async def _run_prepare(self, models: list[str] | None, warm: bool) -> None:
        reporter = asyncio.ensure_future(self._status_loop())
        try:
            await self.prepare(models, warm=warm)
        except asyncio.CancelledError:
            _log.info("prepare cancelled")
        finally:
            reporter.cancel()
            with contextlib.suppress(BaseException):
                await reporter
            await self._send_status()  # the final state, without waiting for the next lease

    async def _status_loop(self, interval_s: float = 3.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self._send_status()

    async def _send_status(self) -> None:
        try:
            reply = await self.client.status(
                StatusBody(
                    telemetry=await self._report_safely(),
                    model_states=self.model_report(),
                    prepare_seen=self.prepare_seen,
                )
            )
        except (httpx.HTTPError, OSError) as exc:
            _log.debug("status report failed: %s", exc)
            return
        cancels = [c for c in reply.commands if c.kind == "cancel_prepare"]
        if cancels and self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()

    async def _report_safely(self) -> dict[str, Any] | None:
        try:
            return await self.report()
        except Exception as exc:  # telemetry never stops the work
            _log.debug("telemetry failed: %s", exc)
            return None

    @property
    def current(self) -> CancellationToken | None:
        """One running task's cancellation token (None when idle)."""
        return next(iter(self.running), None)

    async def run_forever(self, *, max_tasks: int | None = None) -> None:
        if self.registered is None:
            await self.register()
        lanes = max(1, self.config.concurrency)
        await asyncio.gather(*(self._lane(max_tasks) for _ in range(lanes)))

    async def _lane(self, max_tasks: int | None) -> None:
        """One lease → run loop; `concurrency` of them run side by side."""
        backoff = 1.0
        while not self.stopping.is_set():
            if max_tasks is not None and self.completed >= max_tasks:
                return
            try:
                reply = await self.client.lease(
                    LeaseBody(
                        adapters=self.adapters,
                        resident_models=self.resident_models(),
                        cached_models=self.cache.cached_models(),
                        free_vram_gb=self.config.vram_gb,
                        telemetry=await self._report_safely(),
                        model_states=self.model_report(),
                        prepare_seen=self.prepare_seen,
                    )
                )
                backoff = 1.0
                self.handle_commands(reply.commands)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:  # the scheduler forgot us: register again
                    await self.register()
                    continue
                _log.warning("lease failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            except httpx.TransportError as exc:
                _log.warning("scheduler unreachable: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            for task in reply.tasks:
                await self.run_task(task)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop)

    def _stop(self) -> None:
        self.stopping.set()
        for cancel in list(self.running):
            cancel.cancel(SHUTDOWN_REASON)

    async def aclose(self) -> None:
        if self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()
            with contextlib.suppress(BaseException):
                await self._prepare_task
        for adapter_id in list(self.loaded):
            with contextlib.suppress(Exception):
                await self.registry.get(adapter_id).adapter().unload()
        await self.http.aclose()
        await self.client.aclose()
