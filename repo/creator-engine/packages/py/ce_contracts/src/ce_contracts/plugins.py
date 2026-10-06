"""Plugin discovery and loading (§24).

Plugins are Python packages registered in the entry-point group `creator_engine.plugins`. The
entry point's value is the module that contains the plugin's `plugin.yaml`; the manifest's
`entrypoint` names the adapter class, imported only when an adapter is first needed (so a
service can list and route plugins whose heavy dependencies it does not have installed).

`discover()` validates every manifest and refuses a plugin when its manifest is invalid, when a
model or dependency has no license block, when two plugins share an id, or when its declared
family or environment does not match the loading service. Refusals are kept with their reason
(`registry.rejected`) so they can be reported, never silently dropped.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.resources
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ce_contracts.interfaces import BehaviorTranslator
from ce_contracts.manifest import PluginManifest

__all__ = [
    "ENTRY_POINT_GROUP",
    "LoadedPlugin",
    "PluginLoadError",
    "PluginRegistry",
    "Rejected",
    "cpu_engine_errors",
    "cpu_unavailable",
    "discover",
    "import_object",
    "load_manifest_text",
    "safe_load_yaml",
]

ENTRY_POINT_GROUP = "creator_engine.plugins"
MANIFEST_NAME = "plugin.yaml"


class PluginLoadError(ValueError):
    """A plugin cannot be loaded (invalid manifest, missing license block, import failure)."""


class _Yaml12Loader(yaml.SafeLoader):
    """YAML 1.2 booleans only (`on`/`off` stay strings), as in ce_core.yamlio (DECISIONS D15)."""


_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def safe_load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_Yaml12Loader)  # noqa: S506 - derives from SafeLoader


def load_manifest_text(text: str, *, source: str = "<string>") -> PluginManifest:
    try:
        data = safe_load_yaml(text)
    except yaml.YAMLError as exc:
        raise PluginLoadError(f"{source}: cannot parse plugin.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginLoadError(f"{source}: plugin.yaml must be a mapping")
    _require_license_blocks(data, source)
    try:
        return PluginManifest.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = "/".join(str(x) for x in first["loc"])
        raise PluginLoadError(f"{source}: invalid manifest at {loc or '<root>'}: {first['msg']}") from exc


def _require_license_blocks(data: dict[str, Any], source: str) -> None:
    """A clear refusal (rather than a schema error) when a model or a dependency has no license."""
    for model in data.get("models") or []:
        key = model.get("key", "?") if isinstance(model, dict) else "?"
        if not isinstance(model, dict) or not isinstance(model.get("license"), dict):
            raise PluginLoadError(f"{source}: model {key} has no license block (ADR 0012)")
        for dep in model.get("dependencies") or []:
            if not isinstance(dep, dict) or not isinstance(dep.get("license"), dict):
                ref = dep.get("ref", "?") if isinstance(dep, dict) else "?"
                raise PluginLoadError(f"{source}: dependency {ref} of model {key} has no license block (ADR 0012)")


def import_object(path: str) -> Any:
    """The object a `package.module:Name` path names (entrypoints, translators, test backends)."""
    module_name, _, attr = path.partition(":")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError:
        raise PluginLoadError(f"{path}: {module_name} has no attribute {attr}") from None


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    source: str  # the module holding plugin.yaml, or a filesystem path
    _adapter: Any = field(default=None, repr=False)
    _translator: Any = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.manifest.id

    def adapter(self) -> Any:
        """The adapter instance (one per process), imported on first use."""
        if self._adapter is None:
            cls = import_object(self.manifest.entrypoint)
            self._adapter = cls(self.manifest)
        return self._adapter

    def entrypoint(self) -> Any:
        """The object `entrypoint` names (a provider factory, or the adapter class), imported on demand."""
        return import_object(self.manifest.entrypoint)

    def translator(self) -> BehaviorTranslator | None:
        if self.manifest.behavior_translator is None:
            return None
        if self._translator is None:
            cls = import_object(self.manifest.behavior_translator)
            translator = cls()
            if not isinstance(translator, BehaviorTranslator):
                raise PluginLoadError(f"{self.id}: {self.manifest.behavior_translator} is not a BehaviorTranslator")
            self._translator = translator
        result: BehaviorTranslator = self._translator
        return result


@dataclass(frozen=True)
class Rejected:
    name: str
    source: str
    reason: str


@dataclass
class PluginRegistry:
    plugins: dict[str, LoadedPlugin] = field(default_factory=dict)
    rejected: list[Rejected] = field(default_factory=list)

    def get(self, plugin_id: str) -> LoadedPlugin:
        try:
            return self.plugins[plugin_id]
        except KeyError:
            raise KeyError(f"plugin {plugin_id!r} is not loaded") from None

    def by_capability(self, capability_id: str) -> list[LoadedPlugin]:
        return [p for p in self.plugins.values() if p.manifest.capability(capability_id) is not None]

    def providers(self, kind: str) -> dict[str, LoadedPlugin]:
        return {
            p.manifest.provider.key: p
            for p in self.plugins.values()
            if p.manifest.provider is not None and p.manifest.provider.kind == kind
        }

    def manifests(self) -> list[PluginManifest]:
        return [p.manifest for p in sorted(self.plugins.values(), key=lambda p: p.id)]

    def add(self, plugin: LoadedPlugin) -> None:
        if plugin.id in self.plugins:
            raise PluginLoadError(
                f"duplicate plugin id {plugin.id!r} ({self.plugins[plugin.id].source}, {plugin.source})"
            )
        self.plugins[plugin.id] = plugin


def _entry_points() -> list[importlib.metadata.EntryPoint]:
    return sorted(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: ep.name)


def discover(
    *,
    app_env: str | None,
    include_mocks: bool,
    families: Iterable[str] | None = None,
    extra_manifests: Iterable[Path] = (),
    dimension_names: Iterable[str] | None = None,
) -> PluginRegistry:
    """Loads every installed plugin manifest and keeps those this service may use.

    - `app_env`: drop plugins whose `allowed_envs` exclude it (None = keep all);
    - `families`: runtime families this process executes (None = every family; the router and
      the API see all plugins, a worker only its own family);
    - `include_mocks`: `MOCK_GPU=true` registers the mock adapters and the mock GPU provider;
    - `dimension_names`: the requestable dimensions of the vocabulary; behavior matrices may only
      use those (checked when given);
    - `extra_manifests`: plugin.yaml files outside entry points (tests, manifest-only fixtures).
    """
    registry = PluginRegistry()
    allowed_families = set(families) if families is not None else None
    dims = set(dimension_names) if dimension_names is not None else None
    sources: list[tuple[str, str, str]] = []  # (name, source, text)
    for ep in _entry_points():
        try:
            text = (importlib.resources.files(ep.value) / MANIFEST_NAME).read_text(encoding="utf-8")
        except (ModuleNotFoundError, FileNotFoundError, TypeError) as exc:
            registry.rejected.append(Rejected(ep.name, ep.value, f"no {MANIFEST_NAME}: {exc}"))
            continue
        sources.append((ep.name, ep.value, text))
    for path in extra_manifests:
        sources.append((path.parent.name, str(path), Path(path).read_text(encoding="utf-8")))

    for name, source, text in sources:
        try:
            manifest = load_manifest_text(text, source=source)
            if manifest.id != name and not source.endswith(MANIFEST_NAME):
                raise PluginLoadError(f"{source}: entry point name {name!r} differs from manifest id {manifest.id!r}")
            if dims is not None and manifest.behavior_matrix is not None:
                unknown = sorted(set(manifest.behavior_matrix.dimensions) - dims)
                if unknown:
                    raise PluginLoadError(f"{source}: behavior_matrix uses unknown dimensions {unknown}")
        except PluginLoadError as exc:
            registry.rejected.append(Rejected(name, source, str(exc)))
            continue
        if manifest.mock and not include_mocks:
            continue
        if app_env is not None and app_env not in manifest.allowed_envs:
            continue
        if allowed_families is not None and manifest.runtime.family not in allowed_families:
            continue
        try:
            registry.add(LoadedPlugin(manifest, source))
        except PluginLoadError as exc:
            registry.rejected.append(Rejected(name, source, str(exc)))
    return registry


def cpu_unavailable(registry: PluginRegistry, cpu_real_engines: str, model_cache_dir: str | None) -> dict[str, str]:
    """Real CPU engines in `registry` that cannot be used, with the reason (§35 `CPU_REAL_ENGINES`)."""
    from ce_contracts.manifest import cpu_engine_unavailable

    out: dict[str, str] = {}
    for plugin in registry.plugins.values():
        reason = cpu_engine_unavailable(plugin.manifest, cpu_real_engines, model_cache_dir)
        if reason is not None:
            out[plugin.id] = reason
    return out


def cpu_engine_errors(registry: PluginRegistry, cpu_real_engines: str, model_cache_dir: str | None) -> list[str]:
    """Startup errors: with `CPU_REAL_ENGINES=on` every real CPU engine must have its assets."""
    if cpu_real_engines != "on":
        return []
    return [f"{k}: {v}" for k, v in sorted(cpu_unavailable(registry, "on", model_cache_dir).items())]
