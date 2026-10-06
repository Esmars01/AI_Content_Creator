"""RunPod providers against a fake RunPod REST API (httpx MockTransport). Every request body is
checked against the request schemas of RunPod's published OpenAPI document (`runpod_schema.json`,
extracted from https://rest.runpod.io/v1/openapi.json on 2026-10-04): unknown fields or values outside
an enum fail the test. No live call is made (no key, no approved spend)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from ce_gpu.provider import NoCapacityError, ProviderError, ProvisionSpec
from ce_plugin_gpu_runpod.pod.provider import RunPodPodProvider, pod_body
from ce_plugin_gpu_runpod.serverless.provider import RunPodServerlessProvider

SCHEMA = json.loads((Path(__file__).parent / "runpod_schema.json").read_text(encoding="utf-8"))
KEY = "rp_test_key_not_real"


def _check(kind: str, body: dict[str, Any]) -> None:
    allowed = SCHEMA["requests"][kind]
    for field, value in body.items():
        assert field in allowed, f"{kind}: RunPod has no field {field!r}"
        enum = allowed[field].get("enum")
        if enum:
            for item in value if isinstance(value, list) else [value]:
                assert item in enum, f"{kind}.{field}: {item!r} is not a RunPod value"


class FakeRunPod:
    def __init__(self, *, capacity: bool = True) -> None:
        self.capacity = capacity
        self.pods: dict[str, dict[str, Any]] = {}
        self.endpoints: dict[str, dict[str, Any]] = {
            "ep1": {"id": "ep1", "workersMin": 0, "workersMax": 2, "workers": []}
        }
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        path, method = request.url.path.removeprefix("/v1"), request.method
        self.calls.append((method, path))
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if (method, path) == ("POST", "/pods"):
            _check("pod_create", body)
            if not self.capacity:
                return httpx.Response(
                    500,
                    json={"error": "There are no longer any instances available with the requested specifications."},
                )
            pod_id = f"pod{len(self.pods) + 1}"
            self.pods[pod_id] = {
                "id": pod_id,
                "desiredStatus": "RUNNING",
                "costPerHr": 0.74,
                "lastStartedAt": None,
                "env": body["env"],
            }
            return httpx.Response(201, json=self.pods[pod_id])
        if path == "/pods" and method == "GET":
            return httpx.Response(200, json=list(self.pods.values()))
        if path.startswith("/pods/"):
            pod_id, _, action = path.removeprefix("/pods/").partition("/")
            pod = self.pods[pod_id]
            if method == "GET":
                pod["lastStartedAt"] = pod["lastStartedAt"] or "2026-10-04T12:00:00Z"
                assert set(pod) <= set(SCHEMA["pod_fields"])
                return httpx.Response(200, json=pod)
            if action == "stop":
                pod["desiredStatus"] = "EXITED"
            elif action == "start":
                pod["desiredStatus"] = "RUNNING"
            elif method == "DELETE":
                pod["desiredStatus"] = "TERMINATED"
            return httpx.Response(200, json={})
        if path.startswith("/endpoints/"):
            endpoint = self.endpoints[path.removeprefix("/endpoints/")]
            if method == "PATCH":
                _check("endpoint_update", body)
                endpoint.update(body)
                endpoint["workers"] = [
                    {"id": f"w{i}", "desiredStatus": "RUNNING", "costPerHr": 0.8} for i in range(endpoint["workersMin"])
                ]
            assert set(endpoint) <= set(SCHEMA["endpoint_fields"])
            return httpx.Response(200, json=endpoint)
        return httpx.Response(404, json={"error": "not found"})


def _config(fake: FakeRunPod, **extra: Any) -> dict[str, Any]:
    from ce_contracts.plugins import discover

    defaults = discover(app_env=None, include_mocks=False).providers("gpu")["runpod_pod"].manifest.defaults
    return {**defaults, "api_key": KEY, "transport": httpx.MockTransport(fake.handler),
            "image_template": "ghcr.io/example/creator-engine-worker-{family}:{variant}", **extra}  # fmt: skip


SPEC = ProvisionSpec(
    gpu_class="rtx_4090_24gb", region="eu", runtime_family="tts", variant="chatterbox",
    env={"SCHEDULER_URL": "https://sched.example", "WORKER_TOKEN": "t"}, spot_ok=True,
)  # fmt: skip


def test_the_manifests_are_registered_and_inert_without_allow_paid() -> None:
    from ce_contracts.plugins import discover

    providers = discover(app_env="prod", include_mocks=False).providers("gpu")
    assert {"runpod_pod", "runpod_serverless"} <= set(providers)
    assert providers["runpod_pod"].manifest.defaults["allow_paid"] is False


async def test_pod_lifecycle_and_request_shape() -> None:
    fake = FakeRunPod()
    provider = RunPodPodProvider(_config(fake, allow_paid=True))
    body = pod_body(provider.cfg, SPEC)
    _check("pod_create", body)
    assert body["gpuTypeIds"] == ["NVIDIA GeForce RTX 4090"] and body["interruptible"] is True
    assert body["imageName"] == "ghcr.io/example/creator-engine-worker-tts:chatterbox"
    instance = await provider.provision(SPEC)
    assert (instance.external_id, instance.state, instance.runtime_family) == ("pod1", "provisioning", "tts")
    assert instance.price_per_hour_usd == 0.74  # the charged price, not the configured estimate
    assert (await provider.status("pod1")).state == "running"
    assert (await provider.stop("pod1")).state == "stopped"
    assert (await provider.start("pod1")).state == "running"
    assert (await provider.terminate("pod1")).state == "terminated"
    assert ("DELETE", "/pods/pod1") in fake.calls
    assert (await provider.health()).ok


async def test_paid_provisioning_needs_explicit_permission_and_respects_the_price_ceiling() -> None:
    fake = FakeRunPod()
    with pytest.raises(ProviderError, match="allow_paid"):
        await RunPodPodProvider(_config(fake)).provision(SPEC)
    expensive = RunPodPodProvider(_config(fake, allow_paid=True, max_price_per_hour_usd=0.5))
    with pytest.raises(ProviderError, match="max_price"):
        await expensive.provision(SPEC)
    assert fake.calls == []  # nothing reached RunPod


async def test_no_capacity_maps_to_the_fallback_error_and_missing_keys_are_explicit() -> None:
    provider = RunPodPodProvider(_config(FakeRunPod(capacity=False), allow_paid=True))
    with pytest.raises(NoCapacityError):
        await provider.provision(SPEC)
    keyless = RunPodPodProvider({**_config(FakeRunPod(), allow_paid=True), "api_key": ""})
    import os

    if not os.environ.get("RUNPOD_API_KEY"):
        assert not (await keyless.health()).ok
        with pytest.raises(ProviderError, match="no API key"):
            await keyless.provision(SPEC)


async def test_serverless_scales_the_endpoint_minimum() -> None:
    fake = FakeRunPod()
    provider = RunPodServerlessProvider(_config(fake, allow_paid=True, endpoints={"tts:chatterbox": "ep1"}))
    one = await provider.provision(SPEC)
    two = await provider.provision(SPEC)
    assert (one.external_id, two.external_id) == ("ep1/1", "ep1/2") and fake.endpoints["ep1"]["workersMin"] == 2
    assert (await provider.status("ep1/2")).state == "running"
    with pytest.raises(NoCapacityError):  # workersMax 2
        await provider.provision(SPEC)
    await provider.stop("ep1/2")
    assert fake.endpoints["ep1"]["workersMin"] == 1
    with pytest.raises(ProviderError, match="no endpoint"):
        await provider.provision(SPEC.model_copy(update={"variant": "qwen_tts"}))


async def test_offers_come_from_the_configured_classes() -> None:
    provider = RunPodPodProvider(_config(FakeRunPod()))
    offers = await provider.list_offers(region="eu")
    assert {o.gpu_class for o in offers} == {"rtx_4090_24gb", "rtx_5090_32gb", "rtx_pro_6000_96gb"}
    assert all(o.provider == "runpod_pod" and o.region == "eu" for o in offers)
