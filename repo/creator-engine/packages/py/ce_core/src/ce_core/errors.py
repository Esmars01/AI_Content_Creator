"""Domain errors. The API maps them to RFC 9457 problem+json responses (§30)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "CEError",
    "ConflictError",
    "ImmutableRecordError",
    "InvalidInputError",
    "Issue",
    "LockedPathError",
    "NotFoundError",
    "PermissionDeniedError",
    "PolicyDeniedError",
    "SpecValidationError",
]


@dataclass(frozen=True, slots=True)
class Issue:
    """One validation finding, addressed by a SpecPath when it concerns a spec element."""

    code: str
    message: str
    path: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    severity: Literal["error", "warning"] = "error"


class CEError(Exception):
    """Base class. `code` is a stable machine-readable identifier; `status` the HTTP status."""

    code = "error"
    status = 500
    title = "Internal error"

    def __init__(self, message: str, *, issues: list[Issue] | None = None, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.issues = issues or []
        self.detail = detail


class InvalidInputError(CEError):
    """A request value is malformed (not a spec problem): a key, a filename, a size, a media type."""

    code = "invalid_input"
    status = 422
    title = "Invalid input"


class SpecValidationError(CEError):
    code = "spec_invalid"
    status = 422
    title = "The spec is invalid"


class NotFoundError(CEError):
    code = "not_found"
    status = 404
    title = "Not found"


class ConflictError(CEError):
    code = "conflict"
    status = 409
    title = "Conflict"


class ImmutableRecordError(ConflictError):
    """An attempt to change an approved version or an identity column (I3)."""

    code = "immutable"
    title = "Approved records are immutable"


class LockedPathError(ConflictError):
    """A patch touches a locked SpecPath (I8). `detail['lock_group']` names the group."""

    code = "locked"
    title = "The path is locked"


class PermissionDeniedError(CEError):
    code = "forbidden"
    status = 403
    title = "Forbidden"


class PolicyDeniedError(CEError):
    code = "policy_denied"
    status = 403
    title = "Denied by policy"


class UpstreamUnavailableError(CEError):
    """An internal service the request needs (the scheduler's fleet endpoints) did not answer."""

    code = "upstream_unavailable"
    status = 503
    title = "A required service is unavailable"
