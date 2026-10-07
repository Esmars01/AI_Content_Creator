"""`ce_obs.events` without a server: the blocking read stays below the client's socket timeout."""

from __future__ import annotations

from datetime import UTC, datetime

from ce_obs.events import EventBus, valid_stream_id
from redis.asyncio import Redis


def bus(**kwargs: object) -> EventBus:
    redis = Redis.from_url("redis://localhost:6379/15", **kwargs)  # type: ignore[arg-type]
    return EventBus(redis, maxlen=10, retention_s=60, clock=lambda: datetime.now(UTC))


def test_blocking_read_stays_below_the_socket_timeout() -> None:
    """Regression (audit S1): XREAD BLOCK 15 s on redis-py's default 5 s socket timeout raised
    TimeoutError instead of returning empty, which killed every quiet SSE stream after 5 s."""
    assert bus().max_block_ms(15_000) == 4_000  # default socket timeout 5 s
    assert bus(socket_timeout=30).max_block_ms(15_000) == 15_000
    assert bus(socket_timeout=None).max_block_ms(15_000) == 15_000
    assert bus(socket_timeout=0.5).max_block_ms(15_000) == 100


def test_stream_id_validation() -> None:
    assert valid_stream_id("1759500000000-0") and valid_stream_id("12")
    assert not valid_stream_id("not-an-id") and not valid_stream_id("") and not valid_stream_id("1-x")
