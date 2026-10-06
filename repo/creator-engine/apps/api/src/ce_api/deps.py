"""Request dependencies: services, the per-request transaction, the caller, permissions.

The database session is a *function-scoped* dependency: its transaction commits when the
endpoint returns and before the response is sent, so a failed commit is reported to the
client, and background work scheduled by the endpoint sees committed rows.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.errors import CsrfError, UnauthenticatedError
from ce_api.security.auth import SAFE_METHODS, Principal, resolve_api_key, resolve_session
from ce_api.security.rbac import Permission
from ce_api.security.tokens import csrf_token
from ce_api.services import Services

__all__ = [
    "CSRF_HEADER",
    "AnyCaller",
    "Approver",
    "DbSession",
    "KeyManager",
    "MemberManager",
    "OptionalPrincipal",
    "PlatformAdmin",
    "Reader",
    "ServicesDep",
    "Writer",
    "get_services",
    "require",
]

CSRF_HEADER = "X-CSRF-Token"


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    from ce_db.errors import translate

    async with services.db.sessions() as session:
        try:
            async with session.begin():
                yield session
        except DBAPIError as exc:
            raise translate(exc) from exc


DbSession = Annotated[AsyncSession, Depends(get_session, scope="function")]


async def get_optional_principal(request: Request, services: ServicesDep, session: DbSession) -> Principal | None:
    now = services.clock()
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise UnauthenticatedError("use 'Authorization: Bearer <API key>'")
        principal = await resolve_api_key(
            session, value.strip(), now=now, namespace=services.config.security.api_key_prefix
        )
        request.state.principal = principal
        return principal
    token = request.cookies.get(services.config.security.session_cookie)
    if not token:
        return None
    principal = await resolve_session(session, token, now=now)
    if request.method.upper() not in SAFE_METHODS:
        expected = csrf_token(services.signing_secret, str(principal.session_id))
        if not hmac.compare_digest(request.headers.get(CSRF_HEADER, ""), expected):
            raise CsrfError("cookie-authenticated changes need the X-CSRF-Token header")
    request.state.principal = principal
    return principal


OptionalPrincipal = Annotated[Principal | None, Depends(get_optional_principal, scope="function")]


async def get_principal(principal: OptionalPrincipal) -> Principal:
    if principal is None:
        raise UnauthenticatedError("not signed in")
    return principal


def require(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the caller, if their role (and API-key scope) grants `permission`."""

    async def dependency(
        request: Request, principal: Annotated[Principal, Depends(get_principal, scope="function")]
    ) -> Principal:
        from ce_core.errors import PermissionDeniedError

        if not principal.allows(permission, request.method):
            raise PermissionDeniedError(f"your role ({principal.role}) does not allow {permission.value}")
        return principal

    return dependency


def caller(permission: Permission) -> object:
    """`Annotated[Principal, caller(Permission.X)]` in endpoint signatures."""
    return Depends(require(permission), scope="function")


Reader = Annotated[Principal, caller(Permission.READ)]
Writer = Annotated[Principal, caller(Permission.WRITE_CONTENT)]
Approver = Annotated[Principal, caller(Permission.APPROVE_IDENTITY)]
KeyManager = Annotated[Principal, caller(Permission.MANAGE_API_KEYS)]
MemberManager = Annotated[Principal, caller(Permission.MANAGE_MEMBERS)]
AnyCaller = Annotated[Principal, Depends(get_principal, scope="function")]


async def get_platform_admin(principal: Annotated[Principal, Depends(get_principal, scope="function")]) -> Principal:
    """`/v1/admin/*` (§30): platform admins only, whatever their org role."""
    from ce_core.errors import PermissionDeniedError

    if not principal.is_platform_admin:
        raise PermissionDeniedError("platform administrators only")
    return principal


PlatformAdmin = Annotated[Principal, Depends(get_platform_admin, scope="function")]
