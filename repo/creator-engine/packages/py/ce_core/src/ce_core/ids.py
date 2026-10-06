"""Identifiers: UUIDv7 primary keys for database entities (§10.5).

Python 3.12 has no `uuid.uuid7`, so this implements RFC 9562 version 7 directly: a 48-bit
Unix timestamp in milliseconds, then a 12-bit counter that keeps IDs generated within the
same millisecond strictly increasing in this process, then 62 random bits.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["new_id", "uuid7", "uuid7_unix_ms"]

_lock = threading.Lock()
_last_ms = -1
_counter = 0
_COUNTER_MAX = 0xFFF


def uuid7() -> uuid.UUID:
    """Returns a new, time-ordered UUIDv7. Monotonic within a process."""
    global _last_ms, _counter
    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            # Start each millisecond at a random point in the lower half so there is room to count.
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
        else:
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Counter exhausted (or the clock went backwards): borrow the next millisecond.
                _last_ms += 1
                _counter = 0
        ms, counter = _last_ms, _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= counter << 64
    value |= 0b10 << 62  # RFC 9562 variant
    value |= rand_b
    return uuid.UUID(int=value)


def new_id() -> uuid.UUID:
    """The ID constructor used for every database entity."""
    return uuid7()


def uuid7_unix_ms(value: uuid.UUID) -> int:
    """The millisecond timestamp embedded in a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value}")
    return value.int >> 80
