"""Behaviour specific to the local-filesystem provider (signatures, expiry, layout, multipart)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ce_core.errors import InvalidInputError
from ce_plugin_storage_local_fs.provider import LocalFSStorage
from ce_storage.base import MIN_PART_BYTES, CompletedPart


def test_local_provider_needs_a_signing_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        LocalFSStorage(tmp_path, base_url="http://api", signing_secret="")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def test_local_expiry_follows_the_clock(tmp_path: Path) -> None:
    clock = Clock()
    storage = LocalFSStorage(tmp_path, base_url="http://api", signing_secret="s", clock=clock)
    await storage.ensure_bucket("ce-assets")
    await storage.put("ce-assets", "k", b"v", content_type="text/plain")
    request = await storage.presign_get("ce-assets", "k", ttl_s=900)
    assert request.expires_at == clock.now + timedelta(seconds=900)
    clock.now += timedelta(seconds=899)
    assert (await storage.serve_presigned("GET", request.url, {})).status == 200
    clock.now += timedelta(seconds=2)
    assert (await storage.serve_presigned("GET", request.url, {})).status == 403


async def test_local_signatures_depend_on_the_secret(tmp_path: Path) -> None:
    a = LocalFSStorage(tmp_path, base_url="http://api", signing_secret="one")
    b = LocalFSStorage(tmp_path, base_url="http://api", signing_secret="two")
    await a.ensure_bucket("ce-assets")
    await a.put("ce-assets", "k", b"v", content_type="text/plain")
    request = await a.presign_get("ce-assets", "k", ttl_s=60)
    assert (await b.serve_presigned("GET", request.url, {})).status == 403
    assert (await a.serve_presigned("GET", request.url.replace("/v1/storage/local", "/v1/other"), {})).status == 404


async def test_local_files_live_under_the_root_whatever_the_key(tmp_path: Path) -> None:
    root = tmp_path / "root"
    storage = LocalFSStorage(root, base_url="http://api", signing_secret="s")
    await storage.ensure_bucket("ce-assets")
    await storage.put("ce-assets", "a/b/c..d/e", b"v", content_type="text/plain")
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files and all(root in p.parents for p in files)


async def test_local_multipart_enforces_the_minimum_part_size(tmp_path: Path) -> None:
    storage = LocalFSStorage(tmp_path, base_url="http://api", signing_secret="s")
    await storage.ensure_bucket("ce-assets")
    upload = await storage.create_multipart("ce-assets", "big", content_type="video/mp4")
    etags = []
    for number, data in ((1, b"small"), (2, b"tail")):
        request = await storage.presign_part(upload, number, ttl_s=60)
        etags.append((await storage.serve_presigned("PUT", request.url, {}, data)).headers["ETag"].strip('"'))
    with pytest.raises(InvalidInputError):
        await storage.complete_multipart(upload)
    with pytest.raises(InvalidInputError):
        await storage.complete_multipart(upload, [CompletedPart(2, etags[1]), CompletedPart(1, etags[0])])
    request = await storage.presign_part(upload, 1, ttl_s=60)  # a part can be re-sent
    await storage.serve_presigned("PUT", request.url, {}, b"\x00" * MIN_PART_BYTES)
    info = await storage.complete_multipart(upload)
    assert info.size == MIN_PART_BYTES + 4
