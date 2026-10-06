"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

__all__ = ["Database"]


class Database:
    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_size=pool_size, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
