"""Members and invitations (§30). Role changes and removals are audited (§33)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError, PermissionDeniedError
from ce_db.models.tenancy import ROLES as _ROLES
from ce_db.models.tenancy import ApiKey, Invitation, Membership, Session, User
from fastapi import APIRouter, Request, Response
from pydantic import Field

from ce_api.common import audit
from ce_api.deps import DbSession, MemberManager, OptionalPrincipal, Reader, ServicesDep
from ce_api.errors import UnauthenticatedError
from ce_api.schemas import Body, Out, examples
from ce_api.security.passwords import password_issues
from ce_api.security.rbac import can_assign_role
from ce_api.security.tokens import hash_token, new_token

router = APIRouter(tags=["members"])

Role = Literal["owner", "admin", "editor", "viewer", "developer"]
assert set(Role.__args__) == set(_ROLES)  # type: ignore[attr-defined]


class MemberOut(Out):
    user_id: UUID
    email: str
    name: str
    role: str
    joined_at: datetime


class RoleChange(Body):
    role: Role

    model_config = examples([{"role": "editor"}])


async def _members(session: DbSession, org_id: UUID) -> list[MemberOut]:
    rows = (
        await session.execute(
            sa.select(User.id, User.email, User.name, Membership.role, Membership.created_at)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org_id)
            .order_by(Membership.created_at)
        )
    ).all()
    return [MemberOut(user_id=r[0], email=r[1], name=r[2], role=r[3], joined_at=r[4]) for r in rows]


async def _membership(session: DbSession, org_id: UUID, user_id: UUID) -> Membership:
    row = (
        await session.execute(
            sa.select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("member not found")
    return row


async def _owner_count(session: DbSession, org_id: UUID) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count()).where(Membership.org_id == org_id, Membership.role == "owner")
            )
        ).scalar_one()
    )


@router.get("/v1/members", response_model=list[MemberOut])
async def list_members(principal: Reader, session: DbSession) -> list[MemberOut]:
    return await _members(session, principal.org_id)


@router.patch("/v1/members/{user_id}", response_model=MemberOut)
async def change_role(
    user_id: UUID, body: RoleChange, principal: MemberManager, request: Request, session: DbSession
) -> MemberOut:
    membership = await _membership(session, principal.org_id, user_id)
    if not can_assign_role(principal.role, membership.role, body.role):
        raise PermissionDeniedError("only owners grant, change or remove the owner role")
    if membership.role == "owner" and body.role != "owner" and await _owner_count(session, principal.org_id) <= 1:
        raise ConflictError("an organization keeps at least one owner")
    before = membership.role
    membership.role = body.role
    await audit(
        session,
        principal,
        "member.role_change",
        "user",
        user_id,
        request=request,
        before={"role": before},
        after={"role": body.role},
    )
    return next(m for m in await _members(session, principal.org_id) if m.user_id == user_id)


@router.delete("/v1/members/{user_id}", status_code=204)
async def remove_member(
    user_id: UUID, principal: MemberManager, request: Request, session: DbSession, services: ServicesDep
) -> Response:
    membership = await _membership(session, principal.org_id, user_id)
    if not can_assign_role(principal.role, membership.role, None):
        raise PermissionDeniedError("only owners remove owners")
    if membership.role == "owner" and await _owner_count(session, principal.org_id) <= 1:
        raise ConflictError("an organization keeps at least one owner")
    await session.delete(membership)
    await session.execute(
        sa.update(Session)
        .where(Session.org_id == principal.org_id, Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=services.clock())
    )
    # Their API keys too: keys authorize through the membership, so a later re-invitation (even as a
    # viewer) would otherwise bring the old keys back with their old scopes.
    await session.execute(
        sa.update(ApiKey)
        .where(ApiKey.org_id == principal.org_id, ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=services.clock())
    )
    await audit(session, principal, "member.remove", "user", user_id, request=request, before={"role": membership.role})
    return Response(status_code=204)


# ---------------------------------------------------------------------- invitations
class InvitationCreate(Body):
    email: Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")]
    role: Role = "editor"

    model_config = examples([{"email": "sam@example.com", "role": "editor"}])


class InvitationOut(Out):
    id: UUID
    email: str
    role: str
    expires_at: datetime
    accepted_at: datetime | None


class InvitationCreated(InvitationOut):
    token: str = Field(description="send to the invitee; shown once")


class InvitationAccept(Body):
    name: Annotated[str, Field(max_length=200)] = ""
    password: Annotated[str | None, Field(max_length=1024)] = Field(
        default=None, description="required when the invitee has no account yet"
    )

    model_config = examples([{"name": "Sam", "password": "correct horse battery"}, {}])


class AcceptedOut(Out):
    org_id: UUID
    user_id: UUID
    role: str


@router.post("/v1/invitations", response_model=InvitationCreated, status_code=201)
async def invite(
    body: InvitationCreate, principal: MemberManager, request: Request, session: DbSession, services: ServicesDep
) -> InvitationCreated:
    if not can_assign_role(principal.role, None, body.role):
        raise PermissionDeniedError("only owners invite owners")
    email = body.email.strip().lower()
    existing = (
        await session.execute(
            sa.select(Membership)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == principal.org_id, User.email == email)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("this person is already a member")
    token = new_token()
    row = Invitation(
        org_id=principal.org_id,
        email=email,
        role=body.role,
        token_hash=hash_token(token),
        expires_at=services.clock() + timedelta(seconds=services.config.security.invitation_ttl_s),
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    await audit(
        session,
        principal,
        "invitation.create",
        "invitation",
        row.id,
        request=request,
        after={"email": email, "role": body.role},
    )
    return InvitationCreated(**InvitationOut.model_validate(row).model_dump(), token=token)


@router.post("/v1/invitations/{token}:accept", response_model=AcceptedOut)
async def accept_invitation(
    token: str,
    body: InvitationAccept,
    principal: OptionalPrincipal,
    request: Request,
    session: DbSession,
    services: ServicesDep,
) -> AcceptedOut:
    invitation = (
        await session.execute(sa.select(Invitation).where(Invitation.token_hash == hash_token(token)).with_for_update())
    ).scalar_one_or_none()
    now = services.clock()
    if invitation is None or invitation.accepted_at is not None or invitation.expires_at <= now:
        raise NotFoundError("the invitation is invalid, used or expired")
    user = (await session.execute(sa.select(User).where(User.email == invitation.email))).scalar_one_or_none()
    if principal is not None:
        if principal.email != invitation.email:
            raise PermissionDeniedError("the invitation was sent to another email address")
        user = await session.get_one(User, principal.user_id)
    elif user is not None:
        raise UnauthenticatedError("sign in as the invited user to accept")
    else:
        if body.password is None:
            raise InvalidInputError(
                "a password is needed to create the account", issues=[Issue("password", "required")]
            )
        if issues := password_issues(body.password, services.config.security.password_min_length):
            raise InvalidInputError("the password is too weak", issues=issues)
        user = User(email=invitation.email, name=body.name, password_hash=services.passwords.hash(body.password))
        session.add(user)
        await session.flush()
    session.add(Membership(user_id=user.id, org_id=invitation.org_id, role=invitation.role))
    invitation.accepted_at = now
    await session.flush()
    await audit(
        session,
        principal,
        "invitation.accept",
        "invitation",
        invitation.id,
        request=request,
        org_id=invitation.org_id,
        after={"user_id": str(user.id), "role": invitation.role},
    )
    return AcceptedOut(org_id=invitation.org_id, user_id=user.id, role=invitation.role)
