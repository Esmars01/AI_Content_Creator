"""Fixtures for API tests: a harness on the session's migrated test database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from ce_api.testing import ApiHarness, ApiTenant, build_test_services
from ce_testing.database import TestDatabase


@pytest_asyncio.fixture
async def harness(migrated_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(migrated_db.url, storage="local_fs", storage_root=tmp_path / "storage")
    await services.storage.ensure_bucket(services.settings.s3_bucket_assets)
    h = ApiHarness(services)
    yield h
    await h.aclose()


@pytest_asyncio.fixture
async def owner(harness: ApiHarness) -> ApiTenant:
    return await harness.new_tenant("owner")
