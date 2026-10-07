"""Configuration of the Vast provider. It covers:

- GPU classes → Vast `gpu_name` values (+ count and minimum VRAM);
- regions → country codes;
- the worker image per family and variant;
- storage and the model cache;
- the paid-provisioning switch and the price guards;
- the offer search and its parsing.

Vast is a marketplace: offers (machines with a price) come from a live search, and an instance is
rented from one offer. Units follow Vast's client:
- `gpu_ram` is MB per GPU (GB × 1000);
- `dph_total` is USD per hour for the whole offer;
- `min_bid` is the current minimum interruptible bid in USD per hour.
"""

from __future__ import annotations

import re
from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, ProviderError, ProvisionSpec

from ce_plugin_gpu_vast.client import DEFAULT_BASE_URL, VastClient, api_key_from

__all__ = ["VastConfig", "country_code", "driver_version", "offer_price", "offer_vram_gb"]

_COUNTRY = re.compile(r"\b([A-Z]{2})\s*$")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and out >= 0 else default  # NaN and negatives are not prices or sizes


def offer_price(offer: dict[str, Any]) -> float:
    """USD per hour of an on-demand offer (`dph_total`: GPU, storage and platform fee included)."""
    return _float(offer.get("dph_total"))


def offer_vram_gb(offer: dict[str, Any]) -> float:
    """VRAM per GPU in GB (`gpu_ram` is MB per GPU)."""
    return round(_float(offer.get("gpu_ram")) / 1000.0, 1)


def country_code(value: Any) -> str | None:
    """The two-letter country of a Vast `geolocation`. Answers carry it as the trailing code
    ("Sweden, SE") or as the code alone; the search filter takes the code."""
    match = _COUNTRY.search(str(value or ""))
    return match.group(1) if match else None


def driver_version(offer: dict[str, Any]) -> str | None:
    """A dotted NVIDIA driver version: `driver_version` when given, else decoded from `driver_vers`.
    Vast packs that integer as MMMmmmppp: 570124006 → "570.124.6"."""
    text = offer.get("driver_version")
    if isinstance(text, str) and text.strip():
        return text.strip()
    packed = offer.get("driver_vers")
    if isinstance(packed, int) and not isinstance(packed, bool) and packed > 0:
        return f"{packed // 1_000_000}.{packed // 1000 % 1000}.{packed % 1000}"
    return None


class VastConfig:
    def __init__(self, config: dict[str, Any]) -> None:
        self.raw = dict(config)
        self.classes: dict[str, dict[str, Any]] = {k: dict(v) for k, v in dict(config.get("classes") or {}).items()}
        self.regions: dict[str, list[str]] = {k: list(v or []) for k, v in dict(config.get("regions") or {}).items()}
        self.allow_paid = bool(config.get("allow_paid", False))
        self.max_price = _float(config.get("max_price_per_hour_usd"))
        self.image_template = str(config.get("image_template", ""))
        self.images: dict[str, str] = dict(config.get("images") or {})
        self.storage: dict[str, Any] = dict(config.get("storage") or {})
        self.search: dict[str, Any] = dict(config.get("search") or {})
        self.interruptible: dict[str, Any] = dict(config.get("interruptible") or {})
        self.client = VastClient(
            api_key_from(config),
            base_url=str(config.get("endpoint") or DEFAULT_BASE_URL),
            transport=config.get("transport"),
            timeout_s=_float(config.get("timeout_s"), 60.0) or 60.0,
        )

    # ------------------------------------------------------------------ mapping
    def gpu_class(self, name: str) -> dict[str, Any]:
        cls = self.classes.get(name)
        if not cls or not cls.get("gpu_names"):
            raise ProviderError(f"Vast: no GPU mapping for class {name!r} (set classes.{name}.gpu_names)")
        return cls

    def countries(self, region: str) -> list[str]:
        """The country codes of a region; an empty list means anywhere."""
        if region not in self.regions:
            raise ProviderError(f"Vast: unknown region {region!r} (configured: {sorted(self.regions)})")
        return [str(c).upper() for c in self.regions[region]]

    def image(self, spec: ProvisionSpec) -> str:
        if spec.image:
            return spec.image
        variant = spec.variant or spec.runtime_family
        key = f"{spec.runtime_family}:{variant}"
        if key in self.images:
            return self.images[key]
        if not self.image_template:
            raise ProviderError(f"Vast: no image for {key} (set `images` or `image_template`)")
        return self.image_template.format(family=spec.runtime_family, variant=variant)

    def interruptible_for(self, spec: ProvisionSpec) -> bool:
        """Spot (Vast "bid") only when the pool allows it, the class allows it and bidding is enabled."""
        cls = self.classes.get(spec.gpu_class, {})
        return bool(spec.spot_ok and cls.get("spot", False) and self.interruptible.get("enabled", False))

    # ------------------------------------------------------------------ money
    def guard_paid(self, gpu_class: str) -> None:
        """Rule: spending money needs the owner's explicit approval (§41). The provider is inert until an
        administrator sets `allow_paid` on it; a configured price ceiling refuses pricier classes."""
        if not self.allow_paid:
            raise ProviderError("Vast: paid provisioning is not enabled for this provider (allow_paid=false)")
        if not self.max_price:
            raise ProviderError("Vast: set max_price_per_hour_usd before paid provisioning (marketplace prices vary)")
        listed = _float(self.classes.get(gpu_class, {}).get("price_per_hour_usd"))
        if listed > self.max_price:
            raise ProviderError(f"Vast: {gpu_class} lists {listed} USD/h, above max_price_per_hour_usd")

    def bid_price(self, offer: dict[str, Any]) -> float:
        """The interruptible bid for an offer: its minimum bid times (1 + margin), never above the ceiling."""
        margin = _float(self.interruptible.get("bid_margin"), 0.1)
        floor = _float(offer.get("min_bid"))
        if not floor:
            raise ProviderError(f"Vast: offer {offer.get('id')} has no min_bid for an interruptible rental")
        return round(min(floor * (1.0 + margin), self.max_price) if self.max_price else floor * (1.0 + margin), 4)

    # ------------------------------------------------------------------ search
    def disk_gb(self) -> float:
        return _float(self.storage.get("disk_gb"), 80.0) or 80.0

    def query(self, gpu_class: str, region: str, *, interruptible: bool, limit: int | None = None) -> dict[str, Any]:
        """The `POST /bundles/` body: Vast's default filters (verified, rentable, not rented, not
        external), the class's GPU model, count and VRAM, the region's countries, reliability, CUDA,
        the price ceiling and, with a linked volume, its machine."""
        cls = self.gpu_class(gpu_class)
        names = [str(n) for n in cls["gpu_names"]]
        count = int(cls.get("gpu_count", 1))
        min_vram = _float(cls.get("min_vram_gb"), _float(cls.get("vram_gb")) * 0.9)
        query: dict[str, Any] = {
            "verified": {"eq": bool(self.search.get("verified", True))},
            "external": {"eq": False},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "gpu_name": {"in": names} if len(names) > 1 else {"eq": names[0]},
            "num_gpus": {"eq": count},
        }
        if min_vram:
            query["gpu_ram"] = {"gte": round(min_vram * 1000.0)}
        reliability = _float(self.search.get("min_reliability"))
        if reliability:
            query["reliability"] = {"gte": reliability}
        cuda = _float(self.search.get("min_cuda"))
        if cuda:
            query["cuda_max_good"] = {"gte": cuda}
        if self.max_price and not interruptible:
            query["dph_total"] = {"lte": self.max_price}
        countries = self.countries(region)
        if countries:
            query["geolocation"] = {"in": countries} if len(countries) > 1 else {"eq": countries[0]}
        machine = self.storage.get("volume", {}) or {}
        if machine.get("machine_id"):
            query["machine_id"] = {"eq": int(machine["machine_id"])}  # a Vast volume lives on one machine
        query["order"] = [["min_bid" if interruptible else "dph_total", "asc"]]
        query["type"] = "bid" if interruptible else "on-demand"
        query["limit"] = int(limit or self.search.get("limit", 20))
        query["allocated_storage"] = self.disk_gb()
        return query

    def acceptable(self, offer: dict[str, Any], gpu_class: str, *, interruptible: bool) -> bool:
        """Re-checks an offer locally: the answer is filtered by Vast, but a ceiling is not a place to trust."""
        cls = self.classes.get(gpu_class, {})
        if not isinstance(offer.get("id"), int) or isinstance(offer.get("id"), bool):
            return False
        if str(offer.get("gpu_name")) not in {str(n) for n in cls.get("gpu_names", [])}:
            return False
        if int(_float(offer.get("num_gpus"), -1)) != int(cls.get("gpu_count", 1)):
            return False
        if interruptible:
            price = self.bid_price(offer) if _float(offer.get("min_bid")) else 0.0
        else:
            price = offer_price(offer)
        if not price:
            return False  # an offer without a price is not one we can account for
        return not (self.max_price and price > self.max_price)

    def offers(self, provider: str, gpu_class: str, region: str, raw: list[dict[str, Any]]) -> list[GPUOffer]:
        cls = self.classes.get(gpu_class, {})
        out = []
        for offer in raw:
            if not self.acceptable(offer, gpu_class, interruptible=False):
                continue
            out.append(
                GPUOffer(
                    provider=provider,
                    gpu_class=gpu_class,
                    region=region,
                    vram_gb=offer_vram_gb(offer) or _float(cls.get("vram_gb")),
                    price_per_hour_usd=offer_price(offer),
                    available=1,  # a Vast offer is one rentable machine slice
                    driver_version=driver_version(offer),
                    spot=bool(cls.get("spot", False) and self.interruptible.get("enabled", False)),
                )
            )
        return sorted(out, key=lambda o: o.price_per_hour_usd)

    async def health(self) -> HealthStatus:
        if not self.client.api_key:
            return HealthStatus(ok=False, detail="no VAST_API_KEY configured")
        try:
            await self.client.current_user()
        except ProviderError as exc:
            return HealthStatus(ok=False, detail=str(exc)[:200])
        return HealthStatus(ok=True, detail="Vast API reachable")
