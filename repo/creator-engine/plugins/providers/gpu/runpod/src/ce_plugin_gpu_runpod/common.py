"""Configuration shared by the RunPod providers: GPU classes → RunPod GPU type ids, regions → data
centers, the worker image per family and variant, the paid-provisioning switch and price guards."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, ProviderError, ProvisionSpec

from ce_plugin_gpu_runpod.client import RunPodClient, api_key_from

__all__ = ["RunPodConfig", "parse_time"]


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RunPodConfig:
    def __init__(self, config: dict[str, Any]) -> None:
        self.raw = dict(config)
        self.classes: dict[str, dict[str, Any]] = {k: dict(v) for k, v in dict(config.get("classes") or {}).items()}
        self.regions: dict[str, list[str]] = {k: list(v) for k, v in dict(config.get("regions") or {}).items()}
        self.allow_paid = bool(config.get("allow_paid", False))
        self.max_price = float(config.get("max_price_per_hour_usd", 0.0) or 0.0)
        self.image_template = str(config.get("image_template", ""))
        self.images: dict[str, str] = dict(config.get("images") or {})
        self.client = RunPodClient(api_key_from(config), base_url=str(config.get("base_url") or "https://rest.runpod.io/v1"),
                                   transport=config.get("transport"))  # fmt: skip

    def gpu_class(self, name: str) -> dict[str, Any]:
        cls = self.classes.get(name)
        if not cls or not cls.get("gpu_type_ids"):
            raise ProviderError(f"RunPod: no GPU type mapping for class {name!r}")
        return cls

    def data_centers(self, region: str) -> list[str]:
        if region not in self.regions:
            raise ProviderError(f"RunPod: unknown region {region!r} (configured: {sorted(self.regions)})")
        return self.regions[region]

    def image(self, spec: ProvisionSpec) -> str:
        if spec.image:
            return spec.image
        variant = spec.variant or spec.runtime_family
        key = f"{spec.runtime_family}:{variant}"
        if key in self.images:
            return self.images[key]
        if not self.image_template:
            raise ProviderError(f"RunPod: no image for {key} (set `images` or `image_template`)")
        return self.image_template.format(family=spec.runtime_family, variant=variant)

    def guard_paid(self, gpu_class: str) -> None:
        """Rule: spending money needs the owner's explicit approval (§41). The provider is inert until an
        administrator sets `allow_paid` on it; a configured price ceiling refuses pricier classes."""
        if not self.allow_paid:
            raise ProviderError("RunPod: paid provisioning is not enabled for this provider (allow_paid=false)")
        listed = float(self.classes.get(gpu_class, {}).get("price_per_hour_usd", 0.0))
        if self.max_price and listed > self.max_price:
            raise ProviderError(f"RunPod: {gpu_class} lists {listed} USD/h, above max_price_per_hour_usd")

    def offers(self, provider: str, gpu_class: str | None, region: str | None) -> list[GPUOffer]:
        """RunPod's REST API publishes no price list: offers come from the configured classes with their
        list prices [RV]; the price actually charged is captured from the pod (`costPerHr`) at provision."""
        out = []
        for name, cls in sorted(self.classes.items()):
            if gpu_class is not None and name != gpu_class:
                continue
            for reg in sorted(self.regions):
                if region is not None and reg != region:
                    continue
                out.append(
                    GPUOffer(
                        provider=provider,
                        gpu_class=name,
                        region=reg,
                        vram_gb=float(cls.get("vram_gb", 0.0)),
                        price_per_hour_usd=float(cls.get("price_per_hour_usd", 0.0)),
                        available=int(cls.get("max_instances", 1)),
                        driver_version=cls.get("min_driver"),
                        spot=bool(cls.get("spot", False)),
                    )
                )
        return out

    async def health(self) -> HealthStatus:
        if not self.client.api_key:
            return HealthStatus(ok=False, detail="no RUNPOD_API_KEY configured")
        try:
            await self.client.list_pods()
        except ProviderError as exc:
            return HealthStatus(ok=False, detail=str(exc)[:200])
        return HealthStatus(ok=True, detail="RunPod REST API reachable")
