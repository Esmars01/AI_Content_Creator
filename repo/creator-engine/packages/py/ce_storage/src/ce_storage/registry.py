"""Provider registry: `STORAGE_PROVIDER` names the provider; services call `create_storage(settings)`.

Providers are plugins (`plugins/providers/storage/*`, kind `provider`, provider kind `storage`)
discovered through the `creator_engine.plugins` entry points (§24, ADR 0030). The manifest's
`entrypoint` is a factory `create(settings) -> StorageProvider`. `register_provider` adds a
factory directly (tests, embedding applications).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ce_contracts.plugins import discover

from ce_storage.base import StorageProvider

__all__ = ["StorageSettings", "create_storage", "provider_names", "register_provider"]


class _Secret(Protocol):
    def get_secret_value(self) -> str: ...


class StorageSettings(Protocol):
    """The subset of `ce_config.settings.Settings` the providers read."""

    @property
    def app_env(self) -> str: ...
    @property
    def storage_provider(self) -> str: ...
    @property
    def s3_endpoint_url(self) -> str: ...
    @property
    def s3_public_endpoint_url(self) -> str | None: ...
    @property
    def s3_region(self) -> str: ...
    @property
    def s3_access_key_id(self) -> _Secret: ...
    @property
    def s3_secret_access_key(self) -> _Secret: ...
    @property
    def local_storage_root(self) -> str: ...
    @property
    def api_base_url(self) -> str: ...
    @property
    def secret_key(self) -> _Secret: ...


Factory = Callable[[StorageSettings], StorageProvider]

_EXTRA: dict[str, Factory] = {}


def register_provider(name: str, factory: Factory) -> None:
    if name in _EXTRA or name in _plugin_factories(None):
        raise ValueError(f"storage provider {name!r} is already registered")
    _EXTRA[name] = factory


def _plugin_factories(app_env: str | None) -> dict[str, Factory]:
    registry = discover(app_env=app_env, include_mocks=False)
    factories: dict[str, Factory] = {}
    for key, plugin in registry.providers("storage").items():
        factories[key] = plugin.entrypoint()
    return factories


def provider_names(app_env: str | None = None) -> list[str]:
    return sorted({*_plugin_factories(app_env), *_EXTRA})


def create_storage(settings: StorageSettings) -> StorageProvider:
    factories = {**_plugin_factories(settings.app_env), **_EXTRA}
    try:
        factory = factories[settings.storage_provider]
    except KeyError:
        raise ValueError(
            f"unknown STORAGE_PROVIDER {settings.storage_provider!r} for APP_ENV={settings.app_env}; "
            f"known: {sorted(factories)}"
        ) from None
    provider = factory(settings)
    if not isinstance(provider, StorageProvider):
        raise TypeError(f"storage provider {settings.storage_provider!r} did not return a StorageProvider")
    return provider
