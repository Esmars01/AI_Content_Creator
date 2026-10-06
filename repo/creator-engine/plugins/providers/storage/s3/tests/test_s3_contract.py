"""The S3 provider passes the shared contract against SeaweedFS (Compose `core`; ADR 0010)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest
from ce_plugin_storage_s3.provider import S3Storage
from ce_storage.base import PresignedRequest
from ce_storage.contract import SendPresigned, SentResponse, StorageContract

pytestmark = pytest.mark.infra

BUCKET = "ce-contract"


def s3_from_env() -> S3Storage:
    return S3Storage(
        endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:8333"),
        region=os.environ.get("S3_REGION", "us-east-1"),
        access_key_id=os.environ.get("S3_ACCESS_KEY_ID", ""),
        secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", ""),
    )


class TestS3Contract(StorageContract):
    @pytest.fixture
    async def storage(self) -> S3Storage:
        provider = s3_from_env()
        await provider.ensure_bucket(BUCKET)
        return provider

    @pytest.fixture
    def bucket(self) -> str:
        return BUCKET

    @pytest.fixture
    async def send(self) -> AsyncIterator[SendPresigned]:
        async with httpx.AsyncClient(timeout=30) as client:

            async def send(
                request: PresignedRequest, body: bytes | None, headers: Mapping[str, str] | None
            ) -> SentResponse:
                response = await client.request(
                    request.method,
                    request.url,
                    content=body,
                    headers=dict(headers if headers is not None else request.headers),
                )
                return SentResponse(
                    response.status_code, {k.lower(): v for k, v in response.headers.items()}, response.content
                )

            yield send
