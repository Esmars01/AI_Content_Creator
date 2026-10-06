"""Capability interfaces, request/response models, plugin manifest, behavior-matrix schema and the
plugin loader (§23, §24). Importable on Python 3.10 (GPU workers, §7).

Status: implemented and tested in Phase 2 — every §23 capability with its request/result models
and closed feature vocabulary, `Adapter`/`BehaviorTranslator` with `run()`, the behavior matrix,
the manifest schema with license closure, and entry-point discovery with manifest validation.
"""

from ce_contracts.behavior import BehaviorDirectives, BehaviorMatrix, DimensionControl, KnobSpec
from ce_contracts.capabilities import CAPABILITIES, INTERFACE_ONLY, Capability, capability
from ce_contracts.common import (
    ArtifactRef,
    CancellationToken,
    Cancelled,
    ContractModel,
    Estimate,
    HardwareInfo,
    HealthStatus,
    LoadContext,
    RunContext,
)
from ce_contracts.interfaces import Adapter, AdapterBase, BehaviorTranslator
from ce_contracts.manifest import LicenseBlock, PluginManifest, license_closure
from ce_contracts.plugins import ENTRY_POINT_GROUP, LoadedPlugin, PluginLoadError, PluginRegistry, discover

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2

__all__ = [
    "CAPABILITIES",
    "ENTRY_POINT_GROUP",
    "INTERFACE_ONLY",
    "Adapter",
    "AdapterBase",
    "ArtifactRef",
    "BehaviorDirectives",
    "BehaviorMatrix",
    "BehaviorTranslator",
    "CancellationToken",
    "Cancelled",
    "Capability",
    "ContractModel",
    "DimensionControl",
    "Estimate",
    "HardwareInfo",
    "HealthStatus",
    "KnobSpec",
    "LicenseBlock",
    "LoadContext",
    "LoadedPlugin",
    "PluginLoadError",
    "PluginManifest",
    "PluginRegistry",
    "RunContext",
    "capability",
    "discover",
    "license_closure",
]
