"""Vast.ai instances as fleet workers. Provisioning searches the marketplace for the configured class
and region and rents the cheapest acceptable offer.

The instance runs the family image as is (Vast runtype `args`: the image's own entrypoint, `python -m
ce_worker`). It dials out to the scheduler with the environment the fleet passes, and keeps its weights
under the model cache on the container disk, or on a linked Vast volume.

Untested against the live API here: no key, no approved spend (rule 5).

Notes on Vast semantics:
- A stopped instance keeps its disk and still bills storage.
- Starting it again depends on its machine having the GPU free; until then it waits in a scheduling
  state, which `status` reports as provisioning.
- `cancel_unavail` makes a rental fail instead of leaving a stopped instance behind.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, NoCapacityError, ProviderError, ProviderInstance, ProvisionSpec

from ce_plugin_gpu_vast.client import OfferUnavailableError
from ce_plugin_gpu_vast.common import VastConfig, country_code, offer_price, offer_vram_gb

__all__ = ["LABEL_PREFIX", "VastProvider", "create", "instance_body", "instance_label", "labeled_worker_id", "state_of"]

_STARTING = {"created", "loading", "scheduling", "pending"}


def state_of(row: dict[str, Any] | None) -> str:
    """A Vast instance row → `ProviderInstance.state`.

    The row's `actual_status` is one of created, loading, running, exited or offline. `intended_status`
    says what was asked (running or stopped). Vast no longer knowing the instance means it was destroyed.
    """
    if row is None:
        return "terminated"
    actual = str(row.get("actual_status") or "").lower()
    intended = str(row.get("intended_status") or row.get("next_state") or "").lower()
    if intended in {"destroyed", "terminated"}:
        return "terminated"
    if actual == "running":
        return "stopped" if intended == "stopped" else "running"
    if actual in {"exited", "stopped"}:
        return "stopped" if intended == "stopped" else "failed"  # the container ended on its own
    if actual == "offline":
        return "failed"
    if actual in _STARTING or not actual:
        return "stopped" if intended == "stopped" else "provisioning"
    return "failed"


LABEL_PREFIX = "ce-worker-"
_WORKER_ID = re.compile(r"^ce-worker-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def instance_label(spec: ProvisionSpec) -> str:
    worker_id = spec.env.get("WORKER_ID", "")
    if worker_id:
        return f"{LABEL_PREFIX}{worker_id}"[:64]
    return f"{LABEL_PREFIX}{spec.runtime_family}-{spec.variant or spec.runtime_family}"[:64]


def labeled_worker_id(label: Any) -> str | None:
    match = _WORKER_ID.match(str(label or ""))
    return match.group(1) if match else None


def instance_body(cfg: VastConfig, spec: ProvisionSpec, *, bid: float | None) -> dict[str, Any]:
    """The `PUT /asks/{offer_id}/` body (Vast's `build_create_instance_payload` fields)."""
    storage = cfg.storage
    cache_dir = str(storage.get("model_cache_dir") or "/models")
    env = {**{str(k): str(v) for k, v in dict(cfg.raw.get("env") or {}).items()}, **spec.env}
    env.setdefault("MODEL_CACHE_DIR", cache_dir)
    body: dict[str, Any] = {
        "client_id": "me",
        "image": cfg.image(spec),
        "env": env,
        "price": bid,  # None = on-demand; a number = interruptible bid in USD/hour (Vast "bid")
        "disk": cfg.disk_gb(spec),
        # The fleet's worker id in the label lets the scheduler find an instance whose id it never
        # recorded (a crash between rental and bookkeeping) and tell its own instances from others.
        "label": instance_label(spec),
        "runtype": "args",  # the image's own entrypoint, no ssh/jupyter injected
        "cancel_unavail": True,  # fail instead of creating a stopped instance when placement fails
    }
    login = cfg.image_login()
    if login:
        body["image_login"] = login  # a private registry (resolved from image_login_ref, never stored)
    volume = dict(storage.get("volume") or {})
    if volume.get("volume_id"):
        body["volume_info"] = {
            "volume_id": int(volume["volume_id"]),
            "create_new": False,
            "mount_path": str(volume.get("mount_path") or cache_dir),
        }
    return body


class VastProvider(GPUProvider):
    key = "vast"

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = VastConfig(config)
        # external id → (gpu_class, region, runtime family, price captured at provision)
        self._known: dict[str, tuple[str, str, str, float]] = {}

    def _instance(self, external_id: str, row: dict[str, Any] | None) -> ProviderInstance:
        gpu_class, region, family, price = self._known.get(external_id, ("", "", "", 0.0))
        state = state_of(row)
        row = row or {}
        charged = row.get("dph_total")
        started = row.get("start_date")
        return ProviderInstance(
            provider=self.key,
            external_id=external_id,
            gpu_class=gpu_class,
            region=region,
            runtime_family=family,
            state=state,  # type: ignore[arg-type]
            price_per_hour_usd=0.0 if state == "terminated" else float(charged or price or 0.0),
            vram_gb=offer_vram_gb(row) or float(self.cfg.classes.get(gpu_class, {}).get("vram_gb", 0.0)),
            started_at=datetime.fromtimestamp(float(started), UTC) if isinstance(started, int | float) else None,
            detail={
                "actual_status": row.get("actual_status"),
                "intended_status": row.get("intended_status"),
                "status_msg": str(row.get("status_msg") or "")[:200] or None,
                "machine_id": row.get("machine_id"),
                "gpu_name": row.get("gpu_name"),
                "country": country_code(row.get("geolocation")),
                "label": row.get("label"),
                "worker_id": labeled_worker_id(row.get("label")),
                "gpu_util": row.get("gpu_util"),
                "disk_space_gb": row.get("disk_space"),
                "disk_usage_gb": row.get("disk_usage"),
            },
        )

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        self.cfg.guard_paid(spec.gpu_class)
        interruptible = self.cfg.interruptible_for(spec)
        self.cfg.image(spec)  # fail before searching when no image is configured
        query = self.cfg.query(spec.gpu_class, spec.region, interruptible=interruptible, disk_gb=self.cfg.disk_gb(spec))
        offers = [
            o
            for o in await self.cfg.client.search_offers(query)
            if self.cfg.acceptable(o, spec.gpu_class, interruptible=interruptible)
        ]
        if not offers:
            raise NoCapacityError(f"Vast: no offer for {spec.gpu_class} in {spec.region} within the price ceiling")
        attempts = max(1, int(self.cfg.search.get("rent_attempts", 3)))
        for offer in offers[:attempts]:
            bid = self.cfg.bid_price(offer) if interruptible else None
            body = instance_body(self.cfg, spec, bid=bid)
            try:
                contract = await self.cfg.client.create_instance(int(offer["id"]), body)
            except OfferUnavailableError:
                continue  # rented by someone else since the search: the next cheapest offer
            external_id = str(contract)
            price = bid if bid is not None else offer_price(offer)
            self._known[external_id] = (spec.gpu_class, spec.region, spec.runtime_family, float(price))
            instance = self._instance(external_id, {"actual_status": "created", "intended_status": "running"})
            return instance.model_copy(
                update={
                    "price_per_hour_usd": float(price),
                    "vram_gb": offer_vram_gb(offer) or instance.vram_gb,
                    "detail": {
                        **instance.detail,
                        "offer_id": offer["id"],
                        "machine_id": offer.get("machine_id"),
                        "gpu_name": offer.get("gpu_name"),
                        "country": country_code(offer.get("geolocation")),
                        "interruptible": interruptible,
                    },
                }
            )
        raise NoCapacityError(f"Vast: the {min(attempts, len(offers))} cheapest {spec.gpu_class} offers were taken")

    async def start(self, external_id: str) -> ProviderInstance:
        await self.cfg.client.set_state(external_id, "running")
        return await self.status(external_id)

    async def stop(self, external_id: str) -> ProviderInstance:
        await self.cfg.client.set_state(external_id, "stopped")
        return await self.status(external_id)

    async def terminate(self, external_id: str) -> ProviderInstance:
        await self.cfg.client.destroy_instance(external_id)  # already gone counts as terminated
        instance = self._instance(external_id, None)
        self._known.pop(external_id, None)
        return instance

    async def status(self, external_id: str) -> ProviderInstance:
        return self._instance(external_id, await self.cfg.client.get_instance(external_id))

    async def restart(self, external_id: str) -> ProviderInstance:
        """A reboot keeps the machine's GPU; a stop and start could lose it to another renter."""
        await self.cfg.client.reboot_instance(external_id)
        return await self.status(external_id)

    async def list_instances(self) -> list[ProviderInstance]:
        """The account's instances the fleet labeled (`ce-worker-…`); others are never touched."""
        out = []
        for row in await self.cfg.client.list_instances():
            if not str(row.get("label") or "").startswith(LABEL_PREFIX) or row.get("id") is None:
                continue
            out.append(self._instance(str(row["id"]), row))
        return out

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        """Live marketplace offers (on-demand) for the configured classes and regions."""
        out: list[GPUOffer] = []
        for name in sorted(self.cfg.classes):
            if gpu_class is not None and name != gpu_class:
                continue
            for reg in sorted(self.cfg.regions):
                if region is not None and reg != region:
                    continue
                try:
                    query = self.cfg.query(name, reg, interruptible=False)
                except ProviderError:
                    continue  # a class without a GPU mapping offers nothing
                out.extend(self.cfg.offers(self.key, name, reg, await self.cfg.client.search_offers(query)))
        return out

    def price(self, instance: ProviderInstance) -> float:
        return instance.price_per_hour_usd

    async def health(self) -> HealthStatus:
        return await self.cfg.health()


def create(config: dict[str, Any]) -> VastProvider:
    return VastProvider(config)
