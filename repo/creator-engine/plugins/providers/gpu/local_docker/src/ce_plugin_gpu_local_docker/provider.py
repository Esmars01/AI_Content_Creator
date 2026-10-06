"""`local_docker`: one container per worker on the local Docker host (§25 MVP providers).

`provision` creates and starts a container from the family image of the requested variant
(`image_template` or `images["<family>:<variant>"]`, overridable per spec), with the fleet's
environment (scheduler URL, worker token, external id), the model cache bind-mounted at `/models`
and — with `gpus: true` — an NVIDIA device request for all GPUs. `stop`/`start` map to the container's
stop/start, `terminate` removes it, `status` reads its state. Offers are the configured local classes.
Every container carries the label `creator-engine.worker=1`, so `list_workers()` finds them again
after a scheduler restart."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

import httpx
from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, NoCapacityError, ProviderError, ProviderInstance, ProvisionSpec

__all__ = ["LocalDockerProvider", "container_body", "create"]

LABEL = "creator-engine.worker"
STATES = {
    "created": "provisioning",
    "restarting": "provisioning",
    "running": "running",
    "paused": "stopped",
    "exited": "stopped",
    "dead": "failed",
    "removing": "terminated",
}


def _time(value: Any) -> datetime | None:
    text = str(value or "")
    if not text or text.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(text[:26].rstrip("Z") + "+00:00").astimezone(UTC)
    except ValueError:
        return None


def container_body(config: dict[str, Any], spec: ProvisionSpec, *, image: str) -> dict[str, Any]:
    """The `POST /containers/create` body."""
    env = {**spec.env, "WORKER_RUNTIME_FAMILY": spec.runtime_family, "MODEL_CACHE_DIR": "/models"}
    host: dict[str, Any] = {"RestartPolicy": {"Name": "no"}}
    binds = list(spec.volumes)
    if config.get("model_cache_host_dir"):
        binds.append(f"{config['model_cache_host_dir']}:/models")
    if binds:
        host["Binds"] = binds
    if config.get("network"):
        host["NetworkMode"] = str(config["network"])
    if config.get("gpus", True):
        host["DeviceRequests"] = [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]
    body: dict[str, Any] = {
        "Image": image,
        "Env": [f"{k}={v}" for k, v in sorted(env.items())],
        "Labels": {
            LABEL: "1",
            f"{LABEL}.family": spec.runtime_family,
            f"{LABEL}.variant": spec.variant or spec.runtime_family,
            f"{LABEL}.gpu_class": spec.gpu_class,
            f"{LABEL}.region": spec.region,
            **{str(k): str(v) for k, v in dict(config.get("labels") or {}).items()},
        },
        "HostConfig": host,
    }
    if config.get("command"):
        body["Cmd"] = list(config["command"])
    return body


class LocalDockerProvider(GPUProvider):
    key = "local_docker"

    def __init__(self, config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = dict(config)
        self.classes: dict[str, dict[str, Any]] = {k: dict(v) for k, v in dict(config.get("classes") or {}).items()}
        self.regions = list(config.get("regions") or ["local"])
        self.transport = (
            transport
            or config.get("transport")
            or httpx.AsyncHTTPTransport(uds=str(config.get("docker_socket", "/var/run/docker.sock")))
        )

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(transport=self.transport, base_url="http://docker", timeout=60) as http:
            try:
                response = await http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderError(f"docker {method} {path}: {type(exc).__name__}: {exc}") from None
        if response.status_code >= 400:
            raise ProviderError(f"docker {method} {path}: HTTP {response.status_code}: {response.text[:300]}")
        return response

    def _image(self, spec: ProvisionSpec) -> str:
        if spec.image:
            return spec.image
        variant = spec.variant or spec.runtime_family
        images = dict(self.config.get("images") or {})
        if f"{spec.runtime_family}:{variant}" in images:
            return str(images[f"{spec.runtime_family}:{variant}"])
        return str(self.config.get("image_template", "creator-engine/worker-{family}:{variant}")).format(
            family=spec.runtime_family, variant=variant
        )

    async def _containers(self) -> list[dict[str, Any]]:
        response = await self._call(
            "GET", "/containers/json", params={"all": "true", "filters": f'{{"label":["{LABEL}=1"]}}'}
        )
        return list(response.json())

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        cls = self.classes.get(spec.gpu_class)
        if cls is None or spec.region not in self.regions:
            raise ProviderError(f"local_docker offers no {spec.gpu_class} in {spec.region}")
        running = [
            c
            for c in await self._containers()
            if c.get("Labels", {}).get(f"{LABEL}.gpu_class") == spec.gpu_class
            and c.get("State") in ("created", "running")
        ]
        if len(running) >= int(cls.get("count", 1)):
            raise NoCapacityError(f"local_docker: all {cls.get('count', 1)} {spec.gpu_class} slots are in use")
        name = f"ce-worker-{spec.runtime_family}-{secrets.token_hex(4)}"
        body = container_body(self.config, spec, image=self._image(spec))
        body["Env"].append(f"WORKER_EXTERNAL_ID={name}")
        created = (await self._call("POST", "/containers/create", params={"name": name}, json=body)).json()
        await self._call("POST", f"/containers/{created['Id']}/start")
        return await self.status(name)

    async def start(self, external_id: str) -> ProviderInstance:
        await self._call("POST", f"/containers/{external_id}/start")
        return await self.status(external_id)

    async def stop(self, external_id: str) -> ProviderInstance:
        await self._call("POST", f"/containers/{external_id}/stop", params={"t": "30"})
        return await self.status(external_id)

    async def terminate(self, external_id: str) -> ProviderInstance:
        info = await self.status(external_id)
        await self._call("DELETE", f"/containers/{external_id}", params={"force": "true"})
        return info.model_copy(update={"state": "terminated"})

    async def status(self, external_id: str) -> ProviderInstance:
        data = (await self._call("GET", f"/containers/{external_id}/json")).json()
        labels = dict(data.get("Config", {}).get("Labels") or {})
        gpu_class = labels.get(f"{LABEL}.gpu_class", "")
        state = data.get("State", {})
        return ProviderInstance(
            provider=self.key,
            external_id=str(data.get("Name", external_id)).lstrip("/"),
            gpu_class=gpu_class,
            region=labels.get(f"{LABEL}.region", self.regions[0]),
            runtime_family=labels.get(f"{LABEL}.family", ""),
            state=STATES.get(str(state.get("Status")), "failed"),  # type: ignore[arg-type]
            price_per_hour_usd=float(self.classes.get(gpu_class, {}).get("price_per_hour_usd", 0.0)),
            vram_gb=float(self.classes.get(gpu_class, {}).get("vram_gb", 0.0)),
            started_at=_time(state.get("StartedAt")),
            detail={"container_id": str(data.get("Id", ""))[:12], "exit_code": state.get("ExitCode")},
        )

    async def list_workers(self) -> list[ProviderInstance]:
        return [await self.status(str(c["Names"][0]).lstrip("/")) for c in await self._containers()]

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        used: dict[str, int] = {}
        for c in await self._containers():
            if c.get("State") in ("created", "running"):
                key = c.get("Labels", {}).get(f"{LABEL}.gpu_class", "")
                used[key] = used.get(key, 0) + 1
        offers = []
        for name, cls in sorted(self.classes.items()):
            for reg in self.regions:
                if (gpu_class is not None and name != gpu_class) or (region is not None and reg != region):
                    continue
                offers.append(
                    GPUOffer(
                        provider=self.key,
                        gpu_class=name,
                        region=reg,
                        vram_gb=float(cls.get("vram_gb", 0.0)),
                        price_per_hour_usd=float(cls.get("price_per_hour_usd", 0.0)),
                        available=max(0, int(cls.get("count", 1)) - used.get(name, 0)),
                        driver_version=cls.get("driver_version"),
                    )
                )
        return offers

    def price(self, instance: ProviderInstance) -> float:
        return instance.price_per_hour_usd

    async def health(self) -> HealthStatus:
        try:
            await self._call("GET", "/_ping")
        except ProviderError as exc:
            return HealthStatus(ok=False, detail=str(exc)[:200])
        return HealthStatus(ok=True, detail="docker daemon reachable")


def create(config: dict[str, Any]) -> LocalDockerProvider:
    return LocalDockerProvider(config)
