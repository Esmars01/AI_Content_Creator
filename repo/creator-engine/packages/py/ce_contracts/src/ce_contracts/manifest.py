"""The plugin manifest (`plugin.yaml`, §24) and its validation.

A plugin refuses to load when its manifest is invalid or when any model or dependency lacks a
license block (ADR 0012): the license closure — a model plus every weight it depends on — is what
`ce_policy.license.evaluate` checks against the operator profile.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ce_contracts.behavior import BehaviorMatrix, KnobSpec
from ce_contracts.capabilities import BEHAVIOR_ABILITY_NAMES, CAPABILITIES
from ce_contracts.common import PLUGIN_STATUSES, RUNTIME_FAMILIES, VALIDATIONS, ContractModel

__all__ = [
    "LICENSE_CONDITIONS",
    "CapabilityDecl",
    "LanguageDecl",
    "LicenseBlock",
    "ModelDecl",
    "ModelDependency",
    "ModelSource",
    "Obligation",
    "PluginManifest",
    "ProviderDecl",
    "RuntimeDecl",
    "cpu_engine_unavailable",
    "license_closure",
    "missing_assets",
]

CPU_ENGINE_MODES = ("auto", "on", "off")

LICENSE_CONDITIONS = (
    "revenue_cap_usd",
    "mau_cap",
    "territories_excluded",
    "attribution_text",
    "maas_restricted",
    "non_commercial",
    "copyleft",  # e.g. "GPL-3.0-or-later": distribution carries the copyleft terms (source offer)
    "use_restrictions",  # e.g. "CreativeML OpenRAIL-M Attachment A": use-based restrictions passed on to users
)
_ID = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*(?:\.[a-z0-9]+(?:_[a-z0-9]+)*)?$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class LicenseBlock(ContractModel):
    """§24 license block. `verified_at`/`verified_by` record who read the LICENSE at the pinned revision."""

    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    commercial_use: bool
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    verified_at: date
    verified_by: str = Field(min_length=1)
    text_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the license text read at the pinned revision (scripts/verify_licenses.py re-checks it)",
    )
    evidence: str = Field(
        default="",
        description="where the license was read: `license_file` (exact text at the pin), `model_card` (card metadata "
        "at the pin), or a short note when the source is ambiguous",
    )

    @field_validator("conditions")
    @classmethod
    def _known_conditions(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for entry in value:
            unknown = set(entry) - set(LICENSE_CONDITIONS)
            if unknown or len(entry) != 1:
                raise ValueError(f"each condition is one of {LICENSE_CONDITIONS}, got {sorted(entry)}")
        return value

    def condition(self, name: str) -> Any:
        for entry in self.conditions:
            if name in entry:
                return entry[name]
        return None


class ModelSource(ContractModel):
    type: Literal["huggingface", "s3", "url", "builtin", "git"]
    repo: str | None = None
    revision: str = Field(min_length=1)
    uri: str | None = None


class ModelDependency(ContractModel):
    """A weight, code base or package the model depends on. Every dependency carries a license block
    (the license closure, ADR 0012); weights the worker must download also carry a `source` and the
    `files` to fetch (the model cache resolves them with the model, §25)."""

    ref: str = Field(min_length=1, description="repo@revision of a weight the model depends on")
    license: LicenseBlock
    role: str = Field(default="", description="what the dependency is to the model (base_weights, audio_encoder, …)")
    source: ModelSource | None = None
    files: list[str] = Field(default_factory=list)
    sha256: dict[str, str] = Field(default_factory=dict)
    size_gb: float = Field(default=0.0, ge=0)

    @property
    def fetchable(self) -> bool:
        return self.source is not None and self.source.type in ("huggingface", "s3", "url")


class ModelDecl(ContractModel):
    key: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = ""
    source: ModelSource
    files: list[str] = Field(default_factory=list)
    sha256: dict[str, str] = Field(default_factory=dict)
    size_gb: float = Field(default=0.0, ge=0)
    license: LicenseBlock
    dependencies: list[ModelDependency] = Field(default_factory=list)


class LanguageDecl(ContractModel):
    """`validated` = by the model authors [RV]; our own evidence is the `validation` field."""

    mode: Literal["text", "audio_driven", "any"] = "any"
    validated: list[str] = Field(default_factory=list)
    unvalidated: list[str] = Field(default_factory=list)


class CapabilityDecl(ContractModel):
    id: str
    features: list[str] = Field(default_factory=list)
    languages: LanguageDecl = Field(default_factory=LanguageDecl)
    resolutions: list[str] = Field(default_factory=list, description="e.g. 480p, 720p, 1080p; empty = any")
    max_duration_s: float | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _known_capability_and_features(self) -> CapabilityDecl:
        spec = CAPABILITIES.get(self.id)
        if spec is None:
            raise ValueError(f"unknown capability {self.id!r}")
        abilities = sorted(set(self.features) & BEHAVIOR_ABILITY_NAMES)
        if abilities:
            raise ValueError(f"{self.id}: {abilities} are behavior abilities; declare them in behavior_matrix (§23)")
        unknown = sorted(set(self.features) - spec.features)
        if unknown:
            raise ValueError(f"{self.id}: unknown features {unknown}; allowed {sorted(spec.features)}")
        return self

    def max_height(self) -> int | None:
        heights = [int(r.rstrip("p")) for r in self.resolutions if r.rstrip("p").isdigit()]
        return max(heights) if heights else None


class RuntimeDecl(ContractModel):
    family: str
    python: str = "3.12"
    cuda: str | None = None
    requires_gpu: bool = False
    min_vram_gb: float = Field(default=0.0, ge=0)
    recommended_vram_gb: float = Field(default=0.0, ge=0)
    supports_cpu: bool = True

    @field_validator("family")
    @classmethod
    def _known_family(cls, value: str) -> str:
        if value not in RUNTIME_FAMILIES:
            raise ValueError(f"unknown runtime family {value!r}")
        return value


class ProviderDecl(ContractModel):
    """For `kind: provider`: which provider registry this plugin registers into, under which key."""

    kind: Literal["gpu", "storage", "llm", "kms"]
    key: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")


class Obligation(ContractModel):
    text: str
    placement: list[Literal["ui", "docs", "export_metadata"]] = Field(default_factory=list)


AppEnvName = Literal["dev", "test", "prod"]
ALL_ENVS: tuple[AppEnvName, ...] = ("dev", "test", "prod")


class PluginManifest(ContractModel):
    id: str
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: Literal["model_adapter", "analyzer", "code", "provider"]
    entrypoint: str
    behavior_translator: str | None = None
    test_backend: str | None = Field(
        default=None,
        description="`package.module:factory` of a CPU stand-in for the engine's heavy calls; the contract suite "
        "uses it for GPU adapters when no GPU is present (§37: contract tests with heavy calls mocked)",
    )
    mock: bool = Field(default=False, description="registered only when MOCK_GPU=true (§10.6)")
    capabilities: list[CapabilityDecl] = Field(default_factory=list)
    behavior_matrix: BehaviorMatrix | None = None
    knobs: dict[str, KnobSpec] = Field(default_factory=dict)
    runtime: RuntimeDecl
    models: list[ModelDecl] = Field(default_factory=list)
    provider: ProviderDecl | None = None
    schemas: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    pricing: dict[str, float] = Field(
        default_factory=dict, description="estimation hints, e.g. seconds_per_output_second"
    )
    allowed_envs: list[AppEnvName] = Field(default_factory=lambda: list(ALL_ENVS))
    status: str = "sandbox"
    validation: str = "untested_on_gpu"
    maturity_notes: str = ""
    obligations: list[Obligation] = Field(default_factory=list)
    assets: list[str] = Field(
        default_factory=list,
        description="files under MODEL_CACHE_DIR the adapter needs (`make fetch-cpu-assets`, scripts/cpu_assets.yaml); "
        "a real CPU engine is used only when they are present (CPU_REAL_ENGINES=auto)",
    )

    @property
    def cpu_engine(self) -> bool:
        """A real (non-mock) model adapter or analyzer that runs on CPU (§35 `CPU_REAL_ENGINES`): used
        when the setting is `on`, or `auto` with its `assets` present; mocks serve it otherwise."""
        return (
            not self.mock
            and self.kind in ("model_adapter", "analyzer")
            and self.runtime.family in ("cpu_model", "cpu_inproc")
        )

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not _ID.match(value) or len(value) > 64:
            raise ValueError(f"plugin id {value!r} must be lowercase words (optionally category.name)")
        return value

    @field_validator("entrypoint", "behavior_translator", "test_backend")
    @classmethod
    def _entrypoint_shape(cls, value: str | None) -> str | None:
        if value is not None and not _ENTRYPOINT.match(value):
            raise ValueError(f"{value!r} must be 'package.module:ClassName'")
        return value

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        if value not in PLUGIN_STATUSES:
            raise ValueError(f"status must be one of {PLUGIN_STATUSES}")
        return value

    @field_validator("validation")
    @classmethod
    def _validation(cls, value: str) -> str:
        if value not in VALIDATIONS:
            raise ValueError(f"validation must be one of {VALIDATIONS}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> PluginManifest:
        ids = [c.id for c in self.capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("a capability is declared twice")
        if self.kind == "provider":
            if self.provider is None:
                raise ValueError("kind provider needs a `provider` block")
            if self.capabilities:
                raise ValueError("providers declare no capabilities")
        elif not self.capabilities:
            raise ValueError(f"kind {self.kind} needs at least one capability")
        if self.kind == "model_adapter" and not self.models:
            raise ValueError("a model adapter needs at least one model with a license block (ADR 0012)")
        behavior = [c for c in self.capabilities if CAPABILITIES[c.id].behavior_capable]
        if behavior and self.kind == "model_adapter":
            if self.behavior_matrix is None:
                raise ValueError(f"{behavior[0].id} is behavior-capable: declare a behavior_matrix (§23)")
            if self.behavior_translator is None:
                raise ValueError("behavior-capable plugins ship a BehaviorTranslator (§15.7)")
        if self.behavior_matrix is not None:
            for dim, control in self.behavior_matrix.dimensions.items():
                missing = sorted(set(control.knobs) - set(self.knobs))
                if missing:
                    raise ValueError(f"behavior_matrix.{dim} uses undeclared knobs {missing}")
        if self.runtime.family == "cpu_inproc" and self.runtime.requires_gpu:
            raise ValueError("cpu_inproc plugins cannot require a GPU")
        return self

    # ------------------------------------------------------------------ helpers
    def capability(self, capability_id: str) -> CapabilityDecl | None:
        for decl in self.capabilities:
            if decl.id == capability_id:
                return decl
        return None

    @property
    def primary_model(self) -> ModelDecl | None:
        return self.models[0] if self.models else None


def license_closure(manifest: PluginManifest, model_key: str | None = None) -> list[tuple[str, LicenseBlock]]:
    """The model (or every model) and every weight it depends on, as (ref, license) pairs."""
    out: list[tuple[str, LicenseBlock]] = []
    for model in manifest.models:
        if model_key is not None and model.key != model_key:
            continue
        out.append((f"{model.key}@{model.source.revision}", model.license))
        out += [(dep.ref, dep.license) for dep in model.dependencies]
    return out


def missing_assets(manifest: PluginManifest, model_cache_dir: str | Path | None) -> list[str]:
    """The manifest's `assets` that are not present (non-empty) under `model_cache_dir`."""
    if not manifest.assets:
        return []
    if model_cache_dir is None:
        return list(manifest.assets)
    root = Path(model_cache_dir)
    return [a for a in manifest.assets if not ((root / a).is_file() and (root / a).stat().st_size > 0)]


def cpu_engine_unavailable(manifest: PluginManifest, mode: str, model_cache_dir: str | Path | None) -> str | None:
    """Why a real CPU engine cannot be used under `CPU_REAL_ENGINES=mode` (None = usable; also None
    for anything that is not a CPU engine). `auto` and `on` use it when its assets are present;
    with `on` a missing asset is a startup error (`check_cpu_engines`)."""
    if not manifest.cpu_engine:
        return None
    if mode not in CPU_ENGINE_MODES:
        raise ValueError(f"CPU_REAL_ENGINES must be one of {CPU_ENGINE_MODES}, not {mode!r}")
    if mode == "off":
        return "CPU_REAL_ENGINES=off"
    missing = missing_assets(manifest, model_cache_dir)
    if missing:
        shown = ", ".join(missing[:3]) + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
        return f"assets missing under MODEL_CACHE_DIR: {shown} (make fetch-cpu-assets)"
    return None
