"""Model-registry test helpers: copies of installed manifests under unique ids, so tests on the
shared session database (`models` is a platform table) never touch the installed rows."""

from __future__ import annotations

import secrets
from typing import Any

from ce_contracts.plugins import discover

__all__ = ["manifest_copy"]


def manifest_copy(source_id: str, *, license_update: dict[str, Any] | None = None, **update: Any) -> Any:
    """The installed manifest `source_id` under `<id>_<hex>`, its model keys suffixed alike;
    `license_update` replaces fields of every model's license block, `update` manifest fields."""
    manifest = discover(app_env=None, include_mocks=True).get(source_id).manifest
    tag = secrets.token_hex(4)
    models = []
    for decl in manifest.models:
        changes: dict[str, Any] = {"key": f"{decl.key}-{tag}"}
        if license_update:
            changes["license"] = decl.license.model_copy(update=license_update)
        models.append(decl.model_copy(update=changes))
    return manifest.model_copy(update={"id": f"{manifest.id}_{tag}", "models": models, **update})
