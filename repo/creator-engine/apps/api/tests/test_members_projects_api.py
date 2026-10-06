"""Members, invitations and projects (§30): RBAC, last-owner protection, audit, pagination."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant
from ce_db.models.platform import AuditLog

pytestmark = pytest.mark.infra


async def test_members_and_role_changes(harness: ApiHarness, owner: ApiTenant) -> None:
    editor = await harness.add_member(owner.org_id, "editor")
    admin = await harness.add_member(owner.org_id, "admin")
    members = (await owner.client.get("/v1/members")).json()
    assert {m["role"] for m in members} == {"owner", "editor", "admin"}
    # editors read but cannot manage
    assert (await editor.client.get("/v1/members")).status_code == 200
    assert (await editor.client.patch(f"/v1/members/{admin.user_id}", json={"role": "viewer"})).status_code == 403
    # admins manage everyone below owner, never the owner role
    changed = await admin.client.patch(f"/v1/members/{editor.user_id}", json={"role": "viewer"})
    assert changed.status_code == 200 and changed.json()["role"] == "viewer"
    assert (await admin.client.patch(f"/v1/members/{editor.user_id}", json={"role": "owner"})).status_code == 403
    assert (await admin.client.patch(f"/v1/members/{owner.user_id}", json={"role": "admin"})).status_code == 403
    # the last owner stays
    last = await owner.client.patch(f"/v1/members/{owner.user_id}", json={"role": "admin"})
    assert last.status_code == 409 and last.json()["code"] == "conflict"
    async with harness.services.db.session() as session:
        actions = (
            await session.execute(
                sa.select(AuditLog.action, AuditLog.before, AuditLog.after).where(
                    AuditLog.org_id == owner.org_id, AuditLog.action == "member.role_change"
                )
            )
        ).all()
    assert [(a, b, c) for a, b, c in actions] == [("member.role_change", {"role": "editor"}, {"role": "viewer"})]


async def test_removing_a_member_revokes_their_sessions(harness: ApiHarness, owner: ApiTenant) -> None:
    editor = await harness.add_member(owner.org_id, "editor")
    assert (await editor.client.get("/v1/me")).status_code == 200
    assert (await owner.client.delete(f"/v1/members/{editor.user_id}")).status_code == 204
    assert (await editor.client.get("/v1/me")).status_code == 401
    assert (await owner.client.delete(f"/v1/members/{owner.user_id}")).status_code == 409


async def test_invitations(harness: ApiHarness, owner: ApiTenant) -> None:
    invited = await owner.client.post("/v1/invitations", json={"email": "Sam@Example.test", "role": "editor"})
    assert invited.status_code == 201, invited.text
    token = invited.json()["token"]
    assert invited.json()["email"] == "sam@example.test"
    anonymous = harness.client()
    weak = await anonymous.post(f"/v1/invitations/{token}:accept", json={"name": "Sam", "password": "short"})
    assert weak.status_code == 422
    accepted = await anonymous.post(
        f"/v1/invitations/{token}:accept", json={"name": "Sam", "password": "a long dev password"}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "editor"
    sam, _ = await harness.login("sam@example.test", "a long dev password")
    assert (await sam.get("/v1/me")).json()["role"] == "editor"
    again = await anonymous.post(f"/v1/invitations/{token}:accept", json={"password": "a long dev password"})
    assert again.status_code == 404
    duplicate = await owner.client.post("/v1/invitations", json={"email": "sam@example.test", "role": "viewer"})
    assert duplicate.status_code == 409


async def test_invitation_for_an_existing_user_needs_that_user(harness: ApiHarness, owner: ApiTenant) -> None:
    other = await harness.new_tenant()
    token = (await owner.client.post("/v1/invitations", json={"email": other.email, "role": "viewer"})).json()["token"]
    assert (await harness.client().post(f"/v1/invitations/{token}:accept", json={})).status_code == 401
    stranger = await harness.new_tenant()
    assert (await stranger.client.post(f"/v1/invitations/{token}:accept", json={})).status_code == 403
    accepted = await other.client.post(f"/v1/invitations/{token}:accept", json={})
    assert accepted.status_code == 200 and accepted.json()["org_id"] == str(owner.org_id)
    me = (await other.client.get("/v1/me")).json()
    assert {m["org_id"] for m in me["memberships"]} == {str(owner.org_id), str(other.org_id)}


async def test_admins_cannot_invite_owners(harness: ApiHarness, owner: ApiTenant) -> None:
    admin = await harness.add_member(owner.org_id, "admin")
    assert (
        await admin.client.post("/v1/invitations", json={"email": "x@example.test", "role": "owner"})
    ).status_code == 403
    assert (
        await admin.client.post("/v1/invitations", json={"email": "x@example.test", "role": "admin"})
    ).status_code == 201


async def test_projects_crud_and_pagination(harness: ApiHarness, owner: ApiTenant) -> None:
    ids = []
    for n in range(5):
        response = await owner.client.post("/v1/projects", json={"name": f"Project {n}", "budget_usd": "12.50"})
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await owner.client.get("/v1/projects", params=params)).json()
        seen += [p["id"] for p in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and seen == list(reversed(ids))  # newest first, no duplicates or gaps
    patched = await owner.client.patch(f"/v1/projects/{ids[0]}", json={"description": "updated"})
    assert patched.json()["description"] == "updated" and patched.json()["name"] == "Project 0"
    assert (await owner.client.delete(f"/v1/projects/{ids[0]}")).status_code == 204
    listed = (await owner.client.get("/v1/projects", params={"limit": 50})).json()["items"]
    assert ids[0] not in [p["id"] for p in listed]
    archived = (await owner.client.get("/v1/projects", params={"include_archived": True})).json()["items"]
    assert ids[0] in [p["id"] for p in archived]
    assert (await owner.client.get("/v1/projects", params={"cursor": "garbage"})).status_code == 422


async def test_viewers_read_but_do_not_write(harness: ApiHarness, owner: ApiTenant) -> None:
    viewer = await harness.add_member(owner.org_id, "viewer")
    project = (await owner.client.post("/v1/projects", json={"name": "Shared"})).json()
    assert (await viewer.client.get(f"/v1/projects/{project['id']}")).status_code == 200
    denied = await viewer.client.post("/v1/projects", json={"name": "Nope"})
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    assert (await viewer.client.patch(f"/v1/projects/{project['id']}", json={"name": "x"})).status_code == 403


async def test_video_creation_needs_an_input(owner: ApiTenant) -> None:
    """Planning itself is covered in `test_planning_api.py` (Phase 4)."""
    project = (await owner.client.post("/v1/projects", json={"name": "P"})).json()
    response = await owner.client.post(f"/v1/projects/{project['id']}/videos", json={})
    assert response.status_code == 422 and response.json()["code"] == "invalid_input"
