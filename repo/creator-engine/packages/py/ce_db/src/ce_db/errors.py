"""Maps database errors (including the V1 rule SQLSTATEs) to domain errors."""

from __future__ import annotations

from ce_core.errors import CEError, ConflictError, ImmutableRecordError, NotFoundError
from sqlalchemy.exc import DBAPIError

__all__ = ["sqlstate", "translate"]


def sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def translate(exc: DBAPIError) -> CEError:
    code = sqlstate(exc)
    message = str(exc.orig).split("\n")[0]
    if code in ("CE001", "CE002"):
        return ImmutableRecordError(message)
    if code == "CE003":
        return ConflictError(message)
    if code == "23505":
        return ConflictError("a record with the same unique key already exists", constraint=message)
    if code == "23503":
        if message.lower().startswith("update or delete on table"):
            return ConflictError("the record is still referenced by other records", constraint=message)
        return NotFoundError("a referenced record does not exist in this organization", constraint=message)
    if code == "23514":
        return ConflictError("a value violates a database check", constraint=message)
    return CEError(message)
