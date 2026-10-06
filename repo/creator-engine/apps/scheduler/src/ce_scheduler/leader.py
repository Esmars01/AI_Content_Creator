"""Single active leader via a Postgres advisory lock (§9). Followers serve the worker API; only
the leader runs the reaper, the cancellation probes and the fleet manager."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import sqlalchemy as sa
from ce_db.session import Database
from ce_obs import get_logger

__all__ = ["LEADER_LOCK_KEY", "LeaderLoops"]

LEADER_LOCK_KEY = 0x6365_5363_6865_64  # "ceSched"
_log = get_logger("ce.scheduler.leader")


class LeaderLoops:
    def __init__(self, db: Database, loops: list[tuple[str, float, Callable[[], Awaitable[Any]]]]) -> None:
        self.db = db
        self.loops = loops
        self.is_leader = False
        self._tasks: list[asyncio.Task[None]] = []
        self._conn: Any = None
        self._stop = asyncio.Event()

    async def _acquire(self) -> bool:
        if self._conn is None:
            self._conn = await self.db.engine.connect()
        result = await self._conn.execute(sa.select(sa.func.pg_try_advisory_lock(LEADER_LOCK_KEY)))
        await self._conn.commit()
        return bool(result.scalar_one())

    async def _elect(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.is_leader and await self._acquire():
                    self.is_leader = True
                    _log.info("scheduler leadership acquired")
                    for name, interval, fn in self.loops:
                        self._tasks.append(asyncio.create_task(self._loop(name, interval, fn)))
            except Exception as exc:
                _log.warning("leader election failed", error=str(exc)[:200])
                await self._release_connection()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 5.0)

    async def _loop(self, name: str, interval: float, fn: Callable[[], Awaitable[Any]]) -> None:
        while not self._stop.is_set():
            try:
                await fn()
            except Exception as exc:
                _log.warning("leader loop failed", loop=name, error=str(exc)[:300])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    async def _release_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
            self.is_leader = False

    def start(self) -> asyncio.Task[None]:
        task = asyncio.create_task(self._elect())
        self._tasks.append(task)
        return task

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        await self._release_connection()
