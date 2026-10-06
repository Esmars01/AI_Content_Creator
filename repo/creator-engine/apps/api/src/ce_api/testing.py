"""Test harness for the API: services on a test database, an in-process client, tenants.

Used by `apps/api/tests` and the invariant suites. Requires the Compose core infrastructure
(Postgres, Valkey, and SeaweedFS when `storage="s3"`).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from ce_config.settings import load_effective
from ce_db.models.tenancy import Membership, Organization, User

from ce_api.app import create_app
from ce_api.deps import CSRF_HEADER
from ce_api.services import Services, build_services

__all__ = ["ApiHarness", "ApiTenant", "build_test_services", "upload_asset"]

ROOT = Path(__file__).resolve().parents[4]


def build_test_services(
    database_url: str,
    *,
    storage: Literal["local_fs", "s3"] = "local_fs",
    storage_root: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> Services:
    env = {
        "APP_ENV": "test",
        "DATABASE_URL": database_url,
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "SECRET_KEY": "test-secret-key-for-signing-only",
        "STORAGE_PROVIDER": storage,
        "LOCAL_STORAGE_ROOT": str(storage_root or ROOT / ".data" / "test-storage"),
        "API_BASE_URL": "http://api.test",
        "S3_ENDPOINT_URL": os.environ.get("S3_ENDPOINT_URL", "http://localhost:8333"),
        "S3_ACCESS_KEY_ID": os.environ.get("S3_ACCESS_KEY_ID", ""),
        "S3_SECRET_ACCESS_KEY": os.environ.get("S3_SECRET_ACCESS_KEY", ""),
        "S3_BUCKET_ASSETS": os.environ.get("CE_TEST_BUCKET", "ce-test-assets"),
        "PROVENANCE_MODE": "",
        "COOKIE_SECURE": "",
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        "TEMPORAL_TASK_QUEUE_PREFIX": f"test-{secrets.token_hex(4)}-",
        **(extra_env or {}),
    }
    return build_services(load_effective(ROOT / "config", env), pool_size=4)


@dataclass
class ApiTenant:
    org_id: UUID
    user_id: UUID
    email: str
    password: str
    role: str
    client: httpx.AsyncClient
    csrf: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ApiHarness:
    """`services.workflows` starts real Temporal workflows; the first start launches an in-process
    orchestrator worker on this harness's own task queue (compose Temporal), so job tests exercise
    the same workflows as production."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.app = create_app(services)
        self._clients: list[httpx.AsyncClient] = []
        self._worker: Any = None
        self._worker_task: Any = None
        self._exec: Any = None
        services.workflows.before_start = self._ensure_worker
        services.workflows.track = True  # tests wait for every workflow they started (drain)

    async def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        import asyncio

        from ce_exec.context import build_services as build_exec_services
        from ce_orchestrator.worker import make_worker, queues

        self._exec = build_exec_services(self.services.effective, pool_size=2)
        await self._exec.storage.ensure_bucket(self._exec.settings.s3_bucket_artifacts)  # workflows write artifacts
        client = await self.services.workflows.client()
        self._worker = make_worker(client, self._exec, "orchestrator", queues(self.services.effective))
        self._worker_task = asyncio.create_task(self._worker.run())

    def client(self, *, client_ip: str = "127.0.0.1") -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(client_ip, 4321))
        client = httpx.AsyncClient(transport=transport, base_url="http://api.test")
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
        await self.services.close()
        if self._worker is not None:
            await self._worker.shutdown()
            import contextlib

            with contextlib.suppress(BaseException):
                await self._worker_task
            await self._exec.close()

    async def _user(self, org_id: UUID, role: str, *, is_platform_admin: bool = False) -> tuple[User, str]:
        password = secrets.token_urlsafe(12)
        async with self.services.db.transaction() as session:
            user = User(
                email=f"user-{secrets.token_hex(6)}@example.test",
                name="Test user",
                password_hash=self.services.passwords.hash(password),
                is_platform_admin=is_platform_admin,
            )
            session.add(user)
            await session.flush()
            session.add(Membership(user_id=user.id, org_id=org_id, role=role))
        return user, password

    async def login(self, email: str, password: str, *, org_id: UUID | None = None) -> tuple[httpx.AsyncClient, str]:
        client = self.client()
        body: dict[str, Any] = {"email": email, "password": password}
        if org_id is not None:
            body["org_id"] = str(org_id)
        response = await client.post("/v1/auth/login", json=body)
        response.raise_for_status()
        csrf = response.json()["csrf_token"]
        client.headers[CSRF_HEADER] = csrf
        return client, csrf

    async def new_tenant(self, role: str = "owner", *, name: str = "Test org") -> ApiTenant:
        async with self.services.db.transaction() as session:
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            org_id = org.id
        return await self.add_member(org_id, role)

    async def add_member(self, org_id: UUID, role: str, *, is_platform_admin: bool = False) -> ApiTenant:
        user, password = await self._user(org_id, role, is_platform_admin=is_platform_admin)
        client, csrf = await self.login(user.email, password, org_id=org_id)
        return ApiTenant(org_id, user.id, user.email, password, role, client, csrf)


async def upload_asset(
    harness: ApiHarness,
    tenant: ApiTenant,
    data: bytes,
    *,
    mime: str,
    kind: str,
    filename: str = "upload.bin",
    declared_bytes: int | None = None,
) -> dict[str, Any]:
    """Run the full upload flow (initiate → PUT parts → complete → validation job) and return the asset."""
    import uuid

    initiated = await tenant.client.post(
        "/v1/assets:initiate-upload",
        json={"filename": filename, "mime": mime, "bytes": declared_bytes or len(data), "kind": kind},
    )
    initiated.raise_for_status()
    plan = initiated.json()
    part_size = plan["upload"]["part_size"]
    async with httpx.AsyncClient(timeout=60) as network:
        for part in plan["upload"]["parts"]:
            chunk = data[(part["part_number"] - 1) * part_size : part["part_number"] * part_size]
            sender = tenant.client if part["url"].startswith(str(tenant.client.base_url)) else network
            put = await sender.put(part["url"], content=chunk, headers=part["headers"])
            put.raise_for_status()
    completed = await tenant.client.post(
        f"/v1/assets/{plan['asset_id']}:complete", json={}, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    completed.raise_for_status()
    await harness.services.drain()
    asset = await tenant.client.get(f"/v1/assets/{plan['asset_id']}")
    asset.raise_for_status()
    result: dict[str, Any] = asset.json()
    result["job_id"] = completed.json()["job_id"]
    return result
