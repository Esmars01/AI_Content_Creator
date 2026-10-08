"""Production data (cutover §3): demo provenance, no demo data in production, bootstrap, purge.

- The dev seed's org is flagged `is_demo`; production refuses to sign into it, or to use a session or
  API key of it, and `/v1/me` lists no demo membership there.
- `ce admin bootstrap` creates a real org and owner on a clean install.
- `ce data purge-demo` removes demo orgs only, with their users and objects, and keeps everything a
  real org holds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.admin_cli import apply_purge, bootstrap_org, plan_purge
from ce_api.cli import app
from ce_api.security.passwords import Passwords
from ce_api.testing import ApiHarness, ApiTenant, build_test_services
from ce_db.models.assets import Asset
from ce_db.models.creators import Creator
from ce_db.models.tenancy import Membership, Organization, User
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX
from ce_testing.seed import DEV_ADMIN_EMAIL, seed_dev
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "a long enough demo password"


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[ApiHarness]:
    """A private database: these tests seed, re-member and purge the seed org, which other modules share."""
    database = await TestDatabase(prefix="ce_proddata").create()
    services = build_test_services(database.url, storage="local_fs", storage_root=tmp_path / "storage")
    await services.storage.ensure_bucket(services.settings.s3_bucket_assets)
    h = ApiHarness(services)
    try:
        yield h
    finally:
        await h.aclose()
        await database.drop()


async def _seed(harness: ApiHarness) -> None:
    passwords = Passwords(harness.services.config.security.password_hash)
    async with harness.services.db.transaction() as session:
        await seed_dev(session, harness.services.effective.bundle.vocab, passwords.hash(PASSWORD))
        await session.execute(
            sa.update(User).where(User.email == DEV_ADMIN_EMAIL).values(password_hash=passwords.hash(PASSWORD))
        )


@pytest.mark.infra
async def test_the_seed_org_is_flagged_demo(harness: ApiHarness) -> None:
    await _seed(harness)
    async with harness.services.db.session() as session:
        org = await session.get_one(Organization, ALEX.ORG_ID)
    assert org.is_demo is True


@pytest.mark.infra
async def test_production_refuses_demo_orgs(harness: ApiHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(harness)
    client, _ = await harness.login(DEV_ADMIN_EMAIL, PASSWORD)  # dev/test: the demo org is usable
    me = (await client.get("/v1/me")).json()
    assert me["org"]["is_demo"] is True and me["environment"]["demo_data"] is True
    assert (await client.get("/v1/creators")).status_code == 200

    monkeypatch.setattr(harness.services.settings, "app_env", "prod")
    # An existing session of the demo org stops working…
    assert (await client.get("/v1/creators")).status_code == 401
    # …and signing in again finds no non-demo membership.
    response = await harness.client().post("/v1/auth/login", json={"email": DEV_ADMIN_EMAIL, "password": PASSWORD})
    assert response.status_code == 401


@pytest.mark.infra
async def test_production_signs_into_the_real_org_and_hides_demo_memberships(
    harness: ApiHarness, owner: ApiTenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(harness)
    async with harness.services.db.transaction() as session:  # the dev admin also belongs to a real org
        session.add(Membership(user_id=ALEX.USER_ID, org_id=owner.org_id, role="editor"))
    monkeypatch.setattr(harness.services.settings, "app_env", "prod")
    response = await harness.client().post("/v1/auth/login", json={"email": DEV_ADMIN_EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["org"]["id"] == str(owner.org_id)
    assert [m["org_id"] for m in body["memberships"]] == [str(owner.org_id)]
    assert body["environment"] == {
        "app_env": "prod",
        "mock_gpu": harness.services.settings.mock_gpu,
        "demo_data": False,
    }
    # Asking for the demo org by id answers like a missing membership.
    response = await harness.client().post(
        "/v1/auth/login", json={"email": DEV_ADMIN_EMAIL, "password": PASSWORD, "org_id": str(ALEX.ORG_ID)}
    )
    assert response.status_code == 401


@pytest.mark.infra
async def test_bootstrap_creates_a_real_org_and_owner(harness: ApiHarness) -> None:
    passwords = Passwords(harness.services.config.security.password_hash)
    async with harness.services.db.transaction() as session:
        result = await bootstrap_org(
            session,
            org_name="Acme Studio (bootstrap test)",
            email="Owner.Bootstrap@Example.com",
            name="Owner",
            password_hash=passwords.hash(PASSWORD),
            platform_admin=True,
            jurisdiction="EU",
            revenue_band="lt_1m",
        )
    async with harness.services.db.session() as session:
        org = await session.get_one(Organization, result.org_id)
        user = await session.get_one(User, result.user_id)
        role = (await session.execute(sa.select(Membership.role).where(Membership.user_id == user.id))).scalar_one()
    assert org.is_demo is False and org.plan == "standard"
    assert user.email == "owner.bootstrap@example.com" and user.is_platform_admin and role == "owner"
    client, _ = await harness.login("owner.bootstrap@example.com", PASSWORD)
    assert (await client.get("/v1/creators")).json()["items"] == []  # a clean, empty org

    async with harness.services.db.transaction() as session:
        with pytest.raises(ValueError, match="already exists"):
            await bootstrap_org(
                session,
                org_name="Another org",
                email="owner.bootstrap@example.com",
                name="",
                password_hash="x",
                platform_admin=False,
                jurisdiction="EU",
                revenue_band="lt_1m",
            )


@pytest.mark.infra
async def test_purge_removes_demo_orgs_only(harness: ApiHarness, owner: ApiTenant) -> None:
    await _seed(harness)
    s = harness.services.settings
    async with harness.services.db.transaction() as session:  # a real org's row survives
        session.add(Creator(org_id=owner.org_id, name="Real creator"))
    async with harness.services.db.transaction() as session:
        plan = await plan_purge(session, assets_bucket=s.s3_bucket_assets, artifacts_bucket=s.s3_bucket_artifacts)
    assert [org_id for org_id, _ in plan.orgs] == [ALEX.ORG_ID]
    assert plan.rows["creators"] >= 1 and plan.rows["assets"] >= 6
    async with harness.services.db.session() as session:  # the shared test database: another test may
        real = (  # have made the dev admin a member of a real org, which must keep the user
            await session.execute(
                sa.select(sa.func.count()).where(Membership.user_id == ALEX.USER_ID, Membership.org_id != ALEX.ORG_ID)
            )
        ).scalar_one()
    assert ((ALEX.USER_ID, DEV_ADMIN_EMAIL) in plan.users_delete) is (real == 0)
    assert all(key.startswith(f"orgs/{ALEX.ORG_ID}/") for _, key in plan.objects)

    async with harness.services.db.transaction() as session:
        result = await apply_purge(session, plan)
    assert result["orgs"] == 1
    assert result["users_deleted"] + result["users_deactivated"] == len(plan.users_delete)
    async with harness.services.db.session() as session:
        assert await session.get(Organization, ALEX.ORG_ID) is None
        assert (await session.execute(sa.select(sa.func.count()).where(Asset.org_id == ALEX.ORG_ID))).scalar_one() == 0
        assert await session.get(Organization, owner.org_id) is not None
        names = (await session.execute(sa.select(Creator.name).where(Creator.org_id == owner.org_id))).scalars().all()
        assert names == ["Real creator"]
        assert await session.get(User, owner.user_id) is not None
        if real:
            assert await session.get(User, ALEX.USER_ID) is not None
    async with harness.services.db.transaction() as session:
        again = await plan_purge(session, assets_bucket=s.s3_bucket_assets, artifacts_bucket=s.s3_bucket_artifacts)
    assert again.empty and again.describe() == ["no demo organization: nothing to purge"]


def test_purge_cli_is_a_dry_run_without_apply() -> None:
    result = CliRunner().invoke(app, ["data", "purge-demo", "--help"])
    assert result.exit_code == 0 and "--apply" in result.output
    result = CliRunner().invoke(app, ["admin", "bootstrap", "--help"])
    assert result.exit_code == 0 and "--org-name" in result.output
