"""Worker runtime (§25): error classes, presigned artifact I/O, translator application, model cache."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from ce_contracts.common import ArtifactRef, Cancelled
from ce_contracts.contract_suite import sample_directives
from ce_contracts.plugins import discover

from ce_worker import (
    ArtifactIOError,
    ModelCache,
    ModelCacheError,
    PresignedRunContext,
    WorkerConfig,
    WorkerRuntime,
    classify,
)
from ce_worker.model_cache import parse_uri
from ce_worker.protocol import LeasedTask, UploadSlot


def test_error_classes() -> None:
    assert classify(Cancelled("x")) == "cancelled"
    assert classify(asyncio.CancelledError()) == "cancelled"
    assert classify(MemoryError()) == "oom"
    assert classify(RuntimeError("CUDA out of memory")) == "oom"
    assert classify(TimeoutError()) == "timeout"
    assert classify(ArtifactIOError("gone")) == "retryable"
    assert classify(httpx.ConnectError("down")) == "retryable"
    assert classify(ValueError("bad request")) == "fatal"


def _store() -> tuple[dict[str, bytes], httpx.AsyncClient]:
    blobs: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if request.method == "GET":
            return httpx.Response(200, content=blobs[key]) if key in blobs else httpx.Response(404)
        blobs[key] = request.read()
        return httpx.Response(200)

    return blobs, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://s3.test")


async def test_presigned_context_verifies_inputs_and_uploads_outputs(tmp_path: Path) -> None:
    blobs, http = _store()
    data = b"input bytes"
    sha = hashlib.sha256(data).hexdigest()
    blobs["in"] = data
    blobs["tampered"] = b"something else"
    more: list[int] = []

    async def more_slots(n: int) -> list[UploadSlot]:
        more.append(n)
        return [UploadSlot(key=f"staging/t/x{i}", url=f"http://s3.test/staging/t/x{i}") for i in range(n)]

    ctx = PresignedRunContext(
        http=http,
        inputs={sha: "http://s3.test/in", "f" * 64: "http://s3.test/tampered"},
        slots=[UploadSlot(key="staging/t/a", url="http://s3.test/staging/t/a")],
        more_slots=more_slots,
        scratch_dir=tmp_path,
        seed=3,
    )
    path = await ctx.read_artifact(ArtifactRef(sha256=sha, kind="audio", mime="audio/wav"))
    assert path.read_bytes() == data and path.suffix == ".wav"
    with pytest.raises(ArtifactIOError, match="hash"):
        await ctx.read_artifact(ArtifactRef(sha256="f" * 64, kind="audio"))
    with pytest.raises(ArtifactIOError, match="not granted"):
        await ctx.read_artifact(ArtifactRef(sha256="e" * 64, kind="audio"))
    out = tmp_path / "out.mp4"
    out.write_bytes(b"video")
    ref = await ctx.write_artifact(out, "video", role="video")
    assert ref.artifact_id is None and ref.mime == "video/mp4" and blobs["staging/t/a"] == b"video"
    again = await ctx.write_artifact(out, "video")  # same content: uploaded once
    assert again.sha256 == ref.sha256 and len(ctx.outputs) == 1
    other = tmp_path / "other.json"
    other.write_text("{}")
    await ctx.write_artifact(other, "other")
    assert more == [4] and len(ctx.outputs) == 2
    ctx.cancel.cancel("stop")
    with pytest.raises(Cancelled):
        await ctx.write_artifact(other, "other")
    await http.aclose()


def test_behavior_directives_are_translated_on_the_worker(tmp_path: Path) -> None:
    registry = discover(app_env="test", include_mocks=True, families=["cpu_model"])
    runtime = WorkerRuntime(
        WorkerConfig(scheduler_url="http://x", registration_token="t", model_cache_dir=str(tmp_path)), registry
    )
    ref = {"sha256": "a" * 64, "kind": "image", "mime": "image/png"}
    audio = {"sha256": "b" * 64, "kind": "audio", "mime": "audio/wav"}
    request: dict[str, Any] = {
        "keyframe": ref,
        "audio": audio,
        "behavior": sample_directives().model_dump(mode="json"),
        "width": 64,
        "height": 96,
        "fps": 12,
    }
    task = LeasedTask(
        task_id="t", attempt_id="a", capability="avatar.a2v", adapter_id="mock_avatar_segment", model_key="m",
        seed=1, request=request, lease_expires_at="now",
    )  # fmt: skip
    typed = runtime._typed_request(task)
    assert typed.engine, "the plugin's translator fills engine parameters"
    untouched = runtime._typed_request(task.model_copy(update={"request": {**request, "engine": {"x": 1}}}))
    assert untouched.engine == {"x": 1}  # explicit engine parameters are never overwritten


def test_model_uris() -> None:
    assert parse_uri("hf://org/repo@abc123/weights") == ("hf", "org/repo/weights", "abc123")
    assert parse_uri("s3://bucket/models/x") == ("s3", "bucket/models/x", "")
    with pytest.raises(ModelCacheError):
        parse_uri("hf://org/../etc")
    with pytest.raises(ModelCacheError):
        parse_uri("no-scheme")


async def test_model_cache_downloads_once_verifies_and_evicts_lru(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fetch(uri: str, dest: Path) -> None:
        calls.append(uri)
        (dest / "w.bin").write_bytes(uri.encode() * 1000)

    cache = ModelCache(tmp_path, max_gb=25_000 / 1024**3)
    cache.register_fetcher("hf", fetch)
    a = await cache.ensure("a", "hf://o/a@1")
    assert (a / "w.bin").exists()
    await cache.ensure("a", "hf://o/a@1")
    assert calls == ["hf://o/a@1"]
    good = hashlib.sha256(b"hf://o/b@1" * 1000).hexdigest()
    await cache.ensure("b", "hf://o/b@1", expected={"w.bin": good}, pin=True)
    with pytest.raises(ModelCacheError, match="sha256"):
        await cache.ensure("c", "hf://o/c@1", expected={"w.bin": "0" * 64})
    await cache.ensure("d", "hf://o/d@1")
    assert "a" not in cache.entries and "b" in cache.entries  # LRU evicted, pinned kept
    reloaded = ModelCache(tmp_path)
    assert set(reloaded.cached_models()) == set(cache.cached_models())
    with pytest.raises(ModelCacheError, match="no fetcher"):
        await cache.ensure("e", "s3://bucket/e")


async def test_workers_sharing_a_cache_root_fetch_once_and_keep_each_others_entries(tmp_path: Path) -> None:
    """Phase 9 network volumes: two workers (two cache instances on one root) ask for the same model
    at once — one downloads, the other waits on the lock and reuses it; neither loses the other's
    manifest entries; eviction spares what a worker used within the grace period."""
    calls: list[str] = []
    gate = asyncio.Event()

    async def slow_fetch(uri: str, dest: Path) -> None:
        calls.append(uri)
        await gate.wait()
        (dest / "w.bin").write_bytes(uri.encode() * 100)

    one, two = ModelCache(tmp_path), ModelCache(tmp_path)
    one.register_fetcher("hf", slow_fetch)
    two.register_fetcher("hf", slow_fetch)
    first = asyncio.ensure_future(one.ensure("m", "hf://o/m@1"))
    second = asyncio.ensure_future(two.ensure("m", "hf://o/m@1"))
    await asyncio.sleep(0.2)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert a == b and calls == ["hf://o/m@1"] and not list(tmp_path.glob("**/*.partial*"))
    await one.ensure("x", "hf://o/x@1")
    await two.ensure("y", "hf://o/y@1")
    assert {"m", "x", "y"} <= set(ModelCache(tmp_path).cached_models())  # merged, nothing overwritten
    guarded = ModelCache(tmp_path, max_gb=0, evict_grace_s=600)
    assert guarded.evict() == [] and (tmp_path / "hf").exists()  # all used moments ago
    assert set(ModelCache(tmp_path, max_gb=0).evict()) == {"m", "x", "y"}


# ---------------------------------------------------------------------- Phase 8: fetchers and plans


def test_cache_layout_agrees_with_the_plugin_kit(tmp_path: Path) -> None:
    from ce_plugin_kit.engine import cache_layout_path

    cache = ModelCache(tmp_path)
    cases: list[tuple[str, tuple[str, str | None, str, str | None]]] = [
        ("hf://org/repo@abc", ("huggingface", "org/repo", "abc", None)),
        ("url://host.example/a/b.task@v1", ("url", None, "v1", "https://host.example/a/b.task")),
    ]
    for uri, (source_type, repo, revision, url) in cases:
        for files in ((), ("a/*", "b.json")):
            assert cache.path_for(uri, files) == cache_layout_path(tmp_path, source_type, repo, revision, url, files)


async def test_subsets_get_their_own_directories_and_shared_ones_are_reused(tmp_path: Path) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fetch(uri: str, dest: Path, files: list[str]) -> None:
        calls.append((uri, files))
        for name in files or ["all.bin"]:
            (dest / name.replace("*", "x")).write_bytes(name.encode())

    cache = ModelCache(tmp_path)
    cache.register_fetcher("hf", fetch)
    a = await cache.ensure("vae", "hf://o/r@1", files=["vae.bin"])
    b = await cache.ensure("other:vae", "hf://o/r@1", files=["vae.bin"])
    c = await cache.ensure("full", "hf://o/r@1", files=["vae.bin", "unet.bin"])
    assert a == b != c and len(calls) == 2  # the same subset is fetched once and shared
    cache.max_bytes = 0
    cache.evict()
    assert not a.exists() and not c.exists()  # no other user left: removed


async def test_url_sources_need_pins(tmp_path: Path) -> None:
    async def fetch(uri: str, dest: Path, files: list[str]) -> None:
        (dest / "w.task").write_bytes(b"x")

    cache = ModelCache(tmp_path)
    cache.register_fetcher("url", fetch)
    with pytest.raises(ModelCacheError, match="pinned sha256"):
        await cache.ensure("lm", "url://h/w.task@1", files=["w.task"])
    good = hashlib.sha256(b"x").hexdigest()
    assert (await cache.ensure("lm", "url://h/w.task@1", files=["w.task"], expected={"w.task": good})).exists()


async def test_huggingface_fetcher_lists_the_pinned_revision_and_filters(tmp_path: Path) -> None:
    import httpx

    from ce_worker.fetchers import huggingface_fetcher

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/api/models/" in request.url.path:
            return httpx.Response(
                200, json={"siblings": [{"rfilename": n} for n in ("a.json", "w/x.bin", "README.md")]}
            )
        return httpx.Response(200, content=request.url.path.encode())

    fetch = huggingface_fetcher(client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await fetch("hf://org/repo@c0ffee", tmp_path, ["*.json", "w/*"])
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "a.json",
        "w/x.bin",
    ]
    assert seen[0].endswith("/api/models/org/repo/revision/c0ffee") and seen[1].endswith(
        "/org/repo/resolve/c0ffee/a.json"
    )
    with pytest.raises(ModelCacheError, match="no files match"):
        await fetch("hf://org/repo@c0ffee", tmp_path / "x", ["*.safetensors"])


def test_fetch_plan_covers_models_and_fetchable_dependencies() -> None:
    from ce_contracts.plugins import discover

    from ce_worker.models import fetch_plan
    from ce_worker.runtime import needs_model_cache

    registry = discover(app_env="test", include_mocks=True)
    plan = {f.path_key: f for f in fetch_plan(registry.get("musetalk_v15").manifest)}
    assert set(plan) == {"musetalk-v15", "vae", "audio_encoder", "face_landmarker"}  # git code comes with the image
    assert plan["face_landmarker"].uri.startswith("url://storage.googleapis.com/") and plan["face_landmarker"].expected
    assert plan["vae"].cache_key == "musetalk-v15:vae" and plan["vae"].files == (
        "config.json",
        "diffusion_pytorch_model.safetensors",
    )
    assert needs_model_cache(registry.get("musetalk_v15").manifest)
    assert not needs_model_cache(registry.get("mock_voice").manifest)
    assert not needs_model_cache(registry.get("uvq_15").manifest)  # its checkpoints are in the image's git checkout


def test_model_fetch_errors_are_retryable() -> None:
    from ce_worker.model_cache import ModelFetchError
    from ce_worker.runtime import classify

    assert classify(ModelFetchError("HTTP 503")) == "retryable"
    assert classify(ModelCacheError("checksum")) == "fatal"
    assert classify(RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")) == "oom"
