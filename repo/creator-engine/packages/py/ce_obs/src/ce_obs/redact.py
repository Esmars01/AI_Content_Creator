"""Secret redaction for logs, traces and error reports (rule 16: no secrets in logs).

Two layers, applied recursively to every log event:

- **by key**: a value whose key names a secret (`password`, `secret`, `token`, `api_key`,
  `authorization`, `cookie`, `dsn`, `signature`, `credential`, `private_key`, `access_key`, …) is
  replaced whole;
- **by value**: known secret shapes inside any string are masked — bearer tokens, URL passwords,
  presigned-URL signatures and credentials, provider key formats, PEM private keys.

Redaction is conservative: it may over-redact a harmless value, never the reverse on purpose.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

__all__ = ["REDACTED", "is_secret_key", "redact", "redact_processor", "redact_text"]

REDACTED = "[REDACTED]"

_SECRET_PARTS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "apikey",
        "authorization",
        "cookie",
        "cookies",
        "dsn",
        "signature",
        "credential",
        "credentials",
        "privatekey",
        "accesskey",
        "accesskeyid",
        "secretkey",
        "sessiontoken",
    }
)
_SECRET_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key"), ("set", "cookie")})
_SPLIT = re.compile(r"[^a-z0-9]+")

_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]*):[^@\s/]+@"), r"\1:" + REDACTED + "@"),
    (
        re.compile(
            r"(?i)\b(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|X-CE-Signature|Signature)=[^&\s\"']+"
        ),
        r"\1=" + REDACTED,
    ),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bce_(?:live|test|key|wk|enr)_[A-Za-z0-9_-]{16,}"), REDACTED),  # our API/worker token formats
    (re.compile(r"\brpa_[A-Za-z0-9]{20,}"), REDACTED),  # RunPod
]


def is_secret_key(key: str) -> bool:
    parts = [p for p in _SPLIT.split(key.lower()) if p]
    if any(p in _SECRET_PARTS for p in parts) or "".join(parts) in _SECRET_PARTS:
        return True
    return any(pair in _SECRET_PAIRS for pair in itertools.pairwise(parts))


def redact_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, *, _depth: int = 0) -> Any:
    """A redacted copy of `value` (dicts, lists, tuples and strings; other values pass through)."""
    if _depth > 12:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            k: (
                REDACTED
                if isinstance(k, str) and is_secret_key(k) and v not in (None, "")
                else redact(v, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        items = [redact(v, _depth=_depth + 1) for v in value]
        return items if isinstance(value, list) else tuple(items)
    if type(value).__name__ in ("SecretStr", "SecretBytes"):
        return REDACTED
    return value


def redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor; runs after exception formatting so tracebacks are redacted too."""
    redacted = redact(dict(event_dict))
    event_dict.clear()
    event_dict.update(redacted)
    return event_dict
