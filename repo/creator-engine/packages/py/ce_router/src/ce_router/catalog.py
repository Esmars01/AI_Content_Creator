"""Builds a `RouterCatalog` from the plugin registry, the config bundle and runtime facts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ce_config.loader import ConfigBundle
from ce_contracts.manifest import PluginManifest
from ce_contracts.plugins import PluginRegistry, cpu_engine_errors, cpu_unavailable
from ce_policy.license import OperatorProfile

from ce_router.router import MeasuredProfile, RouterCatalog

__all__ = ["apply_overlay", "build_catalog", "check_cpu_engines", "cpu_unavailable", "operator_from_config"]


def operator_from_config(
    bundle: ConfigBundle, *, jurisdiction: str | None = None, revenue_band: str | None = None
) -> OperatorProfile:
    """The config default (`config/policy/operator_profile.yaml`), with the §35 environment overrides."""
    base = bundle.operator_profile
    return OperatorProfile(
        jurisdiction=jurisdiction or (base.jurisdiction if base else "EU"),
        regions_served=tuple(base.regions_served if base else ("EU",)),
        revenue_band=revenue_band or (base.revenue_band if base else "lt_1m"),
    )


def check_cpu_engines(registry: PluginRegistry, cpu_real_engines: str, model_cache_dir: str | None) -> list[str]:
    """Startup errors: with `CPU_REAL_ENGINES=on` every real CPU engine must have its assets."""
    return cpu_engine_errors(registry, cpu_real_engines, model_cache_dir)


def apply_overlay(manifests: Mapping[str, PluginManifest], overlay: Any) -> dict[str, PluginManifest]:
    """The manifests as the database knows them (`ce_db.registry.RegistryOverlay`, duck-typed):
    promoted or disabled status, recorded validation evidence, and knobs `CalibrationWorkflow`
    found monotonic (only those reach the compiler, §15.7)."""
    out: dict[str, PluginManifest] = {}
    for adapter_id, manifest in manifests.items():
        update: dict[str, Any] = {}
        if adapter_id in overlay.status:
            update["status"] = overlay.status[adapter_id]
        if adapter_id in overlay.validation:
            update["validation"] = overlay.validation[adapter_id]
        knobs = dict(manifest.knobs)
        for name, spec in manifest.knobs.items():
            curve = overlay.knobs.get((adapter_id, name))
            if curve is not None:
                monotonic = str(curve.get("monotonic", "unverified"))
                knobs[name] = type(spec).model_validate(
                    {
                        **spec.model_dump(),
                        "calibrated": bool(curve.get("calibrated")),
                        "monotonic": monotonic if monotonic in ("true", "false") else "unverified",
                    }
                )
        if knobs != dict(manifest.knobs):
            update["knobs"] = knobs
        out[adapter_id] = manifest.model_copy(update=update) if update else manifest
    return out


def build_catalog(
    registry: PluginRegistry,
    bundle: ConfigBundle,
    *,
    app_env: str,
    mock_gpu: bool,
    operator: OperatorProfile,
    gpu_prices: Mapping[str, float] | None = None,
    disabled: Iterable[str] = (),
    unhealthy: Iterable[str] = (),
    measured: Mapping[tuple[str, str], MeasuredProfile] | None = None,
    quality: Mapping[tuple[str, str], float] | None = None,
    qc_pass_rate: Mapping[tuple[str, str], float] | None = None,
    cpu_real_engines: str = "off",
    model_cache_dir: str | None = None,
    overlay: Any = None,
) -> RouterCatalog:
    """`overlay` (`ce_db.registry.load_overlay`) applies the database's promotions, evidence,
    disabled adapters, measured profiles and knob calibrations on top of the installed manifests."""
    pools = bundle.gpu_pools
    languages = bundle.languages.languages if bundle.languages else {}
    manifests = {p.id: p.manifest for p in registry.plugins.values() if p.manifest.kind != "provider"}
    measured = dict(measured or {})
    if overlay is not None:
        manifests = apply_overlay(manifests, overlay)
        disabled = frozenset(disabled) | frozenset(overlay.disabled)
        for key, data in overlay.measured.items():
            measured.setdefault(
                key,
                MeasuredProfile(
                    success_rate=float(data["success_rate"]),
                    n=int(data.get("n", 0)),
                    source=str(data.get("source", "bench")),
                ),
            )
    return RouterCatalog(
        manifests=manifests,
        profiles=bundle.routing,
        languages=languages,
        pools=list(pools.pools) if pools else [],
        gpu_class_vram={k: v.vram_gb for k, v in (pools.classes.items() if pools else [])},
        operator=operator,
        app_env=app_env,
        mock_gpu=mock_gpu,
        gpu_providers=frozenset(registry.providers("gpu")),
        gpu_prices=dict(gpu_prices or {}),
        disabled=frozenset(disabled),
        unhealthy=frozenset(unhealthy),
        measured=measured,
        quality=dict(quality or {}),
        qc_pass_rate=dict(qc_pass_rate or {}),
        cpu_unavailable=cpu_unavailable(registry, cpu_real_engines, model_cache_dir),
    )
