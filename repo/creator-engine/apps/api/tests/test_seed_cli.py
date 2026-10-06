"""`ce seed dev`: idempotent seed, admin password handling, placeholder media in storage."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from ce_api.cli import app
from ce_api.security.passwords import Passwords
from ce_api.seed_cli import PASSWORD_ENV, run_seed
from ce_config.settings import EffectiveConfig, load_effective
from ce_db.session import Database
from ce_storage import create_storage
from ce_testing.database import TestDatabase
from ce_testing.seed import DEV_ADMIN_EMAIL, placeholder_objects
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def test_seed_refuses_production() -> None:
    result = CliRunner().invoke(app, ["seed", "dev", "--config-root", str(ROOT / "config")], env={"APP_ENV": "prod"})
    assert result.exit_code == 2
    assert "refusing" in result.output


def _effective(db_url: str, storage_root: Path) -> EffectiveConfig:
    return load_effective(
        ROOT / "config",
        {
            "APP_ENV": "test",
            "DATABASE_URL": db_url,
            "STORAGE_PROVIDER": "local_fs",
            "LOCAL_STORAGE_ROOT": str(storage_root),
            "SECRET_KEY": "test-secret",
        },
    )


async def _admin_hash(url: str) -> str | None:
    database = Database(url, pool_size=1)
    try:
        async with database.session() as session:
            result = await session.execute(
                sa.text("SELECT password_hash FROM users WHERE email = :e"), {"e": DEV_ADMIN_EMAIL}
            )
            value: str | None = result.scalar_one()
            return value
    finally:
        await database.dispose()


@pytest.mark.infra
async def test_seed_sets_passwords_and_uploads_placeholders(
    migrated_db: TestDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effective = _effective(migrated_db.url, tmp_path / "storage")
    passwords = Passwords(effective.bundle.app.security.password_hash)
    # A database seeded by other tests may already have an admin password: reset to a known one.
    monkeypatch.setenv(PASSWORD_ENV, "chosen dev password")
    ids, generated = await run_seed(effective, reset_password=True, skip_storage=False)
    assert generated is None and ids["org_id"]
    first = await _admin_hash(migrated_db.url)
    assert passwords.verify(first, "chosen dev password")

    monkeypatch.delenv(PASSWORD_ENV)
    _, generated = await run_seed(effective, reset_password=False, skip_storage=True)
    assert generated is None and await _admin_hash(migrated_db.url) == first  # kept

    _, generated = await run_seed(effective, reset_password=True, skip_storage=True)
    assert generated is not None and len(generated) >= 20
    assert passwords.verify(await _admin_hash(migrated_db.url), generated)

    storage = create_storage(effective.settings)
    database = Database(migrated_db.url, pool_size=1)
    try:
        async with database.session() as session:
            rows = {
                r.storage_key: r
                for r in (await session.execute(sa.text("SELECT storage_key, sha256, bytes, mime FROM assets"))).all()
            }
    finally:
        await database.dispose()
    for item in placeholder_objects():
        stored = await storage.get(effective.settings.s3_bucket_assets, item.key)
        row = rows[item.key]
        assert hashlib.sha256(stored).hexdigest() == row.sha256
        assert (len(stored), item.mime) == (row.bytes, row.mime)
