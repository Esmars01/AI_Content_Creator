"""Canonical JSON and content digests (§12.2).

Cache keys and content digests hash canonical JSON: keys sorted, no insignificant whitespace,
UTF-8 without escaping, and normalized floats (integral floats become integers, `-0.0`
becomes `0`, other floats use Python's shortest round-trip representation). NaN and
infinities are rejected because they have no canonical JSON form.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

__all__ = ["DIGEST_PREFIX", "canonical_bytes", "canonical_json", "content_digest", "sha256_digest"]

DIGEST_PREFIX = "sha256:"


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN and infinity cannot be canonicalized")
        if value == 0:
            return 0
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, Decimal):
        return _normalize(float(value))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON needs string keys, got {type(key).__name__}")
            out[key] = _normalize(item)
        return out
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_normalize(item) for item in value]
    if isinstance(value, set | frozenset):
        raise TypeError("sets have no canonical order; convert to a sorted list first")
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def content_digest(value: Any) -> str:
    """`sha256:<hex>` over the canonical JSON of `value`."""
    return sha256_digest(canonical_bytes(value))
