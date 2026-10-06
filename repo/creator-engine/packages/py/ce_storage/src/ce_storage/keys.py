"""Bucket and key validation shared by every provider, and the key layout.

Both providers accept exactly the same keys, so a key that works in dev works on R2. The rules
are stricter than S3 on purpose: no `.`/`..` or empty segments, no leading or trailing `/`, no
backslashes or control characters (this also keeps the local provider free of path traversal).

Layout:
- uploads: `orgs/<org_id>/assets/<asset_id>/original` (the filename lives in the DB, never in the key);
- content-addressed blobs: `sha256/<aa>/<bb>/<hex>` (artifacts, Phase 2; storage dedup by sha256, §29).
"""

from __future__ import annotations

import re
import unicodedata
from uuid import UUID

from ce_core.errors import InvalidInputError, Issue

__all__ = [
    "MAX_KEY_BYTES",
    "asset_key",
    "content_key",
    "metadata_issues",
    "validate_bucket",
    "validate_key",
]

MAX_KEY_BYTES = 1024
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_META_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_METADATA_BYTES = 2048  # S3's limit for all user metadata


def _invalid(code: str, message: str, value: str) -> InvalidInputError:
    return InvalidInputError(message, issues=[Issue(code, message, detail={"value": value[:200]})])


def validate_bucket(bucket: str) -> str:
    if not _BUCKET.match(bucket) or ".." in bucket or _IPV4.match(bucket) or ".-" in bucket or "-." in bucket:
        raise _invalid("storage_bucket", "bucket names are 3-63 characters of a-z, 0-9, '.' and '-'", bucket)
    return bucket


def validate_key(key: str) -> str:
    if not key:
        raise _invalid("storage_key", "object keys cannot be empty", key)
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise _invalid("storage_key", f"object keys are at most {MAX_KEY_BYTES} UTF-8 bytes", key)
    if key.startswith("/") or key.endswith("/"):
        raise _invalid("storage_key", "object keys cannot start or end with '/'", key)
    if "\\" in key or any(unicodedata.category(c) in ("Cc", "Cs") for c in key):
        raise _invalid("storage_key", "object keys cannot contain backslashes or control characters", key)
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise _invalid("storage_key", "object keys cannot contain empty, '.' or '..' segments", key)
    return key


def metadata_issues(metadata: dict[str, str]) -> list[Issue]:
    issues: list[Issue] = []
    total = 0
    for k, v in metadata.items():
        if not _META_KEY.match(k):
            issues.append(Issue("storage_metadata", "metadata keys are lowercase a-z, 0-9 and '-'", detail={"key": k}))
        if not v.isascii() or not v.isprintable():
            issues.append(Issue("storage_metadata", "metadata values are printable ASCII", detail={"key": k}))
        total += len(k) + len(v)
    if total > MAX_METADATA_BYTES:
        issues.append(Issue("storage_metadata", f"user metadata is at most {MAX_METADATA_BYTES} bytes"))
    return issues


def asset_key(org_id: UUID, asset_id: UUID) -> str:
    return f"orgs/{org_id}/assets/{asset_id}/original"


def content_key(sha256_hex: str) -> str:
    if not _HEX64.match(sha256_hex):
        raise _invalid("storage_key", "content keys need a lowercase hex sha256", sha256_hex)
    return f"sha256/{sha256_hex[:2]}/{sha256_hex[2:4]}/{sha256_hex}"
