"""Authentication (§33): the `AuthProvider` interface, its password implementation, server-side
sessions (httpOnly cookie → `sessions` row) and API keys (`Authorization: Bearer ce_key_…`).

The OIDC provider is V1 (§40 Phase 1 scope); it implements the same interface.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

import sqlalchemy as sa
from ce_db.models.tenancy import ApiKey, Membership, Organization, Session, User
from ce_db.repository import OrgContext
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.errors import UnauthenticatedError
from ce_api.security.passwords import Passwords
from ce_api.security.rbac import Permission, role_has
from ce_api.security.tokens import hash_token, new_token, parse_api_key

__all__ = [
    "AuthProvider",
    "Credentials",
    "IssuedSession",
    "PasswordAuthProvider",
    "Principal",
    "create_session",
    "resolve_api_key",
    "resolve_session",
    "revoke_session",
]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    """Who is calling, in which org, with which role."""

    user_id: UUID
    email: str
    org_id: UUID
    role: str
    is_platform_admin: bool
    via: Literal["session", "api_key"]
    session_id: UUID | None = None
    api_key_id: UUID | None = None
    scopes: frozenset[str] = frozenset({"read", "write"})
    org_is_demo: bool = False  # the dev seed's org (migration 0006); production refuses it

    @property
    def ctx(self) -> OrgContext:
        return OrgContext(org_id=self.org_id, user_id=self.user_id, is_platform_admin=self.is_platform_admin)

    @property
    def actor_kind(self) -> str:
        return "user" if self.via == "session" else "api_key"

    def allows(self, permission: Permission, method: str) -> bool:
        scope = "read" if method.upper() in SAFE_METHODS else "write"
        return role_has(self.role, permission) and scope in self.scopes


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str


class AuthProvider(Protocol):
    """Turns login credentials into a user. Implementations: password (Phase 1), OIDC (V1)."""

    name: str

    async def authenticate(self, session: AsyncSession, credentials: Credentials) -> User: ...


class PasswordAuthProvider:
    name = "password"

    def __init__(self, passwords: Passwords) -> None:
        self.passwords = passwords

    async def authenticate(self, session: AsyncSession, credentials: Credentials) -> User:
        email = credentials.email.strip().lower()
        user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one_or_none()
        # verify() also spends the hashing time when the user is missing (no user enumeration by timing).
        valid = self.passwords.verify(user.password_hash if user else None, credentials.password)
        if user is None or not valid or not user.is_active:
            raise UnauthenticatedError("invalid email or password")
        if user.password_hash and self.passwords.needs_rehash(user.password_hash):
            user.password_hash = self.passwords.hash(credentials.password)
        return user


async def _membership(
    session: AsyncSession, user_id: UUID, org_id: UUID | None, *, allow_demo: bool = True
) -> Membership | None:
    query = sa.select(Membership).where(Membership.user_id == user_id)
    if org_id is not None:
        query = query.where(Membership.org_id == org_id)
    if not allow_demo:
        query = query.join(Organization, Organization.id == Membership.org_id).where(Organization.is_demo.is_(False))
    query = query.order_by(Membership.created_at, Membership.org_id).limit(1)
    return (await session.execute(query)).scalar_one_or_none()


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session: Session
    membership: Membership
    organization: Organization


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    org_id: UUID | None,
    ttl_s: int,
    now: datetime,
    ip: str | None,
    user_agent: str | None,
    allow_demo: bool = True,
) -> IssuedSession:
    """`allow_demo=False` (production) never signs into a demo org: with no org asked, the oldest
    real membership is used; a demo org asked for by id answers like a missing one."""
    membership = await _membership(session, user.id, org_id, allow_demo=allow_demo)
    if membership is None:
        # Same answer for "no such org" and "not a member" (no tenant enumeration).
        raise UnauthenticatedError("the user is not a member of that organization")
    token = new_token()
    row = Session(
        org_id=membership.org_id,
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=now + timedelta(seconds=ttl_s),
        ip=ip,
        user_agent=(user_agent or "")[:512] or None,
    )
    session.add(row)
    user.last_login_at = now
    await session.flush()
    org = await session.get_one(Organization, membership.org_id)
    return IssuedSession(token, row, membership, org)


async def resolve_session(session: AsyncSession, token: str, *, now: datetime) -> Principal:
    query = (
        sa.select(Session, User, Membership, Organization.is_demo)
        .join(User, User.id == Session.user_id)
        .join(Membership, sa.and_(Membership.user_id == Session.user_id, Membership.org_id == Session.org_id))
        .join(Organization, Organization.id == Session.org_id)
        .where(Session.token_hash == hash_token(token))
    )
    row = (await session.execute(query)).first()
    if row is None:
        raise UnauthenticatedError("not signed in")
    stored, user, membership, is_demo = row
    if stored.revoked_at is not None or stored.expires_at <= now or not user.is_active:
        raise UnauthenticatedError("the session has expired")
    return Principal(
        user_id=user.id,
        email=user.email,
        org_id=stored.org_id,
        role=membership.role,
        is_platform_admin=user.is_platform_admin,
        via="session",
        session_id=stored.id,
        org_is_demo=bool(is_demo),
    )


async def revoke_session(session: AsyncSession, session_id: UUID, *, now: datetime) -> None:
    await session.execute(
        sa.update(Session).where(Session.id == session_id, Session.revoked_at.is_(None)).values(revoked_at=now)
    )


async def resolve_api_key(session: AsyncSession, value: str, *, now: datetime, namespace: str) -> Principal:
    prefix = parse_api_key(value, namespace)
    if prefix is None:
        raise UnauthenticatedError("malformed API key")
    query = (
        sa.select(ApiKey, User, Membership, Organization.is_demo)
        .join(User, User.id == ApiKey.user_id)
        .join(Membership, sa.and_(Membership.user_id == ApiKey.user_id, Membership.org_id == ApiKey.org_id))
        .join(Organization, Organization.id == ApiKey.org_id)
        .where(ApiKey.prefix == prefix)
    )
    row = (await session.execute(query)).first()
    if row is None or not hmac.compare_digest(row[0].hash, hash_token(value)):
        raise UnauthenticatedError("invalid API key")
    key, user, membership, is_demo = row
    if key.revoked_at is not None or (key.expires_at is not None and key.expires_at <= now) or not user.is_active:
        raise UnauthenticatedError("the API key is revoked or expired")
    key.last_used_at = now
    return Principal(
        user_id=user.id,
        email=user.email,
        org_id=key.org_id,
        role=membership.role,
        is_platform_admin=user.is_platform_admin,
        via="api_key",
        api_key_id=key.id,
        scopes=frozenset(key.scopes),
        org_is_demo=bool(is_demo),
    )
