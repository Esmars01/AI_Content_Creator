"""Per-version registry snapshots (I5, Phase 8): a version plans and builds with the registry overlay
in effect at its first plan; later promotions reach new versions (and a running orchestrator) but
never re-route or re-compile an existing version."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from ce_config.settings import load_effective
from ce_db.registry import RegistryOverlay, promote_model, record_validation, sync_registry
from ce_exec.context import build_services
from ce_testing.database import TestDatabase
from ce_testing.registry import manifest_copy

pytestmark = [pytest.mark.infra]

ROOT = Path(__file__).resolve().parents[4]


def test_overlay_json_round_trip() -> None:
    overlay = RegistryOverlay(
        status={"a": "production"}, validation={"a": "smoke_passed"}, promotion_basis={"a": "smoke"},
        disabled=frozenset({"b"}), measured={("a", "gaze"): {"success_rate": 0.5, "n": 4}},
        knobs={("a", "motion_energy"): {"calibrated": True}},
    )  # fmt: skip
    again = RegistryOverlay.from_json(overlay.to_json())
    assert again == overlay and again.digest == overlay.digest


async def test_a_version_keeps_its_registry_snapshot(migrated_db: TestDatabase, tmp_path: Path) -> None:
    env = {
        "APP_ENV": "test", "DATABASE_URL": migrated_db.url, "SECRET_KEY": "test-secret-key-for-signing-only",
        "STORAGE_PROVIDER": "local_fs", "LOCAL_STORAGE_ROOT": str(tmp_path / "storage"), "MOCK_GPU": "true",
        "PROVENANCE_MODE": "", "COOKIE_SECURE": "",
    }  # fmt: skip
    svc = build_services(load_effective(ROOT / "config", env), events=False, pool_size=2)
    try:
        await svc.storage.ensure_bucket(svc.settings.s3_bucket_artifacts)
        org, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # before any overlay is loaded (tests, tools) the live catalog is used and nothing is stored
        assert await svc.version_catalog(org, first) is svc.catalog
        assert not await svc.storage.exists(svc.settings.s3_bucket_artifacts, svc._snapshot_key(org, first))

        await svc.sync_registry()  # what the orchestrator does at startup
        pinned = await svc.version_catalog(org, first)
        assert pinned.manifests["infinitetalk"].status == "sandbox"

        # infinitetalk is smoke-promoted afterwards (the registry is global; use a copy's model rows)
        model_key = svc.registry.get("infinitetalk").manifest.models[0].key
        async with svc.db.transaction() as session:
            row = await record_validation(session, model_key, "smoke_passed", {"cases": 1})
            await promote_model(session, row.id, note="smoke run passed", license_ok=True)
        await svc.reload_catalog()
        assert svc.catalog.manifests["infinitetalk"].status == "production"

        again = await svc.version_catalog(org, first)  # the first version keeps its snapshot
        assert again.manifests["infinitetalk"].status == "sandbox"
        fresh = await svc.version_catalog(org, second)  # a new version starts from the live registry
        assert fresh.manifests["infinitetalk"].status == "production"
    finally:
        async with svc.db.transaction() as session:  # undo the promotion on the shared database
            import sqlalchemy as sa
            from ce_db.models.platform import Model

            await session.execute(
                sa.update(Model)
                .where(Model.model_key == svc.registry.get("infinitetalk").manifest.models[0].key)
                .values(status="sandbox", validation="untested_on_gpu", promotion_basis=None, quality_scores={})
            )
        await svc.close()


async def test_refresh_reloads_only_stale_overlays(migrated_db: TestDatabase, tmp_path: Path) -> None:
    env = {
        "APP_ENV": "test", "DATABASE_URL": migrated_db.url, "SECRET_KEY": "test-secret-key-for-signing-only",
        "STORAGE_PROVIDER": "local_fs", "LOCAL_STORAGE_ROOT": str(tmp_path / "storage"), "MOCK_GPU": "true",
        "PROVENANCE_MODE": "", "COOKIE_SECURE": "",
    }  # fmt: skip
    svc = build_services(load_effective(ROOT / "config", env), events=False, pool_size=2)
    try:
        await svc.refresh_catalog(max_age_s=0)  # never loaded: no-op
        assert svc.registry_overlay is None
        copy = manifest_copy("z_image_turbo")
        async with svc.db.transaction() as session:
            await sync_registry(session, [copy])
        await svc.reload_catalog()
        loaded_at = svc.registry_loaded_at
        await svc.refresh_catalog(max_age_s=3600)
        assert svc.registry_loaded_at == loaded_at
        await svc.refresh_catalog(max_age_s=0)
        assert svc.registry_loaded_at > loaded_at
    finally:
        await svc.close()
