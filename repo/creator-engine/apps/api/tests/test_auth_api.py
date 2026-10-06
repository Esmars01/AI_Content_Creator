"""Auth: login, me, logout, sessions, CSRF, API keys, rate limiting, problem+json (§30, §33)."""

from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant
from ce_db.models.tenancy import Session as SessionRow

pytestmark = pytest.mark.infra


def problem(response: object, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status, response.text  # type: ignore[attr-defined]
    assert response.headers["content-type"].startswith("application/problem+json")  # type: ignore[attr-defined]
    body: dict[str, object] = response.json()  # type: ignore[attr-defined]
    assert body["type"] == f"urn:ce:problem:{code}" and body["code"] == code and body["status"] == status
    return body


async def test_login_sets_secure_session_cookies(harness: ApiHarness, owner: ApiTenant) -> None:
    client = harness.client()
    response = await client.post("/v1/auth/login", json={"email": owner.email, "password": owner.password})
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("ce_session="))
    csrf_cookie = next(c for c in cookies if c.startswith("ce_csrf="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "Secure" not in session_cookie  # COOKIE_SECURE=false in test (env/test.yaml)
    assert "HttpOnly" not in csrf_cookie  # the web app reads it
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["csrf_token"] and body["role"] == "owner"
    assert [m["org_id"] for m in body["memberships"]] == [str(owner.org_id)]


async def test_me_and_logout_revoke_the_session(harness: ApiHarness, owner: ApiTenant) -> None:
    me = await owner.client.get("/v1/me")
    assert me.status_code == 200
    assert (me.json()["user"]["email"], me.json()["auth"]) == (owner.email, "session")
    token = owner.client.cookies["ce_session"]
    assert (await owner.client.post("/v1/auth/logout")).status_code == 204
    problem(await owner.client.get("/v1/me"), 401, "unauthenticated")
    replay = harness.client()
    replay.cookies.set("ce_session", token)
    problem(await replay.get("/v1/me"), 401, "unauthenticated")  # revoked server-side, not just forgotten


async def test_bad_credentials_are_indistinguishable(harness: ApiHarness, owner: ApiTenant) -> None:
    client = harness.client()
    wrong = problem(
        await client.post("/v1/auth/login", json={"email": owner.email, "password": "wrong password"}),
        401,
        "unauthenticated",
    )
    missing = problem(
        await client.post("/v1/auth/login", json={"email": "nobody@example.test", "password": "x"}),
        401,
        "unauthenticated",
    )
    assert wrong["detail"] == missing["detail"] == "invalid email or password"


async def test_login_into_a_foreign_org_is_refused(harness: ApiHarness, owner: ApiTenant) -> None:
    other = await harness.new_tenant()
    body = {"email": owner.email, "password": owner.password, "org_id": str(other.org_id)}
    problem(await harness.client().post("/v1/auth/login", json=body), 401, "unauthenticated")


async def test_expired_sessions_are_refused(harness: ApiHarness, owner: ApiTenant) -> None:
    async with harness.services.db.transaction() as session:
        await session.execute(
            sa.update(SessionRow)
            .where(SessionRow.user_id == owner.user_id)
            .values(expires_at=sa.func.now() - timedelta(seconds=1))
        )
    problem(await owner.client.get("/v1/me"), 401, "unauthenticated")


async def test_cookie_mutations_need_the_csrf_token(owner: ApiTenant) -> None:
    good = owner.client.headers.pop("X-CSRF-Token")
    problem(await owner.client.post("/v1/projects", json={"name": "No token"}), 403, "csrf")
    owner.client.headers["X-CSRF-Token"] = "0" * 64
    problem(await owner.client.post("/v1/projects", json={"name": "Wrong token"}), 403, "csrf")
    owner.client.headers["X-CSRF-Token"] = good
    assert (await owner.client.post("/v1/projects", json={"name": "Right token"})).status_code == 201


async def test_api_keys(harness: ApiHarness, owner: ApiTenant) -> None:
    created = await owner.client.post("/v1/api-keys", json={"scopes": ["read"]})
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert key.startswith("ce_key_") and created.json()["prefix"] in key
    bot = harness.client()
    bot.headers["Authorization"] = f"Bearer {key}"
    me = await bot.get("/v1/me")
    assert (me.status_code, me.json()["auth"], me.json()["role"]) == (200, "api_key", "owner")
    problem(await bot.post("/v1/projects", json={"name": "read-only key"}), 403, "forbidden")  # scope, not role
    listed = await owner.client.get("/v1/api-keys")
    assert [k["id"] for k in listed.json()] == [created.json()["id"]] and "key" not in listed.json()[0]
    assert listed.json()[0]["last_used_at"] is not None
    assert (await owner.client.delete(f"/v1/api-keys/{created.json()['id']}")).status_code == 204
    problem(await bot.get("/v1/me"), 401, "unauthenticated")
    bot.headers["Authorization"] = "Bearer ce_key_short"
    problem(await bot.get("/v1/me"), 401, "unauthenticated")
    bot.headers["Authorization"] = key.replace("ce_key_", "Basic ")
    problem(await bot.get("/v1/me"), 401, "unauthenticated")


async def test_write_keys_cannot_mint_more_than_their_scope(harness: ApiHarness, owner: ApiTenant) -> None:
    write_key = (await owner.client.post("/v1/api-keys", json={"scopes": ["read", "write"]})).json()["key"]
    read_key = (await owner.client.post("/v1/api-keys", json={"scopes": ["read"]})).json()["key"]
    bot = harness.client()
    bot.headers["Authorization"] = f"Bearer {write_key}"
    assert (await bot.post("/v1/projects", json={"name": "via key"})).status_code == 201  # no CSRF for bearer keys
    reader = harness.client()
    reader.headers["Authorization"] = f"Bearer {read_key}"
    problem(await reader.post("/v1/api-keys", json={"scopes": ["read", "write"]}), 403, "forbidden")


async def test_editors_cannot_manage_api_keys(harness: ApiHarness, owner: ApiTenant) -> None:
    editor = await harness.add_member(owner.org_id, "editor")
    problem(await editor.client.post("/v1/api-keys", json={"scopes": ["read"]}), 403, "forbidden")
    developer = await harness.add_member(owner.org_id, "developer")
    assert (await developer.client.post("/v1/api-keys", json={"scopes": ["read"]})).status_code == 201


async def test_login_is_rate_limited(harness: ApiHarness, owner: ApiTenant) -> None:
    services = harness.services
    app = services.effective.bundle.app
    strict = app.model_copy(update={"security": app.security.model_copy(update={"login_rate_limit_per_minute": 3})})
    services.effective.bundle.__dict__["app"] = strict  # frozen dataclass: tests may swap the config
    client = harness.client(client_ip=f"10.{secrets.randbelow(250)}.{secrets.randbelow(250)}.{secrets.randbelow(250)}")
    statuses = [
        (await client.post("/v1/auth/login", json={"email": owner.email, "password": "nope"})).status_code
        for _ in range(5)
    ]
    # The fixture's own login counts toward the per-email window, so the limit may hit one attempt early.
    assert statuses.count(401) in (2, 3) and statuses[-1] == 429 and set(statuses) == {401, 429}
    limited = problem(
        await client.post("/v1/auth/login", json={"email": owner.email, "password": owner.password}),
        429,
        "rate_limited",
    )
    assert limited["detail"]
    services.effective.bundle.__dict__["app"] = app


async def test_validation_errors_are_problems_with_issue_paths(owner: ApiTenant) -> None:
    body = problem(await owner.client.post("/v1/projects", json={"name": "", "unknown": 1}), 422, "invalid_input")
    issues: list[dict[str, str]] = body["issues"]  # type: ignore[assignment]
    paths = sorted(i["path"] for i in issues)
    assert paths == ["/body/name", "/body/unknown"]


async def test_unknown_routes_are_problems(owner: ApiTenant) -> None:
    problem(await owner.client.get("/v1/nothing-here"), 404, "not_found")


async def test_responses_carry_request_ids_and_security_headers(owner: ApiTenant) -> None:
    response = await owner.client.get("/v1/me", headers={"X-Request-ID": "trace-me-123"})
    assert response.headers["x-request-id"] == "trace-me-123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert len((await owner.client.get("/v1/me")).headers["x-request-id"]) == 36
