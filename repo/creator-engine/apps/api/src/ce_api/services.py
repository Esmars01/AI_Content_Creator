"""Process-wide services of the API, built once at startup and shared by every request."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ce_config.settings import EffectiveConfig
from ce_db.session import Database
from ce_storage import StorageProvider, create_storage
from redis.asyncio import Redis

from ce_api.events import EventBus
from ce_api.jobs import WorkflowStarter
from ce_api.scheduler import SchedulerClient
from ce_api.security.passwords import Passwords

__all__ = ["Services", "build_services", "utcnow"]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Services:
    effective: EffectiveConfig
    db: Database
    storage: StorageProvider
    redis: Redis
    events: EventBus
    passwords: Passwords
    workflows: WorkflowStarter
    scheduler: SchedulerClient | None = None  # the scheduler's fleet endpoints (Phase 9)
    clock: Callable[[], datetime] = utcnow
    background: set[Any] = field(default_factory=set)  # strong refs to background tasks

    @property
    def settings(self) -> Any:
        return self.effective.settings

    @property
    def config(self) -> Any:
        return self.effective.bundle.app

    @property
    def vocab(self) -> Any:
        return self.effective.bundle.vocab

    @property
    def signing_secret(self) -> str:
        secret: str = self.effective.settings.secret_key.get_secret_value()
        return secret

    async def drain(self) -> None:
        """Wait for background tasks and the workflows this process started (tests; shutdown)."""
        import asyncio

        while self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)
        await self.workflows.drain()

    async def close(self) -> None:
        await self.drain()
        await self.redis.aclose()
        await self.db.dispose()


def build_services(effective: EffectiveConfig, *, pool_size: int = 10) -> Services:
    settings = effective.settings
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app = effective.bundle.app
    return Services(
        effective=effective,
        db=Database(settings.database_url, pool_size=pool_size),
        storage=create_storage(settings),
        redis=redis,
        events=EventBus(
            redis, maxlen=app.events.stream_maxlen, retention_s=settings.sse_stream_retention_s, clock=utcnow
        ),
        passwords=Passwords(app.security.password_hash),
        workflows=WorkflowStarter(effective),
        scheduler=SchedulerClient(settings.scheduler_internal_url, settings.secret_key.get_secret_value()),
    )
