"""pytest plugin (registered in the root conftest): database fixtures for infra tests.

- `migrated_db`: one throwaway database per test session, migrated to head with Alembic;
- `db`: a `ce_db.session.Database` on it, per test (bound to the test's event loop);
- `seeded_db`: the same database with the dev seed applied once.

Tests using them must be marked `infra`; they are skipped with a reason when the Compose
stack is down (root conftest).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from ce_core.vocab import Vocabulary, load_vocabulary
from ce_db.session import Database

from ce_testing.database import TestDatabase

ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture(scope="session")
def repo_vocab() -> Vocabulary:
    return load_vocabulary(ROOT / "config" / "vocab")


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[TestDatabase]:
    database = TestDatabase()
    asyncio.run(database.create())
    yield database
    asyncio.run(database.drop())


@pytest_asyncio.fixture
async def db(migrated_db: TestDatabase) -> AsyncIterator[Database]:
    database = Database(migrated_db.url, pool_size=2)
    yield database
    await database.dispose()


@pytest.fixture(scope="session")
def seeded_db(migrated_db: TestDatabase, repo_vocab: Vocabulary) -> TestDatabase:
    from ce_testing.seed import seed_dev

    async def run() -> None:
        database = Database(migrated_db.url, pool_size=1)
        async with database.transaction() as session:
            await seed_dev(session, repo_vocab)
        await database.dispose()

    asyncio.run(run())
    return migrated_db
