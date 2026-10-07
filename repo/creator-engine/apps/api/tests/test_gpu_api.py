"""The GPU API (§30, Phase 9) against an in-process scheduler app: pools, workers and offers for
members; providers, provisioning, stopping, enrollment and the queue for platform admins. The
scheduler runs the simulated providers (MOCK_GPU=true); nothing reaches a real provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.scheduler import SchedulerClient
from ce_api.testing import ApiHarness, ApiTenant
from ce_db.models.platform import AuditLog, FleetCost, GpuProvider, GpuWorker

pytestmark = [pytest.mark.infra]


class _Completer:
    async def complete(self, token: str, result: dict[str, Any]) -> None: ...
    async def fail(self, token: str, error_class: str, message: str) -> None: ...
    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None: ...
    async def report_cancellation(self, token: str) -> None: ...


@pytest_asyncio.fixture
async def scheduler(harness: ApiHarness) -> AsyncIterator[httpx.AsyncClient]:
    """The scheduler app in process; the API's fleet client talks to it over ASGI."""
    from ce_scheduler.app import create_app

    app = create_app(
        harness.services.effective, completer=_Completer(), storage=harness.services.storage, start_loops=False
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        harness.services.scheduler = SchedulerClient(
            "http://scheduler.test", harness.services.signing_secret, transport=transport
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://scheduler.test") as client:
            yield client


async def _admin(harness: ApiHarness, tenant: ApiTenant) -> ApiTenant:
    return await harness.add_member(tenant.org_id, "viewer", is_platform_admin=True)


async def test_members_read_pools_workers_and_offers_admins_manage_the_fleet(
    harness: ApiHarness, owner: ApiTenant, scheduler: httpx.AsyncClient
) -> None:
    pools = await owner.client.get("/v1/gpu/pools")
    assert pools.status_code == 200
    by_id = {p["id"]: p for p in pools.json()}
    assert {"mock", "cpu_local", "large_vram", "consumer"} <= set(by_id)
    assert by_id["large_vram"]["enabled"] is False and by_id["large_vram"]["providers_available"] == []
    assert "mock" in by_id["mock"]["providers_available"]
    offers = (await owner.client.get("/v1/gpu/offers", params={"gpu_class": "mock_gpu"})).json()
    assert offers and {o["provider"] for o in offers} == {"mock"} and not any(o["paid"] for o in offers)
    assert (await owner.client.get("/v1/gpu/workers")).status_code == 200
    for method, path, body in (
        ("GET", "/v1/admin/gpu/providers", None),
        ("POST", "/v1/admin/gpu/providers", {"kind": "mock", "name": "x"}),
        ("POST", "/v1/admin/gpu/workers:provision", {"provider": "mock", "gpu_class": "mock_gpu",
                                                     "runtime_family": "cpu_model"}),
        ("POST", "/v1/admin/gpu/workers:enroll", {"runtime_family": "tts"}),
        ("GET", "/v1/admin/gpu/queue", None),
    ):  # fmt: skip
        assert (await owner.client.request(method, path, json=body)).status_code == 403, path

    admin = await _admin(harness, owner)
    # provision on the simulated provider, see the worker, stop it: its lifetime is a fleet cost
    done = await admin.client.post(
        "/v1/admin/gpu/workers:provision",
        json={"provider": "mock", "gpu_class": "mock_gpu", "runtime_family": "cpu_model", "count": 1},
    )
    assert done.status_code == 200, done.text
    (worker,) = done.json()["provisioned"]
    assert worker["provider"] == "mock" and done.json()["attempts"][-1].endswith(f"provisioned {worker['external_id']}")
    listed = (await owner.client.get("/v1/gpu/workers")).json()
    row = next(w for w in listed if w["id"] == worker["worker_id"])
    assert row["state"] == "provisioning" and row["pool_id"] == "admin:mock" and row["provider_kind"] == "mock"
    assert "token_hash" not in row
    stopped = await admin.client.post(f"/v1/admin/gpu/workers/{worker['worker_id']}:stop", json={})
    assert stopped.status_code == 200 and stopped.json()["state"] == "terminated"
    async with harness.services.db.session() as session:
        state = (await session.execute(sa.select(GpuWorker.state).where(GpuWorker.id == worker["worker_id"]))).scalar()
        costs = (
            await session.execute(sa.select(sa.func.count()).where(FleetCost.worker_id == worker["worker_id"]))
        ).scalar()
    assert state == "stopped" and costs == 1
    again = await admin.client.post(f"/v1/admin/gpu/workers/{worker['worker_id']}:stop", json={})
    assert again.status_code == 409

    # provider rows: no secrets in config, kinds validated, paid spending needs an explicit, noted PATCH
    bad = await admin.client.post(
        "/v1/admin/gpu/providers", json={"kind": "runpod_pod", "name": "rp", "config": {"api_key": "rp_x"}}
    )
    assert bad.status_code == 422 and "credentials_ref" in bad.json()["detail"]
    # a kind no plugin registers (it used "vast" before the Vast provider existed)
    unknown = await admin.client.post(
        "/v1/admin/gpu/providers", json={"kind": "not_a_registered_provider", "name": "v"}
    )
    assert unknown.status_code == 422
    eager = await admin.client.post(
        "/v1/admin/gpu/providers", json={"kind": "runpod_pod", "name": "rp", "config": {"allow_paid": True}}
    )
    assert eager.status_code == 409
    created = await admin.client.post(
        "/v1/admin/gpu/providers",
        json={"kind": "runpod_pod", "name": "rp-eu", "credentials_ref": "env:CE_TEST_RUNPOD_KEY_UNSET",
              "regions": ["eu"], "enabled": True, "budget_daily_usd": 5},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    provider = created.json()
    assert provider["paid"] is True and provider["config"] == {}
    rows = (await admin.client.get("/v1/admin/gpu/providers")).json()
    mine = next(r for r in rows["rows"] if r["id"] == provider["id"])
    assert mine["loaded"] is False  # its credentials reference does not resolve: the scheduler skipped it
    assert {r["key"] for r in rows["registered"]} >= {"mock", "example_cloud", "runpod_pod", "local_docker"}
    unnoted = await admin.client.patch(
        f"/v1/admin/gpu/providers/{provider['id']}", json={"config": {"allow_paid": True}}
    )
    assert unnoted.status_code == 422
    approved = await admin.client.patch(
        f"/v1/admin/gpu/providers/{provider['id']}",
        json={"config": {"allow_paid": True, "max_price_per_hour_usd": 0.8}, "note": "owner approved 5 USD/day"},
    )
    assert approved.status_code == 200 and approved.json()["config"]["allow_paid"] is True
    async with harness.services.db.session() as session:
        actions = (
            await session.execute(
                sa.select(AuditLog.action, AuditLog.after).where(AuditLog.target_id == provider["id"])
                .order_by(AuditLog.created_at)
            )
        ).all()  # fmt: skip
        await session.execute(sa.update(GpuProvider).where(GpuProvider.id == provider["id"]).values(enabled=False))
        await session.commit()
    assert [a for a, _ in actions] == ["gpu_provider.create", "gpu_provider.paid_enabled"]
    assert actions[1][1]["note"] == "owner approved 5 USD/day"
    disabled = await admin.client.post(
        "/v1/admin/gpu/workers:provision",
        json={"provider_id": provider["id"], "gpu_class": "rtx_4090_24gb", "runtime_family": "tts"},
    )
    assert disabled.status_code == 409

    queue = await admin.client.get("/v1/admin/gpu/queue")
    assert queue.status_code == 200 and queue.json()["spend"]["budget_daily_usd"] >= 0


async def test_vast_is_an_additional_paid_provider_kind(
    harness: ApiHarness, owner: ApiTenant, scheduler: httpx.AsyncClient
) -> None:
    """The Vast plugin registers next to RunPod: same rules (paid, credentials only by reference,
    paid provisioning only through a noted PATCH), RunPod still registered."""
    admin = await _admin(harness, owner)
    eager = await admin.client.post(
        "/v1/admin/gpu/providers", json={"kind": "vast", "name": "vast-eu", "config": {"allow_paid": True}}
    )
    assert eager.status_code == 409
    secret = await admin.client.post(
        "/v1/admin/gpu/providers", json={"kind": "vast", "name": "vast-eu", "config": {"api_key": "v_x"}}
    )
    assert secret.status_code == 422 and "credentials_ref" in secret.json()["detail"]
    created = await admin.client.post(
        "/v1/admin/gpu/providers",
        json={"kind": "vast", "name": "vast-eu", "credentials_ref": "env:CE_TEST_VAST_KEY_UNSET", "regions": ["eu"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["paid"] is True and created.json()["config"] == {}
    registered = {r["key"] for r in (await admin.client.get("/v1/admin/gpu/providers")).json()["registered"]}
    assert {"vast", "runpod_pod", "runpod_serverless", "local_docker"} <= registered


async def test_enrollment_tokens_register_a_self_managed_host_once(
    harness: ApiHarness, owner: ApiTenant, scheduler: httpx.AsyncClient
) -> None:
    admin = await _admin(harness, owner)
    issued = await admin.client.post("/v1/admin/gpu/workers:enroll", json={"runtime_family": "cpu_model"})
    assert issued.status_code == 201
    token = issued.json()["token"]
    register = {"name": "self-managed-1", "runtime_family": "cpu_model", "adapters": ["mock_voice"],
                "gpu_type": "rtx_4090_24gb"}  # fmt: skip
    wrong_family = await scheduler.post(
        "/internal/v1/worker/register",
        json={**register, "runtime_family": "tts"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert wrong_family.status_code == 401
    first = await scheduler.post(
        "/internal/v1/worker/register", json=register, headers={"Authorization": f"Bearer {token}"}
    )
    assert first.status_code == 200 and "mock_voice" in first.json()["adapters"]
    second = await scheduler.post(
        "/internal/v1/worker/register", json=register, headers={"Authorization": f"Bearer {token}"}
    )
    assert second.status_code == 401  # one-time
    async with harness.services.db.session() as session:
        audited = (
            await session.execute(sa.select(AuditLog.after).where(AuditLog.action == "gpu_worker.enroll"))
        ).scalars().all()  # fmt: skip
    assert audited and all(token not in str(a) for a in audited)  # the token itself is never stored


async def test_the_fleet_endpoints_answer_503_when_the_scheduler_is_down(harness: ApiHarness, owner: ApiTenant) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    harness.services.scheduler = SchedulerClient("http://scheduler.test", "x", transport=httpx.MockTransport(down))
    response = await owner.client.get("/v1/gpu/pools")
    assert response.status_code == 503 and response.json()["code"] == "upstream_unavailable"
    assert (await owner.client.get("/v1/gpu/workers")).status_code == 200  # database reads still work


async def test_the_scheduler_refuses_fleet_calls_without_the_admin_token(
    harness: ApiHarness, scheduler: httpx.AsyncClient
) -> None:
    assert (await scheduler.get("/internal/v1/admin/fleet/status")).status_code == 401
    forged = await scheduler.get("/internal/v1/admin/fleet/status", headers={"X-Admin-Token": "0" * 64})
    assert forged.status_code == 401
