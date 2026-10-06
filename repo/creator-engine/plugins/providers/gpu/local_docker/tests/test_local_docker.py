"""`local_docker`: the container body, the lifecycle against a real Docker daemon (GPUs off: there is
no GPU here, so the NVIDIA device request itself is untested), capacity and offers."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import pytest
from ce_gpu.provider import NoCapacityError, ProviderError, ProvisionSpec
from ce_plugin_gpu_local_docker.provider import LABEL, LocalDockerProvider, container_body

SPEC = ProvisionSpec(
    gpu_class="local_gpu", region="local", runtime_family="tts", variant="chatterbox",
    env={"SCHEDULER_URL": "http://scheduler:8100", "WORKER_TOKEN": "t"},
)  # fmt: skip


def test_container_body_requests_gpus_mounts_the_cache_and_labels_the_worker() -> None:
    body = container_body(
        {"gpus": True, "model_cache_host_dir": "/srv/models", "network": "ce"},
        SPEC,
        image="creator-engine/worker-tts:chatterbox",
    )
    assert body["HostConfig"]["DeviceRequests"] == [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]
    assert body["HostConfig"]["Binds"] == ["/srv/models:/models"] and body["HostConfig"]["NetworkMode"] == "ce"
    assert "WORKER_RUNTIME_FAMILY=tts" in body["Env"] and "MODEL_CACHE_DIR=/models" in body["Env"]
    assert body["Labels"][LABEL] == "1" and body["Labels"][f"{LABEL}.variant"] == "chatterbox"
    assert "DeviceRequests" not in container_body({"gpus": False}, SPEC, image="x")["HostConfig"]


def _docker_ready() -> str | None:
    if not shutil.which("docker") or not os.path.exists("/var/run/docker.sock"):
        return "no Docker daemon socket"
    probe = subprocess.run(
        ["docker", "image", "inspect", "mirror.gcr.io/library/alpine:3.20"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pulled = subprocess.run(
            ["docker", "pull", "-q", "mirror.gcr.io/library/alpine:3.20"], capture_output=True, check=False
        )
        if pulled.returncode != 0:
            return "alpine image unavailable"
    return None


@pytest.mark.infra
async def test_lifecycle_on_the_real_docker_daemon() -> None:
    reason = _docker_ready()
    if reason:
        pytest.skip(reason)
    config: dict[str, Any] = {
        "gpus": False,  # no GPU here; the device request is covered by the body test above
        "images": {"tts:chatterbox": "mirror.gcr.io/library/alpine:3.20"},
        "command": ["sleep", "300"],
        "classes": {"local_gpu": {"count": 1, "vram_gb": 24, "price_per_hour_usd": 0.0, "driver_version": "570.1"}},
        "regions": ["local"],
        "labels": {"creator-engine.test": "local-docker"},
    }
    provider = LocalDockerProvider(config)
    assert (await provider.health()).ok
    instance = await provider.provision(SPEC)
    try:
        assert instance.state == "running" and instance.runtime_family == "tts" and instance.gpu_class == "local_gpu"
        assert any(w.external_id == instance.external_id for w in await provider.list_workers())
        (offer,) = await provider.list_offers()
        assert offer.available == 0 and offer.driver_version == "570.1"
        with pytest.raises(NoCapacityError):
            await provider.provision(SPEC)
        stopped = await provider.stop(instance.external_id)
        assert stopped.state == "stopped"
        assert (await provider.start(instance.external_id)).state == "running"
    finally:
        assert (await provider.terminate(instance.external_id)).state == "terminated"
    with pytest.raises(ProviderError):
        await provider.status(instance.external_id)


async def test_unknown_class_is_refused_and_an_unreachable_daemon_is_unhealthy(tmp_path: Any) -> None:
    provider = LocalDockerProvider({"docker_socket": str(tmp_path / "missing.sock"), "classes": {}})
    assert not (await provider.health()).ok
    with pytest.raises(ProviderError):
        await provider.provision(SPEC)
