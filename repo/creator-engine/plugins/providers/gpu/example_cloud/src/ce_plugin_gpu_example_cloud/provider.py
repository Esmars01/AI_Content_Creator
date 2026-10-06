"""An in-memory vendor API behind the `GPUProvider` interface. Machines move `pending → running`
on the next status read; the account quota and per-region classes drive `NoCapacityError`."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, NoCapacityError, ProviderError, ProviderInstance, ProvisionSpec

__all__ = ["ExampleCloudProvider", "create"]

_STATES = {"pending": "provisioning", "running": "running", "halted": "stopped", "destroyed": "terminated"}


class ExampleCloudProvider(GPUProvider):
    key = "example_cloud"

    def __init__(self, config: dict[str, Any]) -> None:
        self.quota = int(config.get("quota", 2))
        self.regions: dict[str, list[str]] = {k: list(v) for k, v in dict(config.get("regions") or {}).items()}
        self.prices: dict[str, float] = {k: float(v) for k, v in dict(config.get("prices") or {}).items()}
        self.vram: dict[str, float] = {k: float(v) for k, v in dict(config.get("vram") or {}).items()}
        self.driver = str(config.get("driver_version", ""))
        self.machines: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)

    def _instance(self, machine_id: str) -> ProviderInstance:
        m = self.machines.get(machine_id)
        if m is None:
            raise ProviderError(f"example_cloud: no machine {machine_id}")
        return ProviderInstance(
            provider=self.key, external_id=machine_id, gpu_class=m["class"], region=m["region"],
            runtime_family=m["family"], state=_STATES[m["status"]], price_per_hour_usd=m["price"],  # type: ignore[arg-type]
            vram_gb=self.vram.get(m["class"], 0.0), started_at=m["created"],
        )  # fmt: skip

    def _active(self) -> int:
        return sum(1 for m in self.machines.values() if m["status"] in ("pending", "running"))

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        if spec.gpu_class not in self.regions.get(spec.region, []):
            raise NoCapacityError(f"example_cloud: {spec.gpu_class} not offered in {spec.region}")
        if self._active() >= self.quota:
            raise NoCapacityError("example_cloud: account quota reached")
        machine_id = f"ec-{next(self._ids):04d}"
        self.machines[machine_id] = {
            "class": spec.gpu_class, "region": spec.region, "family": spec.runtime_family, "status": "pending",
            "price": self.prices.get(spec.gpu_class, 0.0), "created": datetime.now(UTC), "env": dict(spec.env),
        }  # fmt: skip
        return self._instance(machine_id)

    async def start(self, external_id: str) -> ProviderInstance:
        self.machines[external_id]["status"] = "pending"
        return self._instance(external_id)

    async def stop(self, external_id: str) -> ProviderInstance:
        self._instance(external_id)
        self.machines[external_id]["status"] = "halted"
        return self._instance(external_id)

    async def terminate(self, external_id: str) -> ProviderInstance:
        self._instance(external_id)
        self.machines[external_id]["status"] = "destroyed"
        return self._instance(external_id)

    async def status(self, external_id: str) -> ProviderInstance:
        machine = self.machines.get(external_id)
        if machine is not None and machine["status"] == "pending":
            machine["status"] = "running"
        return self._instance(external_id)

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        free = max(0, self.quota - self._active())
        return [
            GPUOffer(provider=self.key, gpu_class=cls, region=reg, vram_gb=self.vram.get(cls, 0.0),
                     price_per_hour_usd=self.prices.get(cls, 0.0), available=free, driver_version=self.driver or None)
            for reg, classes in sorted(self.regions.items())
            for cls in classes
            if (gpu_class is None or cls == gpu_class) and (region is None or reg == region)
        ]  # fmt: skip

    def price(self, instance: ProviderInstance) -> float:
        return instance.price_per_hour_usd

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="example_cloud simulation")


def create(config: dict[str, Any]) -> ExampleCloudProvider:
    return ExampleCloudProvider(config)
