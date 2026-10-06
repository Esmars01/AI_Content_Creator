"""The model registry in the database (§24, `ce_db.registry`): manifest sync, evidence that a sync
never downgrades, admin decisions that survive syncs, and the overlay the router applies."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from ce_db.models.behavior import ModelBehaviorProfile
from ce_db.models.platform import Model, Plugin
from ce_db.registry import (
    disable_model,
    load_overlay,
    promote_model,
    record_validation,
    store_knob_calibration,
    sync_registry,
)
from ce_db.session import Database
from ce_testing.registry import manifest_copy

pytestmark = [pytest.mark.infra]


async def _model(db: Database, key: str) -> Model:
    async with db.session() as session:
        return (await session.execute(sa.select(Model).where(Model.model_key == key))).scalar_one()


async def _sync(db: Database, *manifests: Any) -> dict[str, int]:
    async with db.transaction() as session:
        return await sync_registry(session, manifests)


async def test_sync_mirrors_manifests_and_is_idempotent(db: Database) -> None:
    manifest = manifest_copy("infinitetalk")
    first = await _sync(db, manifest)
    assert (first["plugins_inserted"], first["models_inserted"]) == (1, 1)
    second = await _sync(db, manifest)
    assert (second["plugins_inserted"], second["models_inserted"], second["models_updated"]) == (0, 0, 1)
    row = await _model(db, manifest.models[0].key)
    decl = manifest.models[0]
    assert row.source_uri == f"hf://{decl.source.repo}@{decl.source.revision}" and row.revision == decl.source.revision
    assert (row.status, row.validation, row.promotion_basis) == ("sandbox", "untested_on_gpu", None)
    assert row.capabilities == ["avatar.a2v"] and row.license["name"] == "Apache-2.0"
    assert {d["role"] for d in row.dependencies} >= {"base_weights", "audio_encoder", "distill_lora", "code"}
    async with db.session() as session:
        plugin = (await session.execute(sa.select(Plugin).where(Plugin.plugin_key == manifest.id))).scalar_one()
    assert (plugin.runtime_family, plugin.status, plugin.enabled) == ("wan", "sandbox", True)


async def test_providers_are_not_registry_models_and_model_keys_are_unique(db: Database) -> None:
    provider = manifest_copy("gpu.local") if _installed("gpu.local") else None
    if provider is not None:
        assert (await _sync(db, provider))["plugins_inserted"] == 0
    a = manifest_copy("z_image_turbo")
    clash = a.model_copy(update={"id": a.id + "_b"})
    with pytest.raises(ValueError, match="declared by both"):
        await _sync(db, a, clash)


def _installed(adapter_id: str) -> bool:
    from ce_contracts.plugins import discover

    return adapter_id in discover(app_env=None, include_mocks=True).plugins


async def test_evidence_and_admin_decisions_survive_syncs(db: Database) -> None:
    manifest = manifest_copy("infinitetalk")
    await _sync(db, manifest)
    key = manifest.models[0].key
    async with db.transaction() as session:
        row = await record_validation(session, key, "smoke_passed", {"cases": 3})
        with pytest.raises(ValueError, match="note"):
            await promote_model(session, row.id, note="  ", license_ok=True)
        with pytest.raises(ValueError, match="license"):
            await promote_model(session, row.id, note="smoke run passed", license_ok=False)
        promoted = await promote_model(session, row.id, note="smoke run passed", license_ok=True)
        assert (promoted.status, promoted.promotion_basis) == ("production", "smoke")
    await _sync(db, manifest)  # the manifest still says sandbox / untested_on_gpu
    row = await _model(db, key)
    assert (row.status, row.validation, row.promotion_basis) == ("production", "smoke_passed", "smoke")
    assert row.quality_scores["smoke_passed"] == {"cases": 3}
    async with db.transaction() as session:
        await disable_model(session, row.id)
    await _sync(db, manifest)
    assert (await _model(db, key)).status == "disabled"


async def test_promotion_needs_smoke_evidence_and_failures_are_recorded(db: Database) -> None:
    manifest = manifest_copy("z_image_turbo")
    await _sync(db, manifest)
    key = manifest.models[0].key
    row = await _model(db, key)
    async with db.transaction() as session:
        with pytest.raises(ValueError, match="smoke_passed"):
            await promote_model(session, row.id, note="looks fine", license_ok=True)
        with pytest.raises(ValueError, match="evidence"):
            await record_validation(session, key, "untested_on_gpu", {})
        await record_validation(session, key, "bench_passed", {"bench": 1})
        kept = await record_validation(session, key, "smoke_passed", {"smoke": 1})
        assert kept.validation == "bench_passed"  # weaker evidence never replaces stronger
        failed = await record_validation(session, key, "failed", {"error": "oom"})
        assert failed.validation == "failed"  # a failure is always recorded


async def test_overlay_carries_promotions_validation_disabled_and_knobs(db: Database) -> None:
    promoted, multi, disabled = manifest_copy("infinitetalk"), manifest_copy("qwen3_tts"), manifest_copy("rife_425")
    await _sync(db, promoted, multi, disabled)
    async with db.transaction() as session:
        row = await record_validation(session, promoted.models[0].key, "smoke_passed", {"n": 1})
        await promote_model(session, row.id, note="smoke run passed", license_ok=True)
        # one of qwen3_tts's two models validated: the adapter is as strong as its weakest model
        await record_validation(session, multi.models[0].key, "smoke_passed", {"n": 1})
        off = (await session.execute(sa.select(Model).where(Model.model_key == disabled.models[0].key))).scalar_one()
        await disable_model(session, off.id)
        await store_knob_calibration(
            session, adapter_id=promoted.id, translator_version="infinitetalk_v1", revision="r1",
            knob="motion_energy", curve={"calibrated": True, "monotonic": "true", "points": [[0, 0], [1, 1]]},
        )  # fmt: skip
        overlay = await load_overlay(session)
    assert overlay.status[promoted.id] == "production" and overlay.promotion_basis[promoted.id] == "smoke"
    assert overlay.validation[promoted.id] == "smoke_passed"
    assert multi.id not in overlay.validation and multi.id not in overlay.status
    assert disabled.id in overlay.disabled
    assert overlay.knobs[(promoted.id, "motion_energy")]["calibrated"] is True

    async with db.transaction() as session:
        await record_validation(session, multi.models[1].key, "smoke_passed", {"n": 1})
        after = await load_overlay(session)
    assert after.validation[multi.id] == "smoke_passed"
    assert after.digest != overlay.digest  # route decisions depend on the overlay


async def test_mock_profiles_reach_the_overlay_only_with_mock_gpu(db: Database) -> None:
    manifest = manifest_copy("mock_avatar_global")
    async with db.transaction() as session:
        session.add(
            ModelBehaviorProfile(
                adapter_id=manifest.id, translator_version="mock_v1", revision="r", dimension="gaze",
                language="und", source="mock", measured={"success_rate": 0.7, "n": 12},
            )
        )  # fmt: skip
    async with db.session() as session:
        assert (manifest.id, "gaze") not in (await load_overlay(session)).measured
        measured = (await load_overlay(session, include_mock_profiles=True)).measured
    assert measured[(manifest.id, "gaze")] == {"success_rate": 0.7, "n": 12, "source": "mock"}
