"""Watermark payloads (§27): a render's `payload_id` is `ce1:` + 24 hex digits (96 bits, the first
96 bits of the final video's sha256). VideoSeal 0.0 carries exactly 96 bits; AudioSeal carries
16, the payload's first 16 bits. Verification decodes the bits and compares them."""

from __future__ import annotations

import hashlib
import re

import numpy as np

__all__ = ["PAYLOAD", "bit_accuracy", "bits_of", "hex_of"]

PAYLOAD = re.compile(r"^ce1:([0-9a-f]{24})$")


def bits_of(payload_id: str, nbits: int = 96) -> np.ndarray:
    """The payload's first `nbits` bits (most significant first) as a 0/1 uint8 array. A `ce1:` id
    is its own 96 bits (decodable back to the id); any other non-empty id is hashed (sha256, first
    96 bits): it can be verified, not decoded."""
    if not payload_id:
        raise ValueError("an empty payload id")
    match = PAYLOAD.match(payload_id)
    hexdigits = match.group(1) if match else hashlib.sha256(payload_id.encode("utf-8")).hexdigest()[:24]
    value = int(hexdigits, 16)
    bits = np.array([(value >> (95 - i)) & 1 for i in range(96)], dtype=np.uint8)
    return bits[:nbits]


def hex_of(bits: np.ndarray) -> str:
    """96 decoded bits → the `ce1:` payload id they spell."""
    if len(bits) != 96:
        raise ValueError("a ce1 payload has 96 bits")
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"ce1:{value:024x}"


def bit_accuracy(decoded: np.ndarray, expected: np.ndarray) -> float:
    return float(np.mean(np.asarray(decoded, dtype=np.uint8) == np.asarray(expected, dtype=np.uint8)))
