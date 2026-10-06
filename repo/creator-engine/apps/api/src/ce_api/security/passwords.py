"""argon2id password hashing (RFC 9106) with parameters from `security.password_hash`."""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from ce_config.schemas import PasswordHashConfig
from ce_core.errors import Issue

__all__ = ["Passwords", "password_issues"]


class Passwords:
    def __init__(self, config: PasswordHashConfig) -> None:
        self._hasher = PasswordHasher(
            time_cost=config.time_cost,
            memory_cost=config.memory_cost_kib,
            parallelism=config.parallelism,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        """False for a wrong password, a malformed hash, or a user without a password."""
        if not password_hash:
            # Same cost as a real check, so a missing user is not faster to probe.
            self._hasher.hash(password)
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


def password_issues(password: str, min_length: int) -> list[Issue]:
    issues = []
    if len(password) < min_length:
        issues.append(Issue("password_too_short", f"passwords have at least {min_length} characters"))
    if len(password) > 1024:
        issues.append(Issue("password_too_long", "passwords have at most 1024 characters"))
    if password.strip() != password or not password.isprintable():
        issues.append(
            Issue("password_characters", "passwords cannot start or end with spaces or contain control characters")
        )
    return issues
