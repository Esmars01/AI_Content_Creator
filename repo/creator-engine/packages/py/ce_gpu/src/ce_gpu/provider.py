"""The `GPUProvider` interface (§23, §25) and its plugin registry.

`gpu_providers.kind` is a text key validated against the registered providers, never a DB enum
(§25). Uploads and downloads never go through provider-specific copy APIs: workers exchange data
only through presigned object-storage URLs (ADR 0006).
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Literal

from ce_contracts.common import HealthStatus
from ce_contracts.plugins import discover
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GPUOffer",
    "GPUProvider",
    "NoCapacityError",
    "ProviderError",
    "ProviderInstance",
    "ProvisionSpec",
    "create_gpu_provider",
    "gpu_provider_keys",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderError(RuntimeError):
    """A provider call failed (network, API error, rejected request)."""


class NoCapacityError(ProviderError):
    """The provider has no capacity for the requested GPU class and region (triggers provider fallback)."""


class GPUOffer(_Model):
    provider: str
    gpu_class: str
    region: str
    vram_gb: float = Field(ge=0)
    price_per_hour_usd: float = Field(ge=0)
    available: int = Field(default=0, ge=0)
    driver_version: str | None = None
    spot: bool = False


class ProvisionSpec(_Model):
    gpu_class: str
    region: str
    runtime_family: str
    variant: str | None = Field(
        default=None, description="the family image variant (ADR 0052; config/gpu/variants.yaml)"
    )
    image: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    volumes: list[str] = Field(default_factory=list)
    spot_ok: bool = False


class ProviderInstance(_Model):
    provider: str
    external_id: str
    gpu_class: str
    region: str
    runtime_family: str
    state: Literal["provisioning", "running", "stopped", "terminated", "failed"]
    price_per_hour_usd: float = Field(ge=0)
    vram_gb: float = Field(default=0.0, ge=0)
    started_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class GPUProvider(abc.ABC):
    """Provisions and controls GPU hosts. The fleet manager is the only caller (§25)."""

    key: str

    @abc.abstractmethod
    async def provision(self, spec: ProvisionSpec) -> ProviderInstance: ...

    @abc.abstractmethod
    async def start(self, external_id: str) -> ProviderInstance: ...

    @abc.abstractmethod
    async def stop(self, external_id: str) -> ProviderInstance: ...

    @abc.abstractmethod
    async def terminate(self, external_id: str) -> ProviderInstance: ...

    @abc.abstractmethod
    async def status(self, external_id: str) -> ProviderInstance: ...

    @abc.abstractmethod
    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]: ...

    @abc.abstractmethod
    def price(self, instance: ProviderInstance) -> float:
        """USD per hour, captured at provision time (the cost ledger multiplies provisioned seconds by it)."""

    @abc.abstractmethod
    async def health(self) -> HealthStatus: ...

    async def restart(self, external_id: str) -> ProviderInstance:
        """Restarts the instance's container. The default stops and starts it; a provider that can
        reboot in place (keeping the machine and its GPU) overrides this."""
        await self.stop(external_id)
        return await self.start(external_id)

    async def list_instances(self) -> list[ProviderInstance]:
        """The instances the platform created on this provider's account (labeled as the fleet's), for
        recovery: adopting an instance whose id was never recorded and reporting orphans. Optional;
        providers that cannot list raise `NotImplementedError` and are skipped."""
        raise NotImplementedError


def gpu_provider_keys(*, app_env: str | None, include_mocks: bool) -> list[str]:
    return sorted(discover(app_env=app_env, include_mocks=include_mocks).providers("gpu"))


def create_gpu_provider(
    key: str, *, app_env: str | None, include_mocks: bool, config: dict[str, Any] | None = None
) -> GPUProvider:
    """Instantiates a registered provider plugin. `MOCK_GPU=true` registers the mock provider (§10.6)."""
    providers = discover(app_env=app_env, include_mocks=include_mocks).providers("gpu")
    try:
        plugin = providers[key]
    except KeyError:
        raise ValueError(f"unknown GPU provider {key!r}; registered: {sorted(providers)}") from None
    factory = plugin.entrypoint()
    provider = factory({**plugin.manifest.defaults, **(config or {})})
    if not isinstance(provider, GPUProvider):
        raise TypeError(f"GPU provider {key!r} did not return a GPUProvider")
    return provider
