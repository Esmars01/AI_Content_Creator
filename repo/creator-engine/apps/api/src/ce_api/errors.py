"""API-level errors (mapped to problem+json like every `CEError`)."""

from __future__ import annotations

from ce_core.errors import CEError, ConflictError

__all__ = [
    "ApprovalBlockedError",
    "CsrfError",
    "NotYetImplementedError",
    "RateLimitedError",
    "UnauthenticatedError",
]


class UnauthenticatedError(CEError):
    code = "unauthenticated"
    status = 401
    title = "Authentication required"


class CsrfError(CEError):
    code = "csrf"
    status = 403
    title = "Missing or invalid CSRF token"


class RateLimitedError(CEError):
    code = "rate_limited"
    status = 429
    title = "Too many requests"


class NotYetImplementedError(CEError):
    """An endpoint of the spec whose behaviour arrives in a later phase (rule 5: fail loudly)."""

    code = "not_implemented"
    status = 501
    title = "Not implemented yet"

    def __init__(self, what: str, phase: int | str) -> None:
        super().__init__(f"{what} arrives in phase {phase} (see ROADMAP.md)", phase=phase)


class ApprovalBlockedError(ConflictError):
    """`:approve` found missing or failed recorded results; `issues` lists each one (§10.2)."""

    code = "approval_blocked"
    title = "Approval requirements are not met"
