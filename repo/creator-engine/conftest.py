"""Shared pytest configuration (repository root, so it applies to every test path).

Loads `.env` (or `.env.example`) defaults so tests see the same settings as `make dev`, and
skips `@pytest.mark.infra` tests with an explicit reason when the Compose core
infrastructure is not running. Set CE_REQUIRE_INFRA=1 to turn those skips into failures
(used by `make test-infra` and CI).
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

pytest_plugins = ["ce_testing.pytest_db"]

ROOT = Path(__file__).resolve().parent


def _load_env_defaults() -> None:
    env_file = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_defaults()

INFRA_PORTS = {
    "postgres": int(os.environ.get("POSTGRES_PORT", "5432")),
    "redis": int(os.environ.get("REDIS_PORT", "6379")),
    "temporal": int(os.environ.get("TEMPORAL_PORT", "7233")),
    "seaweedfs-s3": int(os.environ.get("S3_PORT", "8333")),
}


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    infra_items = [item for item in items if item.get_closest_marker("infra")]
    if not infra_items:
        return
    down = [name for name, port in INFRA_PORTS.items() if not _port_open(port)]
    if not down:
        return
    reason = f"core infrastructure not reachable ({', '.join(down)} down); run `make infra-up`"
    if os.environ.get("CE_REQUIRE_INFRA") == "1":
        raise pytest.UsageError(reason)
    for item in infra_items:
        item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def spec_text() -> str:
    return (ROOT / "docs" / "MASTER_BUILD_PROMPT.md").read_text(encoding="utf-8")
