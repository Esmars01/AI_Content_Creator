"""Behaviour specific to the S3 provider that needs no server."""

from __future__ import annotations

from ce_plugin_storage_s3.provider import S3Storage


async def test_public_endpoint_signs_presigned_urls() -> None:
    s3 = S3Storage(
        endpoint_url="http://seaweedfs:8333",
        public_endpoint_url="https://files.example.test",
        region="us-east-1",
        access_key_id="k",
        secret_access_key="s",
    )
    request = await s3.presign_get("ce-assets", "a/b.txt", ttl_s=60)
    assert request.url.startswith("https://files.example.test/ce-assets/a/b.txt?")
    put = await s3.presign_put("ce-assets", "a/b.txt", ttl_s=60, content_type="image/png")
    assert "content-type" in put.url.lower()  # the content type is a signed header
