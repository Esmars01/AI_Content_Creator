"""The model registry API (§24, §30): reads for org members; promotion, disabling, evidence,
calibration reports and calibration jobs for platform admins only.

`models` is a platform table shared by the session's test database, so every test registers its
own copies of real manifests under unique ids (`_register`) and never touches the installed rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant
from ce_contracts.plugins import discover
from ce_db.models.behavior import ModelBehaviorProfile
from ce_db.models.platform import AuditLog, Model, Plugin
from ce_db.registry import sync_registry
from ce_testing.registry import manifest_copy

pytestmark = [pytest.mark.infra]

NOTE = "Smoke run on rtx_4090_24gb passed every eval/smoke case (GPU_VALIDATION.md)."


async def _register(harness: ApiHarness, *manifests: Any) -> dict[str, UUID]:
    async with harness.services.db.transaction() as session:
        await sync_registry(session, manifests)
        keys = [d.key for m in manifests for d in m.models]
        rows = (await session.execute(sa.select(Model).where(Model.model_key.in_(keys)))).scalars()
        return {r.model_key: r.id for r in rows}


def _smoke(adapter_id: str, *, passed: bool = True, backend: str = "real", kind: str = "smoke") -> dict[str, Any]:
    """The shape of a `scripts/smoke/run.py` report (ce_worker.validation)."""
    return {
        "kind": kind,
        "backend": backend,
        "adapter_id": adapter_id,
        "eval_set_version": "smoke-v1",
        "verdict": ("smoke_passed" if passed else "failed") if backend == "real" else "stand_in_only",
        "cases": [{"case_id": "avatar_en_trajectory", "passed": passed, "seconds": 41.0}],
        "host": {"gpus": [{"name": "NVIDIA RTX 4090", "vram_gb": 24.0}]},
        "timings": {"_all": {"seconds_per_unit_p50": 9.5}},
    }


async def _admin(harness: ApiHarness, tenant: ApiTenant) -> ApiTenant:
    return await harness.add_member(tenant.org_id, "viewer", is_platform_admin=True)


async def test_members_read_the_registry_and_admin_endpoints_need_a_platform_admin(
    harness: ApiHarness, owner: ApiTenant
) -> None:
    manifest = manifest_copy("infinitetalk")
    ids = await _register(harness, manifest)
    model_id = next(iter(ids.values()))

    listed = await owner.client.get("/v1/models", params={"capability": "avatar.a2v"})
    assert listed.status_code == 200
    row = next(r for r in listed.json() if r["id"] == str(model_id))
    assert row["plugin_key"] == manifest.id
    assert (row["status"], row["validation"], row["smoke_promoted"]) == ("sandbox", "untested_on_gpu", False)
    assert row["license"]["name"] == "Apache-2.0" and row["license"]["commercial_use"] is True
    assert all(r["id"] != str(model_id) for r in (await owner.client.get("/v1/models?capability=x.y")).json())

    detail = (await owner.client.get(f"/v1/models/{model_id}")).json()
    assert {d["role"] for d in detail["dependencies"]} >= {"base_weights", "audio_encoder", "code"}
    assert detail["evidence"] == {} and detail["knob_calibrations"] == []
    assert (await owner.client.get(f"/v1/models/{UUID(int=1)}")).status_code == 404

    # an org owner is not a platform admin (§30: /v1/admin/* is platform-only)
    for path, body in (
        (f"/v1/admin/models/{model_id}:promote", {"note": NOTE}),
        (f"/v1/admin/models/{model_id}:disable", {"reason": "nope"}),
        (f"/v1/admin/models/{model_id}/validations", {"validation": "smoke_passed", "report": _smoke(manifest.id)}),
        (f"/v1/admin/models/{model_id}/benchmarks", {"report": _smoke(manifest.id, kind="bench")}),
        (f"/v1/admin/models/{model_id}:calibrate", {}),
    ):
        assert (await owner.client.post(path, json=body)).status_code == 403, path


async def test_promotion_needs_evidence_a_note_and_a_production_license_closure(
    harness: ApiHarness, owner: ApiTenant
) -> None:
    admin = await _admin(harness, owner)
    good = manifest_copy("infinitetalk")
    non_commercial = manifest_copy(
        "infinitetalk", license_update={"name": "CC-BY-NC-4.0", "commercial_use": False, "conditions": []}
    )
    ids = await _register(harness, good, non_commercial)
    good_id, bad_id = ids[good.models[0].key], ids[non_commercial.models[0].key]

    # rule 5: no promotion without recorded evidence
    refused = await admin.client.post(f"/v1/admin/models/{good_id}:promote", json={"note": NOTE})
    assert refused.status_code == 409 and "smoke_passed" in refused.json()["detail"]
    # a written note is required (ADR-style)
    assert (await admin.client.post(f"/v1/admin/models/{good_id}:promote", json={"note": "ok"})).status_code == 422
    # evidence needs its report
    empty = await admin.client.post(
        f"/v1/admin/models/{good_id}/validations", json={"validation": "smoke_passed", "report": {}}
    )
    assert empty.status_code == 422

    # rule 5: a CPU stand-in run, a failed run, another adapter's run or a hand-set bench_passed is not evidence
    for bad, code in (
        (_smoke(good.id, backend="test"), 422),
        ({**_smoke(good.id), "verdict": "smoke_passed", "cases": [{"case_id": "x", "passed": False}]}, 422),
        (_smoke("someone_else"), 422),
    ):
        rejected = await admin.client.post(
            f"/v1/admin/models/{good_id}/validations", json={"validation": "smoke_passed", "report": bad}
        )
        assert rejected.status_code == code, rejected.text
    hand_set = await admin.client.post(
        f"/v1/admin/models/{good_id}/validations",
        json={"validation": "bench_passed", "report": _smoke(good.id, kind="bench")},
    )
    assert hand_set.status_code == 409

    report = _smoke(good.id)
    recorded = await admin.client.post(
        f"/v1/admin/models/{good_id}/validations", json={"validation": "smoke_passed", "report": report}
    )
    assert recorded.status_code == 200 and recorded.json()["validation"] == "smoke_passed"
    # since the benchmark runner exists (Phase 11), smoke evidence alone no longer promotes (§24)
    smoke_only = await admin.client.post(f"/v1/admin/models/{good_id}:promote", json={"note": NOTE})
    assert smoke_only.status_code == 409 and "bench_passed" in smoke_only.json()["detail"]
    async with harness.services.db.transaction() as session:  # what a passed benchmark records
        await session.execute(sa.update(Model).where(Model.id == good_id).values(validation="bench_passed"))
    promoted = await admin.client.post(f"/v1/admin/models/{good_id}:promote", json={"note": NOTE})
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert (body["status"], body["promotion_basis"], body["smoke_promoted"]) == ("production", "bench", False)
    detail = (await owner.client.get(f"/v1/models/{good_id}")).json()
    assert detail["evidence"]["smoke_passed"] == report

    # a later failed run is always recorded and takes the model out of production routing
    failed = await admin.client.post(
        f"/v1/admin/models/{good_id}/validations",
        json={"validation": "failed", "report": _smoke(good.id, passed=False)},
    )
    assert failed.json()["validation"] == "failed"

    # a non-commercial weight in the closure is refused for production, whatever the evidence
    async with harness.services.db.transaction() as session:
        await session.execute(sa.update(Model).where(Model.id == bad_id).values(validation="bench_passed"))
    denied = await admin.client.post(f"/v1/admin/models/{bad_id}:promote", json={"note": NOTE})
    assert denied.status_code == 409
    assert any(i["code"] == "license" for i in denied.json()["issues"])

    async with harness.services.db.session() as session:
        actions = (
            await session.execute(
                sa.select(AuditLog.action, AuditLog.after).where(AuditLog.target_id.in_([str(good_id), str(bad_id)]))
            )
        ).all()
    assert {"model.promote", "model.validation"} <= {action for action, _ in actions}
    promote_entry = next(after for action, after in actions if action == "model.promote")
    assert promote_entry["note"] == NOTE and promote_entry["promotion_basis"] == "bench"


async def test_disable_is_audited_and_survives_a_registry_sync(harness: ApiHarness, owner: ApiTenant) -> None:
    admin = await _admin(harness, owner)
    manifest = manifest_copy("z_image_turbo")
    model_id = next(iter((await _register(harness, manifest)).values()))
    disabled = await admin.client.post(f"/v1/admin/models/{model_id}:disable", json={"reason": "color shift"})
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    await _register(harness, manifest)  # the orchestrator's next startup sync
    assert (await owner.client.get(f"/v1/models/{model_id}")).json()["status"] == "disabled"
    async with harness.services.db.session() as session:
        entry = (
            await session.execute(
                sa.select(AuditLog).where(AuditLog.target_id == str(model_id), AuditLog.action == "model.disable")
            )
        ).scalar_one()
    assert entry.after is not None and entry.after["reason"] == "color shift"
    assert entry.before == {"status": "sandbox"}


async def test_calibration_reports_are_stored_per_knob(harness: ApiHarness, owner: ApiTenant) -> None:
    admin = await _admin(harness, owner)
    manifest = manifest_copy("infinitetalk")
    model_id = next(iter((await _register(harness, manifest)).values()))
    curve = {"calibrated": True, "monotonic": "true", "rho": 0.94, "points": [[0.0, 0.1], [1.0, 0.6]]}
    report = {
        "kind": "calibrate", "backend": "real", "adapter_id": manifest.id, "translator_version": "infinitetalk_v1",
        "revision": "abc", "knobs": {},
    }  # fmt: skip
    stand_in = await admin.client.post(
        f"/v1/admin/models/{model_id}/calibrations", json={"report": {**report, "backend": "test"}}
    )
    assert stand_in.status_code == 422
    wrong = await admin.client.post(
        f"/v1/admin/models/{model_id}/calibrations", json={"report": {**report, "adapter_id": "other"}}
    )
    assert wrong.status_code == 422
    stored = await admin.client.post(
        f"/v1/admin/models/{model_id}/calibrations",
        json={"report": {**report, "knobs": {"motion_energy": curve, "unfitted": {"calibrated": False}}}},
    )
    assert stored.status_code == 200, stored.text
    (knob,) = stored.json()["knob_calibrations"]
    assert (knob["knob"], knob["calibrated"], knob["rho"]) == ("motion_energy", True, 0.94)
    assert knob["points"] == [[0.0, 0.1], [1.0, 0.6]]


async def test_bench_runs_are_stored_pending(harness: ApiHarness, owner: ApiTenant) -> None:
    """A bench run is evidence of timings and measured behavior; pass/fail is Phase 11's."""
    admin = await _admin(harness, owner)
    manifest = manifest_copy("chatterbox")
    model_id = next(iter((await _register(harness, manifest)).values()))
    report = _smoke(manifest.id, kind="bench")
    report["cases"][0]["measurements"] = {"pauses": {"measured": True, "longest_ms": 540}}
    stand_in = await admin.client.post(
        f"/v1/admin/models/{model_id}/benchmarks", json={"report": {**report, "backend": "test"}}
    )
    assert stand_in.status_code == 422
    stored = await admin.client.post(f"/v1/admin/models/{model_id}/benchmarks", json={"report": report})
    assert stored.status_code == 201, stored.text
    assert stored.json()["verdict"] == "pending"
    detail = (await owner.client.get(f"/v1/models/{model_id}")).json()
    (bench,) = detail["benchmarks"]
    assert bench["metrics"]["timings"]["_all"]["seconds_per_unit_p50"] == 9.5
    assert bench["behavior_profile"]["avatar_en_trajectory"]["pauses"]["longest_ms"] == 540
    assert detail["validation"] == "untested_on_gpu"  # a bench record is not a validation


async def test_calibrate_job_refuses_gpu_families_without_claiming_a_run(harness: ApiHarness, owner: ApiTenant) -> None:
    """GPU-family adapters calibrate on a host of their family (`scripts/calibrate`); the job says so."""
    admin = await _admin(harness, owner)
    manifest = discover(app_env=None, include_mocks=True).get("infinitetalk").manifest
    ids = await _register(harness, manifest)  # the installed adapter (the job looks it up by plugin key)
    model_id = ids[manifest.models[0].key]
    accepted = await admin.client.post(f"/v1/admin/models/{model_id}:calibrate", json={})
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    await harness.services.workflows.drain()
    job = (await admin.client.get(f"/v1/jobs/{job_id}")).json()
    assert job["status"] == "failed" and job["error"]["code"] == "needs_gpu_host"
    assert "scripts/calibrate/run.py infinitetalk" in job["error"]["message"]


async def test_calibrate_job_fits_the_mock_engine_knob(harness: ApiHarness, owner: ApiTenant) -> None:
    """A `cpu_model` engine (the mock avatar) calibrates in the orchestrator: renders a knob sweep,
    measures it with the mock observer and stores the fitted curve."""
    admin = await _admin(harness, owner)
    manifest = discover(app_env=None, include_mocks=True).get("mock_avatar_global").manifest
    if not manifest.models or not manifest.knobs:
        pytest.skip("the mock avatar declares no model or no knobs")
    ids = await _register(harness, manifest)
    model_id = ids[manifest.models[0].key]
    try:
        accepted = await admin.client.post(f"/v1/admin/models/{model_id}:calibrate", json={})
        assert accepted.status_code == 202
        await harness.services.workflows.drain()
        job = (await admin.client.get(f"/v1/jobs/{accepted.json()['job_id']}")).json()
        assert job["status"] == "succeeded", job
        knobs = (await owner.client.get(f"/v1/models/{model_id}")).json()["knob_calibrations"]
        assert {k["knob"] for k in knobs} == set(manifest.knobs)
        assert all(len(k["points"]) >= 3 for k in knobs)
    finally:  # keep the shared database's mock profiles as other tests expect them
        async with harness.services.db.transaction() as session:
            await session.execute(
                sa.delete(ModelBehaviorProfile).where(
                    ModelBehaviorProfile.adapter_id == manifest.id,
                    ModelBehaviorProfile.dimension.startswith("knob:"),
                )
            )


async def test_registry_rows_are_platform_rows_not_tenant_rows(harness: ApiHarness, owner: ApiTenant) -> None:
    """Every org sees the same registry (it describes installed software, not tenant data)."""
    manifest = manifest_copy("chatterbox")
    model_id = next(iter((await _register(harness, manifest)).values()))
    other = await harness.new_tenant("viewer", name="Other org")
    assert (await other.client.get(f"/v1/models/{model_id}")).status_code == 200
    async with harness.services.db.session() as session:
        plugin = (await session.execute(sa.select(Plugin).where(Plugin.plugin_key == manifest.id))).scalar_one()
    assert not hasattr(plugin, "org_id")
