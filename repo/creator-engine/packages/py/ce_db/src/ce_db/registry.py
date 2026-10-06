"""The model registry in the database (§24, §29 `plugins`/`models`): the installed plugins'
manifests mirrored into rows, and the evidence and admin decisions that live only in the database.

- `sync_registry` upserts one `plugins` row per manifest (`plugin_key`, `version`) and one `models`
  row per declared model (`model_key` = the manifest model key, what routes and leases name).
  Entry-point discovery has precedence for everything the manifest declares (§30): source,
  revision, license closure, VRAM, languages. Two things are the database's:
  - `validation` evidence recorded by smoke/bench runs (`record_validation`) is never downgraded
    by a sync to the manifest's `untested_on_gpu`;
  - `status` after an admin decision (`promote` sets `promotion_basis`; `disable` sets
    `disabled`) is kept; otherwise the manifest's status is mirrored.
- `load_overlay` reads what the router must apply on top of the manifests: status, validation,
  promotion basis, disabled adapters, measured behavior profiles and knob calibrations.

Manifests are passed in duck-typed (`ce_contracts.manifest.PluginManifest`); this module does not
import the plugin contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.behavior import ModelBehaviorProfile
from ce_db.models.platform import Model, Plugin

__all__ = [
    "KNOB_DIMENSION_PREFIX",
    "RegistryOverlay",
    "disable_model",
    "load_overlay",
    "promote_model",
    "record_validation",
    "source_uri",
    "store_knob_calibration",
    "sync_registry",
]

KNOB_DIMENSION_PREFIX = "knob:"  # model_behavior_profiles.dimension of knob calibration rows
VALIDATION_RANK = {"untested_on_gpu": 0, "failed": 1, "smoke_passed": 2, "bench_passed": 3}


def source_uri(source: Any) -> str:
    if source.type == "huggingface" and source.repo:
        return f"hf://{source.repo}@{source.revision}"
    if source.type == "git" and source.repo:
        return f"git+{source.repo}@{source.revision}"
    if source.uri:
        return str(source.uri)
    return f"{source.type}:{source.repo or ''}@{source.revision}"


@dataclass(frozen=True)
class RegistryOverlay:
    """Database state the router applies over the installed manifests (by adapter id)."""

    status: dict[str, str] = field(default_factory=dict)
    validation: dict[str, str] = field(default_factory=dict)
    promotion_basis: dict[str, str] = field(default_factory=dict)
    disabled: frozenset[str] = frozenset()
    measured: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (adapter, dimension)
    knobs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (adapter, knob) → curve

    def to_json(self) -> dict[str, Any]:
        """The JSON form (tuple keys joined with `|`): the per-version snapshot document."""
        return {
            "status": dict(self.status),
            "validation": dict(self.validation),
            "promotion_basis": dict(self.promotion_basis),
            "disabled": sorted(self.disabled),
            "measured": {f"{a}|{d}": v for (a, d), v in sorted(self.measured.items())},
            "knobs": {f"{a}|{k}": v for (a, k), v in sorted(self.knobs.items())},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RegistryOverlay:
        def pairs(raw: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
            out: dict[tuple[str, str], dict[str, Any]] = {}
            for key, value in raw.items():
                left, _, right = key.partition("|")
                out[(left, right)] = dict(value)
            return out

        return cls(
            status=dict(data.get("status", {})),
            validation=dict(data.get("validation", {})),
            promotion_basis=dict(data.get("promotion_basis", {})),
            disabled=frozenset(data.get("disabled", [])),
            measured=pairs(data.get("measured", {})),
            knobs=pairs(data.get("knobs", {})),
        )

    @property
    def digest(self) -> str:
        """Content digest of the overlay (route decisions and coverage depend on it)."""
        data = self.to_json()
        return "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _model_row(manifest: Any, decl: Any) -> dict[str, Any]:
    return {
        "model_key": decl.key,
        "display_name": decl.display_name or decl.key,
        "capabilities": [c.id for c in manifest.capabilities],
        "source_uri": source_uri(decl.source),
        "revision": decl.source.revision,
        "checksums": dict(decl.sha256),
        "size_gb": float(decl.size_gb),
        "vram_min_gb": float(manifest.runtime.min_vram_gb),
        "vram_rec_gb": float(manifest.runtime.recommended_vram_gb),
        "languages": {c.id: c.languages.model_dump(mode="json") for c in manifest.capabilities if c.languages},
        "license": decl.license.model_dump(mode="json"),
        "dependencies": [d.model_dump(mode="json") for d in decl.dependencies],
        "obligations": [o.model_dump(mode="json") for o in getattr(manifest, "obligations", [])],
        "allowed_envs": list(manifest.allowed_envs),
    }


async def sync_registry(session: AsyncSession, manifests: Iterable[Any]) -> dict[str, int]:
    """Mirrors the installed manifests (providers excluded). Returns counts of inserted/updated rows."""
    counts = {"plugins_inserted": 0, "plugins_updated": 0, "models_inserted": 0, "models_updated": 0}
    seen_models: dict[str, str] = {}
    for manifest in sorted(manifests, key=lambda m: m.id):
        if manifest.kind == "provider":
            continue
        data = manifest.model_dump(mode="json")
        plugin = (
            await session.execute(
                sa.select(Plugin).where(Plugin.plugin_key == manifest.id, Plugin.version == manifest.version)
            )
        ).scalar_one_or_none()
        if plugin is None:
            plugin = Plugin(
                plugin_key=manifest.id,
                version=manifest.version,
                kind=manifest.kind,
                manifest=data,
                status=manifest.status,
                validation=manifest.validation,
                runtime_family=manifest.runtime.family,
                enabled=manifest.status != "disabled",
            )
            session.add(plugin)
            await session.flush()
            counts["plugins_inserted"] += 1
        else:
            plugin.manifest = data
            plugin.kind = manifest.kind
            plugin.runtime_family = manifest.runtime.family
            counts["plugins_updated"] += 1
        for decl in manifest.models:
            owner = seen_models.setdefault(decl.key, manifest.id)
            if owner != manifest.id:
                raise ValueError(f"model key {decl.key!r} is declared by both {owner} and {manifest.id}")
            row = (await session.execute(sa.select(Model).where(Model.model_key == decl.key))).scalar_one_or_none()
            values = _model_row(manifest, decl)
            if row is None:
                session.add(
                    Model(plugin_id=plugin.id, status=manifest.status, validation=manifest.validation, **values)
                )
                counts["models_inserted"] += 1
                continue
            for key, value in values.items():
                setattr(row, key, value)
            row.plugin_id = plugin.id
            if row.promotion_basis is None and row.status != "disabled":
                row.status = manifest.status  # no admin decision yet: the manifest decides
            if VALIDATION_RANK.get(row.validation, 0) == 0:
                row.validation = manifest.validation  # evidence in the database is never downgraded
            counts["models_updated"] += 1
        plugin.status = _plugin_status(manifest, await _models_of(session, plugin.id))
        plugin.validation = _plugin_validation(manifest, await _models_of(session, plugin.id))
    await session.flush()
    return counts


async def _models_of(session: AsyncSession, plugin_id: UUID) -> list[Model]:
    return list((await session.execute(sa.select(Model).where(Model.plugin_id == plugin_id))).scalars())


def _plugin_status(manifest: Any, models: list[Model]) -> str:
    if models and any(m.status == "disabled" for m in models):
        return "disabled"
    if models and all(m.promotion_basis is not None and m.status == "production" for m in models):
        return "production"
    return str(manifest.status)


def _plugin_validation(manifest: Any, models: list[Model]) -> str:
    if not models:
        return str(manifest.validation)
    if any(m.validation == "failed" for m in models):
        return "failed"
    return min((m.validation for m in models), key=lambda v: VALIDATION_RANK.get(v, 0))


async def record_validation(session: AsyncSession, model_key: str, validation: str, evidence: dict[str, Any]) -> Model:
    """Smoke or bench evidence for a model (scripts/smoke, scripts/bench): `smoke_passed`,
    `bench_passed` or `failed`, with the run's report in `quality_scores[validation]`."""
    if validation not in ("smoke_passed", "bench_passed", "failed"):
        raise ValueError(f"not an evidence value: {validation}")
    row = (await session.execute(sa.select(Model).where(Model.model_key == model_key))).scalar_one()
    if validation != "failed" and VALIDATION_RANK[validation] < VALIDATION_RANK.get(row.validation, 0):
        validation = row.validation  # keep the stronger evidence
    row.validation = validation
    row.quality_scores = {**dict(row.quality_scores or {}), validation: evidence}
    await session.flush()
    await session.refresh(row)  # server-side `updated_at`: callers serialize the row
    return row


async def promote_model(
    session: AsyncSession, model_id: UUID, *, note: str, license_ok: bool, require_bench: bool = False
) -> Model:
    """§24 promotion: `status: production` with `promotion_basis` from the evidence (`bench` when
    benchmarked, else `smoke`). Needs a verified license closure, `validation ≥ smoke_passed` and a
    written note (the caller stores the note in the audit log, ADR-style). Once the benchmark runner
    exists (Phase 11) a full benchmark is required: `require_bench` asks for `bench_passed`."""
    row = await session.get_one(Model, model_id)
    if not note.strip():
        raise ValueError("a promotion needs a written note")
    if not license_ok:
        raise ValueError("the license closure is not verified for production")
    if VALIDATION_RANK.get(row.validation, 0) < VALIDATION_RANK["smoke_passed"]:
        raise ValueError(f"validation is {row.validation}: promotion needs smoke_passed or better (rule 5)")
    if require_bench and row.validation != "bench_passed":
        raise ValueError(f"validation is {row.validation}: promotion needs a passed benchmark (bench_passed, §24)")
    row.status = "production"
    row.promotion_basis = "bench" if row.validation == "bench_passed" else "smoke"
    await session.flush()
    await session.refresh(row)
    return row


async def disable_model(session: AsyncSession, model_id: UUID) -> Model:
    row = await session.get_one(Model, model_id)
    row.status = "disabled"
    await session.flush()
    await session.refresh(row)
    return row


async def store_knob_calibration(
    session: AsyncSession,
    *,
    adapter_id: str,
    translator_version: str,
    revision: str,
    knob: str,
    curve: dict[str, Any],
    model_id: UUID | None = None,
    language: str = "und",
) -> None:
    """A knob → observed-effect curve (`CalibrationWorkflow`, §15.7) as a `model_behavior_profiles`
    row (`dimension = 'knob:<name>'`, `source = 'bench'`)."""
    from sqlalchemy.dialects.postgresql import insert

    stmt = insert(ModelBehaviorProfile).values(
        model_id=model_id,
        adapter_id=adapter_id,
        translator_version=translator_version,
        revision=revision,
        dimension=KNOB_DIMENSION_PREFIX + knob,
        language=language,
        source="bench",
        knob_calibration=dict(curve),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["adapter_id", "translator_version", "revision", "dimension", "language", "source"],
            set_={"knob_calibration": stmt.excluded.knob_calibration, "updated_at": sa.func.now()},
        )
    )


async def load_overlay(session: AsyncSession, *, include_mock_profiles: bool = False) -> RegistryOverlay:
    plugins = {p.id: p for p in (await session.execute(sa.select(Plugin))).scalars()}
    status: dict[str, str] = {}
    validation: dict[str, str] = {}
    basis: dict[str, str] = {}
    disabled: set[str] = set()
    for plugin in plugins.values():
        if not plugin.enabled:
            disabled.add(plugin.plugin_key)
    by_adapter: dict[str, list[Model]] = {}
    for model in (await session.execute(sa.select(Model))).scalars():
        owner = plugins.get(model.plugin_id) if model.plugin_id else None
        if owner is not None:
            by_adapter.setdefault(owner.plugin_key, []).append(model)
    # An adapter is routed as a whole: it is production only when every model it loads was
    # promoted, its validation is its weakest model's (a `failed` model fails it), and one
    # disabled model disables it (the same rules as the `plugins` row, `_plugin_status`).
    for adapter, models in by_adapter.items():
        if any(m.status == "disabled" for m in models):
            disabled.add(adapter)
        if all(m.promotion_basis is not None for m in models):
            status[adapter] = "production" if all(m.status == "production" for m in models) else models[0].status
            basis[adapter] = "smoke" if any(m.promotion_basis == "smoke" for m in models) else "bench"
        weakest = (
            "failed"
            if any(m.validation == "failed" for m in models)
            else min((m.validation for m in models), key=lambda v: VALIDATION_RANK.get(v, 0))
        )
        if VALIDATION_RANK.get(weakest, 0) > 0:
            validation[adapter] = weakest
    measured: dict[tuple[str, str], dict[str, Any]] = {}
    knobs: dict[tuple[str, str], dict[str, Any]] = {}
    for row in (await session.execute(sa.select(ModelBehaviorProfile))).scalars():
        if row.dimension.startswith(KNOB_DIMENSION_PREFIX):
            if row.knob_calibration:
                knobs[(row.adapter_id, row.dimension[len(KNOB_DIMENSION_PREFIX) :])] = dict(row.knob_calibration)
            continue
        if row.translator_version == "proxy_calibration":
            continue
        if row.source == "mock" and not include_mock_profiles:
            continue
        measured_data = dict(row.measured or {})
        if "success_rate" in measured_data:
            key = (row.adapter_id, row.dimension)
            previous = measured.get(key)
            if previous is None or int(measured_data.get("n", 0)) > int(previous.get("n", 0)):
                measured[key] = {**measured_data, "source": row.source}
    return RegistryOverlay(
        status=status,
        validation=validation,
        promotion_basis=basis,
        disabled=frozenset(disabled),
        measured=measured,
        knobs=knobs,
    )
