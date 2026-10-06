"""Process-wide services of build execution (orchestrator and render worker) and per-version caches.

Activities receive ids only (§7); everything else is loaded here: the version's spec and
references (immutable once approved, so cached per version), the execution graph document (by
sha256), node output documents (by sha256), the router catalog and the in-process plugin registry
(`cpu_inproc` adapters: observer, QC metrics, captions, provenance, effects, translation).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_build.refs import BuildRefs
from ce_config.loader import ConfigBundle
from ce_config.settings import EffectiveConfig
from ce_contracts.common import LoadContext
from ce_contracts.plugins import PluginRegistry, discover
from ce_core.build import ExecutionGraph
from ce_core.spec.videospec import VideoSpec
from ce_db.models.videos import Video, VideoVersion
from ce_db.session import Database
from ce_llm import provider_from_settings
from ce_obs import get_logger
from ce_obs.events import EventBus, EventType
from ce_router import RouterCatalog, build_catalog, check_cpu_engines, operator_from_config
from ce_storage import StorageProvider, create_storage
from ce_storage.content import ContentStore
from redis.asyncio import Redis

from ce_exec.outputs import DocCache
from ce_exec.refs_loader import load_refs

__all__ = ["ExecServices", "VersionData", "build_services"]

_log = get_logger("ce.exec")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class VersionData:
    org_id: UUID
    version_id: UUID
    video_id: UUID
    project_id: UUID
    parent_version_id: UUID | None
    state: str
    spec: VideoSpec
    refs: BuildRefs
    catalog: RouterCatalog | None = None  # the router catalog pinned to this version (registry snapshot)


@dataclass
class ExecServices:
    effective: EffectiveConfig
    db: Database
    storage: StorageProvider
    content: ContentStore
    docs: DocCache
    registry: PluginRegistry
    catalog: RouterCatalog
    events: EventBus | None
    scratch_root: Path
    clock: Callable[[], datetime] = _utcnow
    catalog_factory: Callable[[Any], RouterCatalog] | None = None  # rebuilds the catalog with a registry overlay
    registry_digest: str | None = None  # the overlay applied to the catalog (ce_db.registry)
    registry_overlay: Any = None  # that overlay (None until `reload_catalog` ran: tests, CPU tools)
    registry_loaded_at: float = 0.0  # monotonic time of the last overlay load
    # Tests only: an httpx transport and a DNS resolver for the SSRF-guarded fetcher (research
    # ingestion); production always uses the real network path and the system resolver.
    fetch_transport: Any = None
    fetch_resolver: Any = None
    _snapshot_catalogs: dict[str, RouterCatalog] = field(default_factory=dict)
    _speeds: dict[str, float] = field(default_factory=dict)  # measured seconds per work unit (p50)
    _speeds_at: float = -1e9
    _versions: dict[UUID, VersionData] = field(default_factory=dict)
    _graphs: dict[str, ExecutionGraph] = field(default_factory=dict)
    _loaded: set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def bundle(self) -> ConfigBundle:
        return self.effective.bundle

    @property
    def settings(self) -> Any:
        return self.effective.settings

    @property
    def provenance_mode(self) -> str:
        return self.effective.provenance_mode

    def plugin_settings(self) -> dict[str, Any]:
        """Service settings a plugin may need at load (§35): the C2PA signing certificate and key,
        and the LLM provider factory for adapters that call the configured LLM."""
        out: dict[str, Any] = {}
        for name in ("c2pa_signing_cert_path", "c2pa_signing_key_path"):
            value = getattr(self.settings, name, None)
            if value:
                out[name] = value
        # captions_llm (Phase 12): the configured LLM provider, created when the adapter loads
        # (secrets stay in the settings), and the translation limits
        settings = self.settings
        out["llm_factory"] = lambda: provider_from_settings(settings)
        out["captions_translation"] = self.bundle.app.captions.model_dump(mode="json")
        return out

    async def version(self, org_id: UUID, version_id: UUID, *, fresh: bool = False) -> VersionData:
        cached = self._versions.get(version_id)
        if cached is not None and not fresh and cached.org_id == org_id:
            return cached
        async with self.db.session() as session:
            row = (
                await session.execute(
                    sa.select(VideoVersion, Video.project_id)
                    .join(Video, sa.and_(Video.org_id == VideoVersion.org_id, Video.id == VideoVersion.video_id))
                    .where(VideoVersion.org_id == org_id, VideoVersion.id == version_id)
                )
            ).one_or_none()
            if row is None:
                raise LookupError(f"version {version_id} not found")
            version, project_id = row
            spec = VideoSpec.model_validate(version.spec)
            refs = await load_refs(session, org_id, spec)
        catalog = await self.version_catalog(org_id, version.id)
        data = VersionData(
            org_id, version.id, version.video_id, project_id, version.parent_version_id, version.state, spec, refs,
            catalog,
        )  # fmt: skip
        self._versions[version_id] = data
        return data

    # ------------------------------------------------------------------ registry snapshots (I5)
    def _snapshot_key(self, org_id: UUID, version_id: UUID) -> str:
        return f"registry-overlays/{org_id}/{version_id}.json"

    async def version_catalog(self, org_id: UUID, version_id: UUID) -> RouterCatalog:
        """The router catalog a version plans and builds with (I5, deterministic builds).

        The registry overlay (promotions, evidence, disabled adapters, measured profiles, calibrated
        knobs) is pinned per version: the first plan of a version stores the overlay in effect as a
        snapshot document, and every later plan or node run of that version (resume, previz →
        generation, re-renders) uses the snapshot, whatever was promoted or calibrated since. New
        versions (edits, new videos) start from the live registry; their clean nodes keep the
        parent's pinned routes (§12.4). Without a loaded overlay (tests and tools that set the
        catalog directly) the live catalog is used and nothing is stored."""
        if self.catalog_factory is None or self.registry_overlay is None:
            return self.catalog
        import json

        bucket = self.settings.s3_bucket_artifacts
        key = self._snapshot_key(org_id, version_id)
        if await self.storage.exists(bucket, key):
            from ce_db.registry import RegistryOverlay

            overlay = RegistryOverlay.from_json(json.loads(await self.storage.get(bucket, key)))
            if overlay.digest == self.registry_digest:
                return self.catalog
            cached = self._snapshot_catalogs.get(overlay.digest)
            if cached is None:
                cached = self.catalog_factory(overlay)
                self._snapshot_catalogs[overlay.digest] = cached
                if len(self._snapshot_catalogs) > 32:
                    self._snapshot_catalogs.pop(next(iter(self._snapshot_catalogs)))
            return cached
        await self.refresh_catalog()
        body = json.dumps(self.registry_overlay.to_json(), sort_keys=True).encode("utf-8")
        await self.storage.put(bucket, key, body, content_type="application/json")
        return self.catalog

    async def refresh_catalog(self, max_age_s: float = 30.0) -> None:
        """Reloads the live overlay when it is older than `max_age_s` (promotions and disables made
        through the API reach a running orchestrator without a restart). No-op before the first load."""
        import time

        if self.registry_overlay is not None and time.monotonic() - self.registry_loaded_at > max_age_s:
            await self.reload_catalog()

    async def seconds_per_unit(self, adapter_id: str, *, max_age_s: float = 300.0) -> float | None:
        """The measured p50 seconds per work unit of an adapter (its slowest GPU class), from the
        completed attempts the workers reported (`ce_db.fleet.estimate_stats`, Phase 9); None below
        five samples. Only the fleet's backlog uses it — plans and cache keys never do."""
        import time

        if time.monotonic() - self._speeds_at > max_age_s:
            from ce_db.fleet import estimate_stats

            speeds: dict[str, float] = {}
            try:
                async with self.db.session() as session:
                    for row in await estimate_stats(session):
                        speeds[row.adapter_id] = max(speeds.get(row.adapter_id, 0.0), row.p50_seconds_per_unit)
            except Exception as exc:  # an estimate never blocks a dispatch
                _log.warning("estimate stats unavailable", error=str(exc)[:200])
            self._speeds, self._speeds_at = speeds, time.monotonic()
        return self._speeds.get(adapter_id)

    async def graph(self, sha256: str) -> ExecutionGraph:
        graph = self._graphs.get(sha256)
        if graph is None:
            graph = ExecutionGraph.model_validate_json(await self.docs.raw(sha256))
            self._graphs[sha256] = graph
            if len(self._graphs) > 64:
                self._graphs.pop(next(iter(self._graphs)))
        return graph

    async def adapter(self, adapter_id: str) -> Any:
        """A loaded in-process adapter (`cpu_inproc` family)."""
        plugin = self.registry.get(adapter_id)
        adapter = plugin.adapter()
        async with self._lock:
            if adapter_id not in self._loaded:
                await adapter.load(
                    LoadContext(
                        model_cache_dir=self.settings.model_cache_dir,
                        scratch_dir=str(self.scratch_root / "adapters" / adapter_id),
                        app_env=self.settings.app_env,
                        config=self.plugin_settings(),
                    )
                )
                self._loaded.add(adapter_id)
        return adapter

    def scratch(self, prefix: str) -> Path:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=self.scratch_root))

    async def publish(
        self, org_id: UUID, type: EventType | str, data: dict[str, Any], *, project_id: UUID | None = None
    ) -> None:
        if self.events is not None:
            try:
                await self.events.publish(org_id, type, data, project_id=project_id)
            except Exception as exc:  # events are best effort; the database is the record
                _log.warning("event publish failed", type=str(type), error=str(exc)[:200])

    async def sync_registry(self, *, load: bool = True) -> None:
        """Mirrors the installed manifests into `plugins`/`models` (§24) and, with `load`, applies the
        database's promotions, evidence and calibrations to the router catalog. Concurrent starts
        are harmless: a lost upsert race is logged and the overlay is still loaded."""
        from ce_db.registry import sync_registry

        try:
            async with self.db.transaction() as session:
                await sync_registry(session, [p.manifest for p in self.registry.plugins.values()])
        except Exception as exc:  # another process synced first
            _log.warning("registry sync skipped", error=str(exc)[:200])
        if load:
            await self.reload_catalog()

    async def reload_catalog(self) -> None:
        """Rebuilds the router catalog with the current registry overlay (promotions, disabled
        adapters, measured profiles, calibrated knobs)."""
        from ce_db.registry import load_overlay

        if self.catalog_factory is None:
            return
        async with self.db.session() as session:
            overlay = await load_overlay(session, include_mock_profiles=bool(self.settings.mock_gpu))
        import time

        self.catalog = self.catalog_factory(overlay)
        self.registry_digest = overlay.digest
        self.registry_overlay = overlay
        self.registry_loaded_at = time.monotonic()

    async def close(self) -> None:
        await self.db.dispose()
        if self.events is not None:
            await self.events.redis.aclose()


def build_services(effective: EffectiveConfig, *, events: bool = True, pool_size: int = 10) -> ExecServices:
    settings = effective.settings
    storage = create_storage(settings)
    content = ContentStore(storage, settings.s3_bucket_artifacts)
    registry = discover(app_env=settings.app_env, include_mocks=settings.mock_gpu)
    errors = check_cpu_engines(registry, settings.cpu_real_engines, settings.model_cache_dir)
    if errors:
        raise RuntimeError("CPU_REAL_ENGINES=on but assets are missing: " + "; ".join(errors))

    def make_catalog(overlay: Any = None) -> RouterCatalog:
        return build_catalog(
            registry,
            effective.bundle,
            app_env=settings.app_env,
            mock_gpu=settings.mock_gpu,
            operator=operator_from_config(
                effective.bundle,
                jurisdiction=settings.operator_jurisdiction,
                revenue_band=settings.operator_revenue_band,
            ),
            cpu_real_engines=settings.cpu_real_engines,
            model_cache_dir=settings.model_cache_dir,
            overlay=overlay,
        )

    catalog = make_catalog()
    bus = None
    if events:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        bus = EventBus(
            redis,
            maxlen=effective.bundle.app.events.stream_maxlen,
            retention_s=settings.sse_stream_retention_s,
            clock=_utcnow,
        )
    scratch = Path(tempfile.gettempdir()) / "ce-exec"
    return ExecServices(
        effective=effective,
        db=Database(settings.database_url, pool_size=pool_size),
        storage=storage,
        content=content,
        docs=DocCache(content),
        registry=registry,
        catalog=catalog,
        events=bus,
        scratch_root=scratch,
        catalog_factory=make_catalog,
    )
