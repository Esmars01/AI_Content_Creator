"""Opaque bearer secrets: session tokens, API keys and invitation tokens.

Secrets are 256-bit random values; the database stores only their SHA-256 (a fast hash is
right for high-entropy tokens; passwords use argon2id). API keys look like
`ce_key_<12 chars>_<43 chars>`: the first part is the stored, unique, displayable `prefix`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

__all__ = ["ApiKeySecret", "csrf_token", "hash_token", "new_api_key", "new_token", "parse_api_key"]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApiKeySecret:
    prefix: str
    secret: str  # the full key, shown once

    @property
    def hash(self) -> str:
        return hash_token(self.secret)


_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_api_key(namespace: str = "ce_") -> ApiKeySecret:
    ident = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    prefix = f"{namespace}key_{ident}"
    return ApiKeySecret(prefix, f"{prefix}_{secrets.token_urlsafe(32)}")


def parse_api_key(value: str, namespace: str = "ce_") -> str | None:
    """The prefix of a well-formed key, else None."""
    head = f"{namespace}key_"
    if not value.startswith(head):
        return None
    ident, sep, rest = value[len(head) :].partition("_")
    if not sep or len(ident) != 12 or not ident.isalnum() or len(rest) < 32:
        return None
    return f"{head}{ident}"


def csrf_token(signing_secret: str, session_id: str) -> str:
    """Bound to the session: a cookie-authenticated mutation must echo it in `X-CSRF-Token`."""
    return hmac.new(signing_secret.encode("utf-8"), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()
