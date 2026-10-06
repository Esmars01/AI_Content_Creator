"""Statically started workers: offers are informational, provisioning is refused (the operator starts
the workers), prices are zero (self-hosted cost is outside the ledger)."""

from __future__ import annotations

from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, ProviderError, ProviderInstance, ProvisionSpec

__all__ = ["LocalProvider", "create"]


class LocalProvider(GPUProvider):
    key = "local"

    def __init__(self, config: dict[str, Any]) -> None:
        self.classes: dict[str, dict[str, float]] = dict(config.get("classes") or {})
        self.regions: list[str] = list(config.get("regions") or ["local"])

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        raise ProviderError(f"local workers are started by the operator, not provisioned ({spec.gpu_class})")

    async def start(self, external_id: str) -> ProviderInstance:
        raise ProviderError(f"local worker {external_id} is controlled by the operator")

    async def stop(self, external_id: str) -> ProviderInstance:
        raise ProviderError(f"local worker {external_id} is controlled by the operator")

    async def terminate(self, external_id: str) -> ProviderInstance:
        raise ProviderError(f"local worker {external_id} is controlled by the operator")

    async def status(self, external_id: str) -> ProviderInstance:
        raise ProviderError(f"local worker {external_id}: status comes from its heartbeats, not the provider")

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        return [
            GPUOffer(
                provider=self.key,
                gpu_class=name,
                region=reg,
                vram_gb=float(cls.get("vram_gb", 0.0)),
                price_per_hour_usd=float(cls.get("price_per_hour_usd", 0.0)),
                available=0,
            )
            for name, cls in sorted(self.classes.items())
            if gpu_class is None or name == gpu_class
            for reg in self.regions
            if region is None or reg == region
        ]

    def price(self, instance: ProviderInstance) -> float:
        return 0.0

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="statically started workers")


def create(config: dict[str, Any]) -> LocalProvider:
    return LocalProvider(config)
