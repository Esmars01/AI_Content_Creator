"""Fixtures for the in-process end-to-end stack (Compose Postgres, Valkey, Temporal, SeaweedFS)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from ce_core.vocab import Vocabulary
from ce_db.session import Database
from ce_testing.database import TestDatabase
from ce_testing.seed import placeholder_objects, seed_dev
from ce_testing.stack import Stack, stack_env


@pytest.fixture(scope="module")
def e2e_db(repo_vocab: Vocabulary) -> Iterator[TestDatabase]:
    import asyncio

    database = TestDatabase(prefix="ce_e2e")

    async def create() -> None:
        await database.create()
        db = Database(database.url, pool_size=1)
        async with db.transaction() as session:
            await seed_dev(session, repo_vocab)
        await db.dispose()

    asyncio.run(create())
    yield database
    asyncio.run(database.drop())


@pytest_asyncio.fixture
async def stack(e2e_db: TestDatabase) -> AsyncIterator[Stack]:
    async with Stack.run(stack_env(e2e_db.url)) as s:
        bucket = s.effective.settings.s3_bucket_assets
        for item in placeholder_objects():
            await s.exec.storage.put(bucket, item.key, item.data, content_type=item.mime)
        yield s
