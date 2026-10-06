"""Auth and API keys (§30): login, logout, me, API keys."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import InvalidInputError, Issue, NotFoundError, PermissionDeniedError
from ce_db.models.tenancy import ApiKey, Membership, Organization, User
from fastapi import APIRouter, Request, Response
from pydantic import Field

from ce_api.common import audit
from ce_api.deps import AnyCaller, DbSession, KeyManager, ServicesDep
from ce_api.errors import RateLimitedError
from ce_api.schemas import Body, Out, examples
from ce_api.security.auth import Credentials, PasswordAuthProvider, create_session, revoke_session
from ce_api.security.rbac import API_KEY_SCOPES
from ce_api.security.tokens import csrf_token, new_api_key

router = APIRouter(tags=["auth"])


class LoginRequest(Body):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]
    org_id: UUID | None = Field(default=None, description="sign into this org; default: the oldest membership")

    model_config = examples([{"email": "admin@creator-engine.local", "password": "correct horse battery"}])


class UserOut(Out):
    id: UUID
    email: str
    name: str
    is_platform_admin: bool


class OrgOut(Out):
    id: UUID
    name: str
    plan: str


class MembershipOut(Out):
    org_id: UUID
    org_name: str
    role: str


class MeOut(Out):
    user: UserOut
    org: OrgOut
    role: str
    auth: Literal["session", "api_key"]
    memberships: list[MembershipOut]


class LoginOut(MeOut):
    csrf_token: str = Field(description="echo in X-CSRF-Token on cookie-authenticated changes")
    expires_at: datetime


async def _rate_limit(services: ServicesDep, *subjects: str) -> None:
    limit = services.config.security.login_rate_limit_per_minute
    window = int(services.clock().timestamp() // 60)
    for subject in subjects:
        key = f"ce:rl:login:{hashlib.sha256(subject.encode()).hexdigest()[:32]}:{window}"
        count = await services.redis.incr(key)
        if count == 1:
            await services.redis.expire(key, 120)
        if count > limit:
            raise RateLimitedError("too many login attempts; try again in a minute", retry_after_s=60)


async def _me(session: DbSession, user: User, org_id: UUID, role: str, via: Literal["session", "api_key"]) -> MeOut:
    org = await session.get_one(Organization, org_id)
    rows = (
        await session.execute(
            sa.select(Membership.org_id, Organization.name, Membership.role)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user.id)
            .order_by(Organization.name)
        )
    ).all()
    return MeOut(
        user=UserOut.model_validate(user),
        org=OrgOut.model_validate(org),
        role=role,
        auth=via,
        memberships=[MembershipOut(org_id=r[0], org_name=r[1], role=r[2]) for r in rows],
    )


@router.post("/v1/auth/login", response_model=LoginOut)
async def login(
    body: LoginRequest, request: Request, response: Response, session: DbSession, services: ServicesDep
) -> LoginOut:
    ip = request.client.host if request.client else "unknown"
    await _rate_limit(services, f"email:{body.email.strip().lower()}", f"ip:{ip}")
    provider = PasswordAuthProvider(services.passwords)
    user = await provider.authenticate(session, Credentials(body.email, body.password))
    security = services.config.security
    issued = await create_session(
        session,
        user,
        org_id=body.org_id,
        ttl_s=security.session_ttl_s,
        now=services.clock(),
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    secure = services.effective.cookie_secure
    csrf = csrf_token(services.signing_secret, str(issued.session.id))
    response.set_cookie(
        security.session_cookie,
        issued.token,
        max_age=security.session_ttl_s,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        security.csrf_cookie,
        csrf,
        max_age=security.session_ttl_s,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    me = await _me(session, user, issued.membership.org_id, issued.membership.role, "session")
    return LoginOut(**me.model_dump(), csrf_token=csrf, expires_at=issued.session.expires_at)


@router.post("/v1/auth/logout", status_code=204)
async def logout(principal: AnyCaller, response: Response, session: DbSession, services: ServicesDep) -> Response:
    if principal.session_id is not None:
        await revoke_session(session, principal.session_id, now=services.clock())
    security = services.config.security
    response.delete_cookie(security.session_cookie, path="/")
    response.delete_cookie(security.csrf_cookie, path="/")
    response.status_code = 204
    return response


@router.get("/v1/me", response_model=MeOut)
async def me(principal: AnyCaller, session: DbSession) -> MeOut:
    user = await session.get_one(User, principal.user_id)
    return await _me(session, user, principal.org_id, principal.role, principal.via)


# ---------------------------------------------------------------------- API keys
class ApiKeyCreate(Body):
    scopes: list[Literal["read", "write"]] = Field(default_factory=lambda: ["read"], min_length=1)  # type: ignore[arg-type]
    expires_at: datetime | None = None

    model_config = examples([{"scopes": ["read", "write"]}])


class ApiKeyOut(Out):
    id: UUID
    prefix: str
    user_id: UUID
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="the full key; shown once, never stored")


@router.post("/v1/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    body: ApiKeyCreate, principal: KeyManager, request: Request, session: DbSession, services: ServicesDep
) -> ApiKeyCreated:
    if principal.via == "api_key" and not set(body.scopes) <= principal.scopes:
        raise PermissionDeniedError("an API key cannot create a key with more scopes than its own")
    if body.expires_at is not None and body.expires_at <= services.clock():
        raise InvalidInputError("expires_at is in the past", issues=[Issue("expires_at", "must be in the future")])
    secret = new_api_key(services.config.security.api_key_prefix)
    row = ApiKey(
        org_id=principal.org_id,
        user_id=principal.user_id,
        prefix=secret.prefix,
        hash=secret.hash,
        scopes=sorted(set(body.scopes) & set(API_KEY_SCOPES)),
        expires_at=body.expires_at,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit(session, principal, "api_key.create", "api_key", row.id, request=request, after={"scopes": row.scopes})
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), key=secret.secret)


@router.get("/v1/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(principal: KeyManager, session: DbSession) -> list[ApiKeyOut]:
    query = sa.select(ApiKey).where(ApiKey.org_id == principal.org_id).order_by(ApiKey.created_at.desc())
    if principal.role not in ("admin", "owner"):
        query = query.where(ApiKey.user_id == principal.user_id)
    return [ApiKeyOut.model_validate(k) for k in (await session.execute(query)).scalars()]


@router.delete("/v1/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: UUID, principal: KeyManager, request: Request, session: DbSession, services: ServicesDep
) -> Response:
    key = (
        await session.execute(sa.select(ApiKey).where(ApiKey.org_id == principal.org_id, ApiKey.id == key_id))
    ).scalar_one_or_none()
    if key is None or (key.user_id != principal.user_id and principal.role not in ("admin", "owner")):
        raise NotFoundError("api key not found")
    if key.revoked_at is None:
        key.revoked_at = services.clock()
        await audit(session, principal, "api_key.revoke", "api_key", key.id, request=request)
    return Response(status_code=204)


__all__ = ["router"]
