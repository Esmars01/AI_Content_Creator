"""The fleet's GPU providers (§25, Phase 9): the registered `GPUProvider` plugins configured from
their manifest defaults and the `gpu_providers` rows an administrator manages.

- A row's `kind` names a registered provider key; its `config` overrides the manifest defaults (never
  secrets), and its `credentials_ref` says where the secret comes from: `env:NAME` (an environment
  variable of the scheduler) or `file:/path` (a mounted secret). The secret is passed to the plugin
  as `api_key` and never stored in the database, logged or returned by the API.
- Paid providers (their manifest declares `allow_paid`) are used only through an enabled row, and
  the plugin itself refuses to provision until that row sets `allow_paid: true` — the owner's spend
  approval (§41). Free providers (the mock and the third-party stub in dev/test, `local`,
  `local_docker`) are available without a row; a disabled row switches one off.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_contracts.plugins import discover
from ce_db.models.platform import GpuProvider
from ce_db.session import Database
from ce_gpu.provider import GPUProvider, create_gpu_provider
from ce_obs import get_logger

__all__ = ["FleetProvider", "is_paid", "load_providers", "resolve_credentials"]

_log = get_logger("ce.scheduler.providers")


class CredentialsError(ValueError):
    """A credentials reference that cannot be resolved (the message never contains the secret)."""


def resolve_credentials(ref: str | None, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`env:NAME` or `file:/path` → `{"api_key": value}`; no reference → `{}`."""
    if not ref:
        return {}
    scheme, _, target = ref.partition(":")
    env = os.environ if environ is None else environ
    if scheme == "env" and target:
        value = env.get(target, "")
        if not value:
            raise CredentialsError(f"credentials reference {ref}: the variable is not set")
        return {"api_key": value}
    if scheme == "file" and target:
        try:
            value = Path(target).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CredentialsError(f"credentials reference {ref}: {type(exc).__name__}") from None
        if not value:
            raise CredentialsError(f"credentials reference {ref}: the file is empty")
        return {"api_key": value}
    raise CredentialsError(f"credentials reference {ref!r}: use env:NAME or file:/path")


def is_paid(key: str, *, app_env: str | None, include_mocks: bool) -> bool:
    plugin = discover(app_env=app_env, include_mocks=include_mocks).providers("gpu").get(key)
    return plugin is not None and "allow_paid" in plugin.manifest.defaults


@dataclass
class FleetProvider:
    """A configured provider: the plugin instance, its `gpu_providers` row (if any) and its daily cap."""

    key: str
    provider: GPUProvider
    row_id: UUID | None = None
    name: str | None = None
    budget_daily_usd: float | None = None
    regions: list[str] = field(default_factory=list)
    paid: bool = False
    fingerprint: str = ""  # the configuration it was built from (a reload keeps an unchanged instance)


def _rows_by_kind(rows: Iterable[GpuProvider]) -> dict[str, GpuProvider]:
    """One row per kind drives the fleet: the first enabled one (by name), else the first."""
    out: dict[str, GpuProvider] = {}
    for row in sorted(rows, key=lambda r: (not r.enabled, r.name)):
        out.setdefault(row.kind, row)
    return out


async def load_providers(
    db: Database | None,
    *,
    app_env: str | None,
    include_mocks: bool,
    overrides: Mapping[str, dict[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
    previous: Mapping[str, FleetProvider] | None = None,
) -> dict[str, FleetProvider]:
    """Builds every usable provider. A provider that fails to configure (bad reference, missing
    setting) is skipped with a warning naming the reason — never the secret. With `previous`, a
    provider whose configuration did not change keeps its instance (and its in-flight state)."""
    registered = discover(app_env=app_env, include_mocks=include_mocks).providers("gpu")
    rows: dict[str, GpuProvider] = {}
    if db is not None:
        with contextlib.suppress(Exception):  # the table may not exist before migrations (fresh dev DB)
            async with db.session() as session:
                rows = _rows_by_kind((await session.execute(sa.select(GpuProvider))).scalars())
    out: dict[str, FleetProvider] = {}
    for key in sorted(registered):
        paid = "allow_paid" in registered[key].manifest.defaults
        row = rows.get(key)
        if row is not None and not row.enabled:
            continue
        if row is None and paid:
            continue  # a paid provider needs an administrator-created, enabled row
        try:
            config: dict[str, Any] = {**(dict(row.config or {}) if row else {}), **dict((overrides or {}).get(key, {}))}
            if row is not None:
                config.update(resolve_credentials(row.credentials_ref, environ=environ))
            fingerprint = hashlib.sha256(
                json.dumps([str(row.id) if row else None, config], sort_keys=True, default=repr).encode()
            ).hexdigest()
            kept = (previous or {}).get(key)
            if kept is not None and kept.fingerprint == fingerprint:
                kept.budget_daily_usd = (
                    float(row.budget_daily_usd) if row and row.budget_daily_usd is not None else None
                )
                out[key] = kept
                continue
            provider = create_gpu_provider(key, app_env=app_env, include_mocks=include_mocks, config=config)
        except Exception as exc:
            _log.warning("GPU provider skipped", provider=key, error=str(exc)[:300])
            continue
        out[key] = FleetProvider(
            key=key,
            provider=provider,
            row_id=row.id if row else None,
            name=row.name if row else None,
            budget_daily_usd=float(row.budget_daily_usd) if row and row.budget_daily_usd is not None else None,
            regions=list(row.regions or []) if row else [],
            paid=paid,
            fingerprint=fingerprint,
        )
    return out
