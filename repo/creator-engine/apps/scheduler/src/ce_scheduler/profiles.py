"""Model profiles (production cutover §9, §13–§14): what an instance must hold, computed from the
plugin manifests instead of a fixed disk size.

`size_profile` adds up, for the models a profile prepares, the size each manifest declares
(`ModelDecl.size_gb`: the weights at the pinned revision plus the dependencies it fetches), then:

- `staging_headroom` × the model bytes: a re-download or an upgrade lands beside the old copy before
  the atomic rename, and the cache keeps a reserve free;
- `scratch_gb`: inputs, intermediates and encoded outputs of the jobs;
- `image_gb`: the worker image on the instance's disk (an estimate until the image is built and
  measured).

The disk is rounded up to 10 GB. The sizes are what the manifests declare — they were read from the
Hugging Face listing when the manifests were written; this module does not measure anything. The
worker measures the real bytes when it downloads (`model_states`), and refuses a download its disk
cannot hold (`ModelCache.ensure_space`).

VRAM: a colocated profile loads every prepared adapter at once, so their minimum VRAM must fit the class.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ce_config.schemas import GpuProfile, GpuProfileComponent, GpuVariant
from ce_contracts.manifest import PluginManifest

__all__ = ["ModelSize", "ProfileSizing", "component_adapters", "component_models", "size_profile"]


@dataclass(frozen=True)
class ModelSize:
    key: str
    adapter: str
    declared_gb: float
    repo: str | None
    revision: str | None
    license: str | None


@dataclass
class ProfileSizing:
    models: list[ModelSize]
    models_gb: float
    staging_gb: float
    scratch_gb: float
    image_gb: float
    disk_gb: int
    vram_gb: float
    vram_min_gb: float
    vram_recommended_gb: float
    fits: bool
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # adapters or models a component names that are not installed

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "models": [asdict(m) for m in self.models]}


def component_adapters(component: GpuProfileComponent, variants: Mapping[str, GpuVariant]) -> list[str]:
    if component.adapters:
        return list(component.adapters)
    return sorted(
        a for a, v in variants.items() if str(v.family) == str(component.family) and v.variant == component.variant
    )


def component_models(
    component: GpuProfileComponent, manifests: Mapping[str, PluginManifest], adapters: list[str]
) -> list[str]:
    """The model keys a component prepares: its `prepare` list, else every model of its adapters."""
    if component.prepare:
        return list(component.prepare)
    return [m.key for a in adapters if a in manifests for m in manifests[a].models]


def size_profile(
    profile: GpuProfile, manifests: Mapping[str, PluginManifest], variants: Mapping[str, GpuVariant]
) -> ProfileSizing:
    models: list[ModelSize] = []
    missing: list[str] = []
    vram_min = vram_rec = 0.0
    for component in profile.components:
        adapters = component_adapters(component, variants)
        wanted = component_models(component, manifests, adapters)
        found = set()
        for adapter in adapters:
            manifest = manifests.get(adapter)
            if manifest is None:
                missing.append(f"adapter {adapter}")
                continue
            loads = False
            for decl in manifest.models:
                if decl.key in wanted:
                    found.add(decl.key)
                    loads = True
                    source = decl.source
                    models.append(
                        ModelSize(
                            key=decl.key,
                            adapter=adapter,
                            declared_gb=float(decl.size_gb or 0.0),
                            repo=getattr(source, "repo", None),
                            revision=getattr(source, "revision", None),
                            license=decl.license.name if decl.license is not None else None,
                        )
                    )
            if loads:  # warmed at boot: loaded at the same time as the others on this instance
                vram_min += float(manifest.runtime.min_vram_gb or 0.0)
                vram_rec += float(manifest.runtime.recommended_vram_gb or manifest.runtime.min_vram_gb or 0.0)
        missing += [f"model {k}" for k in wanted if k not in found]
    models_gb = round(sum(m.declared_gb for m in models), 2)
    staging_gb = round(models_gb * profile.staging_headroom, 2)
    total = models_gb + staging_gb + profile.scratch_gb + profile.image_gb
    disk = max(math.ceil(total / 10.0) * 10, math.ceil(profile.min_disk_gb))
    notes = [
        "model sizes are the manifests' declared size_gb (weights at the pin plus fetched dependencies), "
        "not measured here; the worker reports the real bytes when it downloads",
        f"image_gb ({profile.image_gb:g}) is an estimate until the image is built and measured",
    ]
    undeclared = [m.key for m in models if m.declared_gb <= 0]
    if undeclared:
        notes.append(f"no declared size for {', '.join(undeclared)}: the disk estimate is too small")
    fits = vram_min <= profile.vram_gb if profile.colocate else True
    if profile.colocate and vram_rec > profile.vram_gb:
        notes.append(
            f"the prepared adapters recommend {vram_rec:g} GB VRAM together, above the class's {profile.vram_gb:g} GB: "
            "expect lower throughput or load them on demand"
        )
    elif profile.colocate and vram_rec == profile.vram_gb:
        notes.append(f"the prepared adapters' recommended VRAM fills the class ({vram_rec:g} GB): no headroom")
    return ProfileSizing(
        models=models,
        models_gb=models_gb,
        staging_gb=staging_gb,
        scratch_gb=float(profile.scratch_gb),
        image_gb=float(profile.image_gb),
        disk_gb=disk,
        vram_gb=float(profile.vram_gb),
        vram_min_gb=vram_min,
        vram_recommended_gb=vram_rec,
        fits=fits,
        notes=notes,
        missing=missing,
    )
