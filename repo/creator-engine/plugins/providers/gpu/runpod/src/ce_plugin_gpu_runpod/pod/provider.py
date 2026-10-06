"""RunPod pods as fleet workers. A pod runs a family image (`python -m ce_worker`), dials out to the
scheduler with the environment the fleet passes, and keeps its weights on a network volume mounted
at `/models` (§25 model cache). Untested against the live API here: no key, no approved spend (rule 5)."""

from __future__ import annotations

from typing import Any

from ce_contracts.common import HealthStatus
from ce_gpu.provider import GPUOffer, GPUProvider, ProviderInstance, ProvisionSpec

from ce_plugin_gpu_runpod.common import RunPodConfig, parse_time

__all__ = ["RunPodPodProvider", "create", "pod_body"]

STATES = {"RUNNING": "running", "EXITED": "stopped", "TERMINATED": "terminated"}


def pod_body(cfg: RunPodConfig, spec: ProvisionSpec) -> dict[str, Any]:
    """The `POST /pods` body (RunPod `PodCreateInput`)."""
    cls = cfg.gpu_class(spec.gpu_class)
    raw = cfg.raw
    body: dict[str, Any] = {
        "name": f"ce-worker-{spec.runtime_family}-{spec.variant or spec.runtime_family}"[:64],
        "computeType": "GPU",
        "imageName": cfg.image(spec),
        "gpuTypeIds": list(cls["gpu_type_ids"]),
        "gpuTypePriority": "custom",
        "gpuCount": int(cls.get("gpu_count", 1)),
        "cloudType": str(raw.get("cloud_type", "SECURE")),
        "interruptible": bool(spec.spot_ok and cls.get("spot", False)),
        "dataCenterIds": cfg.data_centers(spec.region),
        "dataCenterPriority": "availability",
        "containerDiskInGb": int(raw.get("container_disk_gb", 40)),
        "env": {**dict(raw.get("env") or {}), **spec.env},
        "allowedCudaVersions": list(raw.get("allowed_cuda_versions", ["12.8", "12.9", "13.0"])),
    }
    volume = dict(raw.get("network_volumes") or {}).get(spec.region)
    if volume:
        body["networkVolumeId"] = volume
        body["volumeMountPath"] = str(raw.get("volume_mount_path", "/models"))
    return body


class RunPodPodProvider(GPUProvider):
    key = "runpod_pod"

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = RunPodConfig(config)
        self._family: dict[str, tuple[str, str, str]] = {}

    def _instance(
        self, pod: dict[str, Any], *, gpu_class: str = "", region: str = "", family: str = ""
    ) -> ProviderInstance:
        known = self._family.get(str(pod.get("id")), (gpu_class, region, family))
        status = str(pod.get("desiredStatus", "RUNNING"))
        state = STATES.get(status, "failed")
        if state == "running" and not pod.get("lastStartedAt"):
            state = "provisioning"
        price = (
            pod.get("adjustedCostPerHr")
            or pod.get("costPerHr")
            or self.cfg.classes.get(known[0], {}).get("price_per_hour_usd", 0.0)
        )
        return ProviderInstance(
            provider=self.key,
            external_id=str(pod["id"]),
            gpu_class=known[0],
            region=known[1],
            runtime_family=known[2],
            state=state,  # type: ignore[arg-type]
            price_per_hour_usd=float(price or 0.0),
            vram_gb=float(self.cfg.classes.get(known[0], {}).get("vram_gb", 0.0)),
            started_at=parse_time(pod.get("lastStartedAt")),
            detail={"desiredStatus": status, "machine": (pod.get("machine") or {}).get("dataCenterId")},
        )

    async def provision(self, spec: ProvisionSpec) -> ProviderInstance:
        self.cfg.guard_paid(spec.gpu_class)
        pod = await self.cfg.client.create_pod(pod_body(self.cfg, spec))
        self._family[str(pod["id"])] = (spec.gpu_class, spec.region, spec.runtime_family)
        return self._instance(pod)

    async def start(self, external_id: str) -> ProviderInstance:
        await self.cfg.client.start_pod(external_id)
        return await self.status(external_id)

    async def stop(self, external_id: str) -> ProviderInstance:
        await self.cfg.client.stop_pod(external_id)
        return await self.status(external_id)

    async def terminate(self, external_id: str) -> ProviderInstance:
        await self.cfg.client.delete_pod(external_id)
        known = self._family.get(external_id, ("", "", ""))
        return ProviderInstance(
            provider=self.key, external_id=external_id, gpu_class=known[0], region=known[1],
            runtime_family=known[2], state="terminated", price_per_hour_usd=0.0,
        )  # fmt: skip

    async def status(self, external_id: str) -> ProviderInstance:
        return self._instance(await self.cfg.client.get_pod(external_id))

    async def list_offers(self, gpu_class: str | None = None, region: str | None = None) -> list[GPUOffer]:
        return self.cfg.offers(self.key, gpu_class, region)

    def price(self, instance: ProviderInstance) -> float:
        return instance.price_per_hour_usd

    async def health(self) -> HealthStatus:
        return await self.cfg.health()


def create(config: dict[str, Any]) -> RunPodPodProvider:
    return RunPodPodProvider(config)
