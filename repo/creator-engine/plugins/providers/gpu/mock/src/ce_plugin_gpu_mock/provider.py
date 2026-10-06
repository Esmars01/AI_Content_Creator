"""Simulated GPU provider. Deterministic given its config and clock; failure injection is explicit.

- `start_latency_s`: an instance stays `provisioning` until that much time has passed;
- `provision_failure_rate`: the fraction of provisions that fail (a seeded sequence, not `random`);
- `no_capacity`: every provision raises `NoCapacityError` (provider fallback tests);
- `capacity_per_class`: running instances per class before `NoCapacityError`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, NoCapacityError, ProviderError, ProviderInstance, ProvisionSpec

__all__ = ["MockGPUProvider", "create"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MockGPUProvider(GPUProvider):
    key = "mock"

    def __init__(self, config: dict[str, Any], *, clock: Callable[[], datetime] = _utcnow) -> None:
        self.classes: dict[str, dict[str, float]] = dict(config.get("classes") or {})
        self.regions: list[str] = list(config.get("regions") or ["local"])
        self.start_latency = timedelta(seconds=float(config.get("start_latency_s", 0.0)))
        self.failure_rate = float(config.get("provision_failure_rate", 0.0))
        self.no_capacity = bool(config.get("no_capacity", False))
        self.capacity = int(config.get("capacity_per_class", 8))
        self.clock = clock
        self.instances: dict[str, ProviderInstance] = {}
        self.specs: dict[str, ProvisionSpec] = {}  # what each instance was asked to run (its environment)
        self._provisions = 0

    def _fails(self) -> bool:
        if self.failure_rate <= 0:
            return False
        digest = hashlib.sha256(f"mock-gpu-provision:{self._provisions}".encode()).digest()
        return int.from_bytes(digest[:4], "big") / 2**32 < self.failure_rate

    def _refresh(self, instance: ProviderInstance) -> ProviderInstance:
        started = instance.started_at
        if instance.state == "provisioning" and started is not None and self.clock() - started >= self.start_latency:
            instance = instance.model_copy(update={"state": "running"})
            self.instances[instance.external_id] = instance
        return instance

    def _get(self, external_id: str) -> ProviderInstance:
        try:
            return self._refresh(self.instances[external_id])
        except KeyError:
            raise ProviderError(f"mock instance {external_id} not found") from None

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        self._provisions += 1
        cls = self.classes.get(spec.gpu_class)
        if cls is None or spec.region not in self.regions:
            raise ProviderError(f"mock provider offers no {spec.gpu_class} in {spec.region}")
        running = [i for i in self.instances.values() if i.gpu_class == spec.gpu_class and i.state != "terminated"]
        if self.no_capacity or len(running) >= self.capacity:
            raise NoCapacityError(f"mock provider: no capacity for {spec.gpu_class}")
        if self._fails():
            raise ProviderError("mock provider: injected provision failure")
        external_id = f"mock-{self._provisions:06d}"
        instance = ProviderInstance(
            provider=self.key,
            external_id=external_id,
            gpu_class=spec.gpu_class,
            region=spec.region,
            runtime_family=spec.runtime_family,
            state="provisioning",
            price_per_hour_usd=float(cls.get("price_per_hour_usd", 0.0)),
            vram_gb=float(cls.get("vram_gb", 0.0)),
            started_at=self.clock(),
        )
        self.instances[external_id] = instance
        self.specs[external_id] = spec
        return self._refresh(instance)

    async def start(self, external_id: str) -> ProviderInstance:
        instance = self._get(external_id)
        if instance.state == "stopped":
            instance = instance.model_copy(update={"state": "running"})
            self.instances[external_id] = instance
        return instance

    async def stop(self, external_id: str) -> ProviderInstance:
        instance = self._get(external_id).model_copy(update={"state": "stopped"})
        self.instances[external_id] = instance
        return instance

    async def terminate(self, external_id: str) -> ProviderInstance:
        instance = self._get(external_id).model_copy(update={"state": "terminated"})
        self.instances[external_id] = instance
        return instance

    async def status(self, external_id: str) -> ProviderInstance:
        return self._get(external_id)

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        offers = []
        for name, cls in sorted(self.classes.items()):
            if gpu_class is not None and name != gpu_class:
                continue
            for reg in self.regions:
                if region is not None and reg != region:
                    continue
                used = sum(1 for i in self.instances.values() if i.gpu_class == name and i.state != "terminated")
                offers.append(
                    GPUOffer(
                        provider=self.key,
                        gpu_class=name,
                        region=reg,
                        vram_gb=float(cls.get("vram_gb", 0.0)),
                        price_per_hour_usd=float(cls.get("price_per_hour_usd", 0.0)),
                        available=0 if self.no_capacity else max(0, self.capacity - used),
                    )
                )
        return offers

    def price(self, instance: ProviderInstance) -> float:
        return instance.price_per_hour_usd

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="simulated")


def create(config: dict[str, Any]) -> MockGPUProvider:
    return MockGPUProvider(config)
