"""The local-filesystem provider passes the shared contract (always runs; no infrastructure)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from ce_plugin_storage_local_fs.provider import LocalFSStorage
from ce_storage.base import PresignedRequest
from ce_storage.contract import SendPresigned, SentResponse, StorageContract

BUCKET = "ce-contract"


class TestLocalFSContract(StorageContract):
    @pytest.fixture
    async def storage(self, tmp_path: Path) -> LocalFSStorage:
        provider = LocalFSStorage(tmp_path / "storage", base_url="http://api.test", signing_secret="test-secret")
        await provider.ensure_bucket(BUCKET)
        return provider

    @pytest.fixture
    def bucket(self) -> str:
        return BUCKET

    @pytest.fixture
    def send(self, storage: LocalFSStorage) -> SendPresigned:
        async def send(
            request: PresignedRequest, body: bytes | None, headers: Mapping[str, str] | None
        ) -> SentResponse:
            response = await storage.serve_presigned(
                request.method, request.url, dict(headers if headers is not None else request.headers), body
            )
            payload = response.file.read_bytes() if response.file is not None else response.body
            return SentResponse(response.status, {k.lower(): v for k, v in response.headers.items()}, payload)

        return send
