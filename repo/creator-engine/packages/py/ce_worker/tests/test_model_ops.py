"""Model operations (production cutover §10–§12): byte progress, upstream content checks, the disk
check before a download, no orphaned staging, model states and the prepare command. No network: the
fetchers run against an httpx MockTransport fake of the Hugging Face API."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ce_worker.fetchers import huggingface_fetcher
from ce_worker.model_cache import FetchProgress, ModelCache, ModelFetchError, NotEnoughDiskError
from ce_worker.multi import child_environments
from ce_worker.telemetry import TelemetryCollector, parse_gpu_rows

FILES = {"model.safetensors": b"w" * 4096, "config.json": b"{}"}


def _hf(files: dict[str, bytes], *, lie: dict[str, str] | None = None, stall: asyncio.Event | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/models/" in request.url.path:
            assert request.url.params["blobs"] == "true"
            siblings = []
            for name, data in files.items():
                entry: dict[str, Any] = {"rfilename": name, "size": len(data)}
                if name.endswith(".safetensors"):
                    digest = (lie or {}).get(name) or hashlib.sha256(data).hexdigest()
                    entry["lfs"] = {"size": len(data), "sha256": digest, "pointerSize": 130}
                siblings.append(entry)
            return httpx.Response(200, json={"siblings": siblings})
        name = request.url.path.split("/resolve/c0ffee/", 1)[1]
        return httpx.Response(200, content=files[name])

    return huggingface_fetcher(client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_a_download_reports_bytes_and_is_checked_against_the_upstream_hash(tmp_path: Path) -> None:
    cache = ModelCache(tmp_path / "cache", reserve_gb=0)
    cache.register_fetcher("hf", _hf(FILES))
    progress = FetchProgress("m")
    path = await cache.ensure("m", "hf://org/repo@c0ffee", progress=progress)
    assert sorted(p.name for p in path.iterdir()) == ["config.json", "model.safetensors"]
    assert progress.state == "installed" and progress.bytes_total == 4098 and progress.bytes_done == 4098
    assert progress.files_done == progress.files_total == 2
    assert progress.as_dict()["state"] == "installed"
    assert cache.entries["m"].verification == {"manifest": 0, "upstream": 1, "recorded": 1}
    again = FetchProgress("m")
    await cache.ensure("m", "hf://org/repo@c0ffee", progress=again)  # cached: no second download
    assert again.state == "installed" and again.bytes_done == 0


async def test_a_corrupt_download_is_refused_and_leaves_no_staging(tmp_path: Path) -> None:
    cache = ModelCache(tmp_path / "cache", reserve_gb=0)
    cache.register_fetcher("hf", _hf(FILES, lie={"model.safetensors": "0" * 64}))
    progress = FetchProgress("m")
    with pytest.raises(ModelFetchError, match="does not match the source"):
        await cache.ensure("m", "hf://org/repo@c0ffee", progress=progress)
    assert progress.state == "failed" and "does not match" in (progress.error or "")
    assert not list((tmp_path / "cache").rglob("*.partial-*")) and "m" not in cache.entries


async def test_the_disk_is_checked_before_anything_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = ModelCache(tmp_path / "cache", reserve_gb=0)
    cache.register_fetcher("hf", _hf(FILES))
    monkeypatch.setattr(cache, "free_bytes", lambda: 1000)  # less than the 4 KB the listing announces
    with pytest.raises(NotEnoughDiskError, match="needs"):
        await cache.ensure("m", "hf://org/repo@c0ffee")
    assert not any(p.is_file() for p in (tmp_path / "cache").rglob("*.safetensors"))


async def test_a_cancelled_download_removes_its_staging(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def slow(uri: str, dest: Path, files: list[str], *, progress: FetchProgress | None = None) -> None:
        (dest / "part.bin").write_bytes(b"x" * 10)
        started.set()
        await asyncio.sleep(3600)

    cache = ModelCache(tmp_path / "cache", reserve_gb=0)
    cache.register_fetcher("hf", slow)
    progress = FetchProgress("m")
    task = asyncio.ensure_future(cache.ensure("m", "hf://org/repo@c0ffee", progress=progress))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert progress.state == "failed" and progress.error == "cancelled"
    assert not list((tmp_path / "cache").rglob("*.partial-*"))


def test_old_staging_left_by_a_crash_is_cleaned(tmp_path: Path) -> None:
    cache = ModelCache(tmp_path, reserve_gb=0)
    old = tmp_path / "hf" / "org" / "repo@c0ffee.partial-123-abcd"
    new = tmp_path / "hf" / "org" / "repo@c0ffee.partial-456-ef01"
    for d in (old, new):
        d.mkdir(parents=True)
    os.utime(old, (time.time() - 7 * 3600, time.time() - 7 * 3600))
    assert cache.clean_staging() == [str(old)]
    assert new.exists() and not old.exists()  # a recent one may be another worker's download


def test_gpu_telemetry_parsing_leaves_out_what_the_driver_does_not_report() -> None:
    rows = parse_gpu_rows("0, NVIDIA A100-SXM4-80GB, 87, 61234, 81920, 54, [N/A], 570.124.06\n")
    assert rows == [
        {"index": 0, "name": "NVIDIA A100-SXM4-80GB", "util_pct": 87.0, "vram_used_gb": 59.8, "vram_total_gb": 80.0,
         "temp_c": 54.0, "driver_version": "570.124.06"}
    ]  # fmt: skip
    assert "power_w" not in rows[0]  # [N/A] is absent, never 0
    assert parse_gpu_rows("garbage") == []


async def test_telemetry_without_a_gpu_reports_no_gpu_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ce_worker.telemetry.shutil.which", lambda name: None)
    snapshot = await TelemetryCollector(str(tmp_path)).snapshot({"running_tasks": 0, "unknown": None})
    assert "gpus" not in snapshot and "gpu_util_pct" not in snapshot and "unknown" not in snapshot
    assert snapshot["disk"]["total_gb"] > 0 and snapshot["running_tasks"] == 0


def test_the_profile_supervisor_gives_each_family_its_own_identity() -> None:
    env = {
        "WORKER_COMPONENTS": "wan,tts", "APP_ENV": "prod", "SCHEDULER_URL": "https://sched", "HF_TOKEN": "hf",
        "WORKER_TOKEN_WAN": "tw", "WORKER_TOKEN_TTS": "tt", "WORKER_ID_WAN": "w1", "WORKER_ID_TTS": "w2",
        "WORKER_ADAPTERS_WAN": "infinitetalk", "WORKER_ADAPTERS_TTS": "chatterbox_turbo",
        "WORKER_PREPARE_WAN": "infinitetalk-single", "WORKER_PYTHON_WAN": "/opt/env/infinitetalk/bin/python",
        "WORKER_PYTHONPATH_WAN": "/opt/upstream/InfiniteTalk", "WORKER_PREPARE_WARM": "1",
    }  # fmt: skip
    children = {family: (argv, child) for family, argv, child in child_environments(env)}
    wan_argv, wan = children["wan"]
    assert wan_argv == ["/opt/env/infinitetalk/bin/python", "-m", "ce_worker"]
    assert (wan["WORKER_RUNTIME_FAMILY"], wan["WORKER_TOKEN"], wan["WORKER_ID"]) == ("wan", "tw", "w1")
    assert wan["PYTHONPATH"] == "/opt/upstream/InfiniteTalk" and wan["WORKER_PREPARE"] == "infinitetalk-single"
    _, tts = children["tts"]
    assert (tts["WORKER_TOKEN"], tts["WORKER_ADAPTERS"], tts["HF_TOKEN"]) == ("tt", "chatterbox_turbo", "hf")
    assert "WORKER_TOKEN_WAN" not in tts and "PYTHONPATH" not in tts and tts["WORKER_PREPARE_WARM"] == "1"
    with pytest.raises(ValueError, match="WORKER_TOKEN_TTS"):
        child_environments({**env, "WORKER_TOKEN_TTS": ""})
    with pytest.raises(ValueError, match="empty"):
        child_environments({"WORKER_COMPONENTS": ""})


def _runtime(tmp_path: Path, fetcher: Any) -> Any:
    from ce_contracts.plugins import PluginRegistry, discover

    from ce_worker.protocol import StatusReply
    from ce_worker.runtime import WorkerConfig, WorkerRuntime

    found = discover(app_env="test", include_mocks=False, families=["tts"])
    registry = PluginRegistry()
    registry.add(found.get("chatterbox_turbo"))
    cache = ModelCache(tmp_path / "cache", reserve_gb=0)
    cache.register_fetcher("hf", fetcher)

    class Client:
        def __init__(self) -> None:
            self.reports: list[Any] = []

        async def status(self, body: Any) -> StatusReply:
            self.reports.append(body)
            return StatusReply()

        async def aclose(self) -> None: ...

    config = WorkerConfig(scheduler_url="http://sched", registration_token="t", runtime_family="tts",
                          model_cache_dir=str(tmp_path / "cache"))  # fmt: skip
    return WorkerRuntime(config, registry, client=Client(), cache=cache)  # type: ignore[arg-type]


async def test_an_operator_prepare_fetches_reports_and_can_be_cancelled(tmp_path: Path) -> None:
    from ce_worker.protocol import WorkerCommand

    files: dict[str, bytes] = {}

    async def fake(uri: str, dest: Path, patterns: list[str], *, progress: FetchProgress | None = None) -> None:
        if files.get("slow"):
            await asyncio.sleep(3600)
        for name in ("t3_turbo.safetensors", "conds.pt"):
            (dest / name).write_bytes(b"x" * 64)
            if progress is not None:
                progress.add(64)

    runtime = _runtime(tmp_path, fake)
    states = runtime.check_cache()
    assert states["chatterbox-turbo"]["state"] == "not_installed" and states["chatterbox-turbo"]["declared_gb"] == 3.77

    runtime.handle_commands([WorkerCommand(kind="prepare", id="r1", models=["chatterbox-turbo"], warm=False)])
    assert runtime.prepare_seen == "r1"
    await runtime._prepare_task
    state = runtime.model_report()["chatterbox-turbo"]
    assert state["state"] == "installed" and state["size_bytes"] == 128 and state["verified"]["recorded"] == 2
    assert (
        runtime.client.reports and runtime.client.reports[-1].model_states["chatterbox-turbo"]["state"] == "installed"
    )
    task = runtime._prepare_task
    runtime.handle_commands([WorkerCommand(kind="prepare", id="r1")])  # the same request again: ignored
    assert runtime._prepare_task is task

    import shutil

    shutil.rmtree(tmp_path / "cache")  # as if evicted: the next prepare downloads again, slowly
    runtime.cache.entries.clear()
    files["slow"] = b"1"
    runtime.model_states["chatterbox-turbo"]["state"] = "not_installed"
    runtime.handle_commands([WorkerCommand(kind="prepare", id="r2", models=["chatterbox-turbo"], warm=False)])
    for _ in range(50):
        await asyncio.sleep(0.01)
        if runtime.model_report()["chatterbox-turbo"]["state"] == "downloading":
            break
    assert runtime.model_report()["chatterbox-turbo"]["state"] == "downloading"
    runtime.handle_commands([WorkerCommand(kind="cancel_prepare", id="r2")])
    await runtime._prepare_task
    final = runtime.model_report()["chatterbox-turbo"]
    assert final["state"] == "not_installed" and final["note"] == "download cancelled"
    assert not list((tmp_path / "cache").rglob("*.partial-*"))
    await runtime.aclose()


async def test_a_restarted_process_resumes_with_its_stored_worker_token(tmp_path: Path) -> None:
    """Cutover §17, case 1: the enrollment token is spent at the first registration, so a process
    restarted inside the instance resumes with the worker token it stored (0600, keyed by WORKER_ID);
    a token the scheduler refuses is deleted and the worker registers again."""
    from ce_contracts.plugins import PluginRegistry

    from ce_worker.__main__ import credential_file_from_env
    from ce_worker.runtime import SchedulerClient, WorkerConfig, WorkerRuntime

    live = {"worker-token-1"}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        calls.append(f"{path}:{bearer}")
        if path == "register":
            if bearer != "enroll-once" or "spent" in calls:
                return httpx.Response(401, json={"detail": "invalid registration token"})
            calls.append("spent")
            return httpx.Response(200, json={"worker_id": "w1", "token": "worker-token-1", "heartbeat_s": 5,
                                             "lease_s": 30, "long_poll_s": 20, "adapters": []})  # fmt: skip
        if path == "status":
            return httpx.Response(200 if bearer in live else 401, json={"commands": []})
        return httpx.Response(404)

    worker_id = "0192f0a0-0000-7000-8000-00000000abcd"
    env = {"WORKER_ID": worker_id, "MODEL_CACHE_DIR": str(tmp_path / "cache")}
    credential = credential_file_from_env(env)
    assert credential == str(tmp_path / "cache" / ".ce-worker" / f"{worker_id}.json")
    assert credential_file_from_env({"MODEL_CACHE_DIR": "/models"}) is None  # compose/self-managed: none
    assert credential_file_from_env({**env, "WORKER_ID": "../x"}) is None

    def runtime() -> WorkerRuntime:
        client = SchedulerClient("http://sched", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        config = WorkerConfig(scheduler_url="http://sched", registration_token="enroll-once", runtime_family="tts",
                              model_cache_dir=str(tmp_path / "cache"), credential_file=credential)  # fmt: skip
        return WorkerRuntime(config, PluginRegistry(), client=client, cache=ModelCache(tmp_path / "cache"))

    first = await runtime().register()
    assert first.worker_id == "w1" and Path(credential).exists()
    assert os.stat(credential).st_mode & 0o777 == 0o600

    restarted = runtime()  # the same instance, a new process: the enrollment token is spent
    assert (await restarted.register()).worker_id == "w1"
    assert restarted.client.token == "worker-token-1"
    assert calls.count("register:enroll-once") == 1

    live.clear()  # the scheduler no longer accepts it (the row was failed or terminated)
    with pytest.raises(httpx.HTTPStatusError):
        await runtime().register()  # the stale token is dropped; the spent enrollment token is refused
    assert not Path(credential).exists()
