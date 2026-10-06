"""Throwaway databases for tests: create, migrate to head, drop.

Each test session gets its own database on the Compose Postgres (or DATABASE_URL's server),
migrated with Alembic exactly as production is, so tests exercise the real schema, triggers
and constraints.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg

__all__ = ["ALEMBIC_INI", "TestDatabase", "admin_dsn", "async_url"]

ALEMBIC_INI = Path(__file__).resolve().parents[5] / "packages" / "py" / "ce_db" / "alembic.ini"


def _base_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (copy .env.example to .env)")
    return url


def admin_dsn(database: str = "postgres") -> str:
    parts = urlsplit(_base_url().replace("postgresql+asyncpg://", "postgresql://", 1))
    return urlunsplit(parts._replace(path=f"/{database}"))


def async_url(database: str) -> str:
    parts = urlsplit(_base_url())
    return urlunsplit(parts._replace(path=f"/{database}"))


class TestDatabase:
    """A uniquely named database migrated to `head` (or left empty with `migrate=False`)."""

    __test__ = False  # not a pytest test class

    def __init__(self, prefix: str = "ce_test") -> None:
        self.name = f"{prefix}_{uuid4().hex[:10]}"
        self.url = async_url(self.name)

    async def create(self, *, migrate: bool = True) -> TestDatabase:
        conn = await asyncpg.connect(admin_dsn())
        try:
            await conn.execute(f'CREATE DATABASE "{self.name}"')
        finally:
            await conn.close()
        if migrate:
            self.alembic("upgrade", "head")
        return self

    def alembic(self, *args: str) -> str:
        env = {**os.environ, "DATABASE_URL": self.url}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not ALEMBIC_INI.is_file():
            raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result.stdout + result.stderr

    async def drop(self) -> None:
        conn = await asyncpg.connect(admin_dsn())
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        finally:
            await conn.close()
