"""Every state-changing route names a permission stronger than `read` (Phase 14 security review).

Read-only computations sent as POST and the unauthenticated entry points are listed explicitly;
a new mutating route that forgets its permission fails here.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from ce_api.app import create_app
from fastapi.routing import APIRoute

# route → why it needs no write permission
EXCEPTIONS = {
    ("POST", "/v1/auth/login"): "unauthenticated by definition; rate-limited per email and per client address",
    ("POST", "/v1/auth/logout"): "ends the caller's own session",
    ("POST", "/v1/invitations/{token}:accept"): "authorized by the invitation token",
    ("PUT", "/v1/storage/local/{path:path}"): "dev/test local storage: HMAC-signed, expiring presigned URL",
    ("POST", "/v1/estimates"): "read-only computation (an estimate), nothing is written",
    ("POST", "/v1/spec-templates:compose"): "read-only computation (a composition preview), nothing is written",
}
WEAK = {"read", "get_principal", "get_optional_principal"}


def _routes(routes: Iterable[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):  # included routers
            yield from _routes(route.original_router.routes)


def _permissions(dependant: Any, found: set[str]) -> set[str]:
    for dep in dependant.dependencies:
        name = getattr(dep.call, "__qualname__", "")
        if name == "require.<locals>.dependency":
            found.add(dep.call.__closure__[0].cell_contents.value)
        elif name in ("get_platform_admin", "get_principal", "get_optional_principal"):
            found.add(name)
        _permissions(dep, found)
    return found


def test_every_mutating_route_requires_more_than_read() -> None:
    weak = set()
    mutating = 0
    for route in _routes(create_app().routes):
        if not route.path.startswith("/v1/"):
            continue
        for method in (route.methods or set()) - {"GET", "HEAD", "OPTIONS"}:
            mutating += 1
            if not _permissions(route.dependant, set()) - WEAK:
                weak.add((method, route.path))
    assert mutating > 100
    assert weak - set(EXCEPTIONS) == set(), "add a permission (Writer, Approver, …) or justify an exception"
    assert set(EXCEPTIONS) - weak == set(), "stale exception"
