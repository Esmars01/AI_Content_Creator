"""RunPod serverless endpoints as fleet capacity. Our workers dial out to the scheduler instead of
consuming RunPod's request queue, so an endpoint is used as a pool of *active* workers: provisioning
one instance raises the endpoint's `workersMin` by one, stopping lowers it (the endpoint must use a
family image whose start command is `python -m ce_worker`). One endpoint per family variant
(`endpoints: {"<family>:<variant>": "<endpoint id>"}`); instance ids are `<endpoint id>/<slot>`.
Untested against the live API here: no key, no approved spend (rule 5)."""

from __future__ import annotations

from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, ProviderError, ProviderInstance, ProvisionSpec

from ce_plugin_gpu_runpod.common import RunPodConfig

__all__ = ["RunPodServerlessProvider", "create"]


class RunPodServerlessProvider(GPUProvider):
    key = "runpod_serverless"

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = RunPodConfig(config)
        self.endpoints: dict[str, str] = dict(config.get("endpoints") or {})
        self._slots: dict[str, ProvisionSpec] = {}

    def _endpoint(self, spec: ProvisionSpec) -> str:
        key = f"{spec.runtime_family}:{spec.variant or spec.runtime_family}"
        endpoint = self.endpoints.get(key)
        if not endpoint:
            raise ProviderError(f"RunPod serverless: no endpoint configured for {key}")
        return endpoint

    async def _scale(self, endpoint_id: str, delta: int) -> dict[str, Any]:
        endpoint = await self.cfg.client.get_endpoint(endpoint_id)
        current = int(endpoint.get("workersMin") or 0)
        wanted = max(0, current + delta)
        maximum = int(endpoint.get("workersMax") or wanted)
        if wanted > maximum:
            from ce_gpu.provider import NoCapacityError

            raise NoCapacityError(f"RunPod serverless {endpoint_id}: workersMax {maximum} reached")
        await self.cfg.client.update_endpoint(endpoint_id, {"workersMin": wanted})
        return {**endpoint, "workersMin": wanted}

    def _instance(self, external_id: str, state: str, endpoint: dict[str, Any] | None = None) -> ProviderInstance:
        spec = self._slots.get(external_id)
        workers = list((endpoint or {}).get("workers") or [])
        prices = [float(w.get("adjustedCostPerHr") or w.get("costPerHr") or 0.0) for w in workers]
        listed = float(self.cfg.classes.get(spec.gpu_class if spec else "", {}).get("price_per_hour_usd", 0.0))
        return ProviderInstance(
            provider=self.key,
            external_id=external_id,
            gpu_class=spec.gpu_class if spec else "",
            region=spec.region if spec else "",
            runtime_family=spec.runtime_family if spec else "",
            state=state,  # type: ignore[arg-type]
            price_per_hour_usd=max(prices) if prices else listed,
            vram_gb=float(self.cfg.classes.get(spec.gpu_class if spec else "", {}).get("vram_gb", 0.0)),
            detail={"workersMin": (endpoint or {}).get("workersMin"), "workers": len(workers)},
        )

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        self.cfg.guard_paid(spec.gpu_class)
        endpoint_id = self._endpoint(spec)
        endpoint = await self._scale(endpoint_id, +1)
        external_id = f"{endpoint_id}/{endpoint['workersMin']}"
        self._slots[external_id] = spec
        return self._instance(external_id, "provisioning", endpoint)

    async def start(self, external_id: str) -> ProviderInstance:
        endpoint = await self._scale(external_id.split("/", 1)[0], +1)
        return self._instance(external_id, "provisioning", endpoint)

    async def stop(self, external_id: str) -> ProviderInstance:
        endpoint = await self._scale(external_id.split("/", 1)[0], -1)
        return self._instance(external_id, "stopped", endpoint)

    async def terminate(self, external_id: str) -> ProviderInstance:
        instance = await self.stop(external_id)
        self._slots.pop(external_id, None)
        return instance.model_copy(update={"state": "terminated"})

    async def status(self, external_id: str) -> ProviderInstance:
        endpoint_id, _, slot = external_id.partition("/")
        endpoint = await self.cfg.client.request("GET", f"/endpoints/{endpoint_id}", params={"includeWorkers": "true"})
        running = len([w for w in endpoint.get("workers") or [] if w.get("desiredStatus") == "RUNNING"])
        state = "running" if slot.isdigit() and running >= int(slot) else "provisioning"
        if slot.isdigit() and int(endpoint.get("workersMin") or 0) < int(slot):
            state = "stopped"
        return self._instance(external_id, state, endpoint)

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        return self.cfg.offers(self.key, gpu_class, region)

    def price(self, instance: ProviderInstance) -> float:
        return instance.price_per_hour_usd

    async def health(self) -> HealthStatus:
        return await self.cfg.health()


def create(config: dict[str, Any]) -> RunPodServerlessProvider:
    return RunPodServerlessProvider(config)
