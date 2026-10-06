"""The `StorageProvider` contract suite. Every provider's tests subclass `StorageContract`.

A subclass provides three fixtures:

- `storage`: the provider under test;
- `bucket`: an existing bucket the test may write to;
- `send`: an async callable that performs a `PresignedRequest` the way a browser or GPU worker
  would (real HTTP for S3; `serve_presigned` for the local provider) and returns `SentResponse`.

Each test writes under its own random prefix, so suites can share a bucket.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from ce_core.errors import InvalidInputError, NotFoundError

from ce_storage.base import (
    MIN_PART_BYTES,
    CompletedPart,
    MultipartUpload,
    PresignedRequest,
    StorageProvider,
    multipart_etag,
)

__all__ = ["SendPresigned", "SentResponse", "StorageContract"]


@dataclass(frozen=True)
class SentResponse:
    status: int
    headers: Mapping[str, str]  # lowercase names
    body: bytes


SendPresigned = Callable[[PresignedRequest, bytes | None, Mapping[str, str] | None], Awaitable[SentResponse]]


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class StorageContract:
    """Behaviour every provider must share (S3 semantics, ADR 0010)."""

    @pytest.fixture
    def prefix(self) -> str:
        return f"contract/{uuid4().hex}"

    # ------------------------------------------------------------------ objects
    async def test_put_get_head_round_trip(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        data = b"hello creator engine\n" * 100
        info = await storage.put(bucket, f"{prefix}/a.txt", data, content_type="text/plain")
        assert info.size == len(data)
        assert info.etag == _md5(data)
        assert info.sha256 == hashlib.sha256(data).hexdigest()
        assert await storage.get(bucket, f"{prefix}/a.txt") == data
        head = await storage.head(bucket, f"{prefix}/a.txt")
        assert (head.size, head.etag, head.content_type, head.sha256) == (
            len(data),
            info.etag,
            "text/plain",
            info.sha256,
        )
        assert head.last_modified.tzinfo is not None
        assert await storage.exists(bucket, f"{prefix}/a.txt")

    async def test_put_from_path_and_stream(
        self, storage: StorageProvider, bucket: str, prefix: str, tmp_path: Path
    ) -> None:
        data = bytes(range(256)) * 4096  # 1 MiB
        source = tmp_path / "source.bin"
        source.write_bytes(data)
        from_path = await storage.put(bucket, f"{prefix}/p.bin", source, content_type="application/octet-stream")
        from_stream = await storage.put(
            bucket, f"{prefix}/s.bin", io.BytesIO(data), content_type="application/octet-stream"
        )
        digest = hashlib.sha256(data).hexdigest()
        assert from_path.sha256 == from_stream.sha256 == digest
        assert from_path.size == from_stream.size == len(data)
        assert await storage.get(bucket, f"{prefix}/s.bin") == data

    async def test_overwrite_replaces_the_object(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        key = f"{prefix}/o.txt"
        await storage.put(bucket, key, b"first version", content_type="text/plain")
        await storage.put(bucket, key, b"second", content_type="text/markdown")
        assert await storage.get(bucket, key) == b"second"
        head = await storage.head(bucket, key)
        assert (head.size, head.content_type) == (6, "text/markdown")
        assert [o.key for o in await storage.list(bucket, prefix)] == [key]

    async def test_concurrent_overwrites_leave_one_whole_object(
        self, storage: StorageProvider, bucket: str, prefix: str
    ) -> None:
        key = f"{prefix}/race.bin"
        bodies = [bytes([i]) * 200_000 for i in range(8)]
        await asyncio.gather(*(storage.put(bucket, key, b, content_type="application/octet-stream") for b in bodies))
        assert await storage.get(bucket, key) in bodies

    async def test_user_metadata(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        info = await storage.put(
            bucket, f"{prefix}/m", b"x", content_type="text/plain", metadata={"asset-id": "as_123", "kind": "logo"}
        )
        assert dict(info.metadata) == {"asset-id": "as_123", "kind": "logo"}
        assert dict((await storage.head(bucket, f"{prefix}/m")).metadata) == {"asset-id": "as_123", "kind": "logo"}
        with pytest.raises(InvalidInputError):
            await storage.put(bucket, f"{prefix}/m2", b"x", content_type="text/plain", metadata={"Bad Key": "v"})
        with pytest.raises(InvalidInputError):
            await storage.put(bucket, f"{prefix}/m3", b"x", content_type="text/plain", metadata={"k": "ğ"})

    async def test_missing_objects(self, storage: StorageProvider, bucket: str, prefix: str, tmp_path: Path) -> None:
        key = f"{prefix}/missing"
        with pytest.raises(NotFoundError):
            await storage.get(bucket, key)
        with pytest.raises(NotFoundError):
            await storage.head(bucket, key)
        with pytest.raises(NotFoundError):
            await storage.download(bucket, key, tmp_path / "out")
        assert not (tmp_path / "out").exists()
        assert not await storage.exists(bucket, key)

    async def test_delete_is_idempotent(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        key = f"{prefix}/d"
        await storage.put(bucket, key, b"x", content_type="text/plain")
        await storage.delete(bucket, key)
        await storage.delete(bucket, key)
        assert not await storage.exists(bucket, key)
        assert await storage.list(bucket, prefix) == []

    async def test_download_to_a_file(self, storage: StorageProvider, bucket: str, prefix: str, tmp_path: Path) -> None:
        data = b"\x00\x01" * 300_000
        await storage.put(bucket, f"{prefix}/big.bin", data, content_type="application/octet-stream")
        dest = tmp_path / "nested" / "copy.bin"
        info = await storage.download(bucket, f"{prefix}/big.bin", dest)
        assert dest.read_bytes() == data
        assert info.size == len(data)
        assert [p.name for p in dest.parent.iterdir()] == ["copy.bin"]  # no temporary files left

    async def test_copy_keeps_content_type_and_records_a_verified_hash(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        data = b"staged output"
        request = await storage.presign_put(bucket, f"{prefix}/staging/out", ttl_s=60, content_type="video/mp4")
        sent = await send(request, data, request.headers)
        assert sent.status == 200
        digest = hashlib.sha256(data).hexdigest()
        assert (await storage.head(bucket, f"{prefix}/staging/out")).sha256 is None  # presigned PUTs carry no hash
        info = await storage.copy(bucket, f"{prefix}/staging/out", f"{prefix}/sha256/{digest}", sha256=digest)
        assert info.sha256 == digest and info.content_type == "video/mp4" and info.size == len(data)
        assert await storage.get(bucket, f"{prefix}/sha256/{digest}") == data
        assert await storage.get(bucket, f"{prefix}/staging/out") == data  # the source stays
        await storage.put(bucket, f"{prefix}/m", b"meta", content_type="text/plain", metadata={"role": "x"})
        copied = await storage.copy(bucket, f"{prefix}/m", f"{prefix}/m2")
        assert copied.metadata == {"role": "x"} and copied.sha256 == hashlib.sha256(b"meta").hexdigest()
        with pytest.raises(NotFoundError):
            await storage.copy(bucket, f"{prefix}/missing", f"{prefix}/x")

    # ------------------------------------------------------------------ keys and listing
    async def test_a_key_and_its_children_coexist(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        await storage.put(bucket, f"{prefix}/a", b"parent", content_type="text/plain")
        await storage.put(bucket, f"{prefix}/a/b", b"child", content_type="text/plain")
        assert await storage.get(bucket, f"{prefix}/a") == b"parent"
        assert await storage.get(bucket, f"{prefix}/a/b") == b"child"

    async def test_list_is_ordered_by_key_and_limited(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        keys = [f"{prefix}/k/{name}" for name in ("b", "a", "c/d", "c", "Z", "é")]
        for key in keys:
            await storage.put(bucket, key, key.encode(), content_type="text/plain")
        await storage.put(bucket, f"{prefix}/other", b"x", content_type="text/plain")
        listed = await storage.list(bucket, f"{prefix}/k/")
        assert [o.key for o in listed] == sorted(keys, key=lambda k: k.encode("utf-8"))
        assert [o.size for o in listed] == [len(o.key.encode()) for o in listed]
        assert len(await storage.list(bucket, f"{prefix}/k/", limit=2)) == 2

    async def test_unicode_keys(self, storage: StorageProvider, bucket: str, prefix: str) -> None:
        key = f"{prefix}/tr/İstanbul ğüşöç/العربية 1.txt"
        await storage.put(bucket, key, b"merhaba", content_type="text/plain")
        assert await storage.get(bucket, key) == b"merhaba"
        assert [o.key for o in await storage.list(bucket, f"{prefix}/tr/")] == [key]

    @pytest.mark.parametrize("key", ["", "/abs", "a//b", "a/", "../x", "a/./b", "a/../b", "a\\b", "a\x00b", "x" * 1025])
    async def test_invalid_keys_are_rejected(self, storage: StorageProvider, bucket: str, key: str) -> None:
        with pytest.raises(InvalidInputError):
            await storage.put(bucket, key, b"x", content_type="text/plain")
        with pytest.raises(InvalidInputError):
            await storage.presign_get(bucket, key, ttl_s=60)

    async def test_ensure_bucket_is_idempotent(self, storage: StorageProvider, bucket: str) -> None:
        await storage.ensure_bucket(bucket)
        await storage.ensure_bucket(bucket)

    # ------------------------------------------------------------------ presigned requests
    async def test_presigned_get(self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned) -> None:
        key = f"{prefix}/dl/report final.txt"
        await storage.put(bucket, key, b"downloadable", content_type="text/plain")
        request = await storage.presign_get(bucket, key, ttl_s=60, download_filename="report final.txt")
        assert request.method == "GET"
        response = await send(request, None, None)
        assert response.status == 200
        assert response.body == b"downloadable"
        assert "attachment" in response.headers.get("content-disposition", "")

    async def test_presigned_urls_are_bound_to_key_and_method(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        await storage.put(bucket, f"{prefix}/one", b"1", content_type="text/plain")
        await storage.put(bucket, f"{prefix}/two", b"2", content_type="text/plain")
        request = await storage.presign_get(bucket, f"{prefix}/one", ttl_s=60)
        parts = urlsplit(request.url)
        tampered = PresignedRequest(
            "GET", urlunsplit(parts._replace(path=parts.path.replace("/one", "/two"))), {}, request.expires_at
        )
        assert (await send(tampered, None, None)).status == 403
        as_put = PresignedRequest("PUT", request.url, {"Content-Type": "text/plain"}, request.expires_at)
        assert (await send(as_put, b"overwrite", None)).status == 403
        assert await storage.get(bucket, f"{prefix}/one") == b"1"

    async def test_presigned_urls_expire(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        await storage.put(bucket, f"{prefix}/e", b"x", content_type="text/plain")
        request = await storage.presign_get(bucket, f"{prefix}/e", ttl_s=1)
        await asyncio.sleep(2.2)
        assert (await send(request, None, None)).status == 403

    async def test_presigned_put(self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned) -> None:
        key = f"{prefix}/up/clip.mp4"
        request = await storage.presign_put(bucket, key, ttl_s=60, content_type="video/mp4")
        assert request.method == "PUT" and dict(request.headers) == {"Content-Type": "video/mp4"}
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 4096
        response = await send(request, data, None)
        assert response.status == 200
        assert response.headers["etag"].strip('"') == _md5(data)
        head = await storage.head(bucket, key)
        assert (head.size, head.content_type, head.etag) == (len(data), "video/mp4", _md5(data))
        assert head.sha256 is None  # only `put` records the sha256; uploads are hashed on completion

    async def test_presigned_put_requires_the_signed_content_type(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        key = f"{prefix}/up/x.png"
        request = await storage.presign_put(bucket, key, ttl_s=60, content_type="image/png")
        response = await send(request, b"not really a png", {"Content-Type": "text/html"})
        assert response.status == 403
        assert not await storage.exists(bucket, key)

    # ------------------------------------------------------------------ multipart
    async def _upload_parts(
        self, storage: StorageProvider, send: SendPresigned, bucket: str, key: str, parts: list[bytes]
    ) -> tuple[MultipartUpload, list[CompletedPart]]:
        upload = await storage.create_multipart(bucket, key, content_type="video/mp4")
        uploaded: list[CompletedPart] = []
        for number, data in enumerate(parts, start=1):
            request = await storage.presign_part(upload, number, ttl_s=120)
            response = await send(request, data, None)
            assert response.status == 200, response.body[:300]
            uploaded.append(CompletedPart(number, response.headers["etag"].strip('"')))
        return upload, uploaded

    async def test_multipart_upload_round_trip(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        key = f"{prefix}/mp/video.mp4"
        parts = [b"\x11" * MIN_PART_BYTES, b"\x22" * MIN_PART_BYTES, b"tail" * 100]
        upload, uploaded = await self._upload_parts(storage, send, bucket, key, parts)
        assert [p.etag for p in uploaded] == [_md5(p) for p in parts]
        assert await storage.list_parts(upload) == uploaded
        info = await storage.complete_multipart(upload)
        whole = b"".join(parts)
        assert info.size == len(whole)
        assert info.etag == multipart_etag([p.etag for p in uploaded])
        assert info.content_type == "video/mp4"
        assert await storage.get(bucket, key) == whole

    async def test_multipart_completion_checks_etags(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        key = f"{prefix}/mp/bad.mp4"
        upload, uploaded = await self._upload_parts(storage, send, bucket, key, [b"only part"])
        with pytest.raises(InvalidInputError):
            await storage.complete_multipart(upload, [CompletedPart(1, "0" * 32)])
        assert not await storage.exists(bucket, key)
        info = await storage.complete_multipart(upload, uploaded)
        assert info.size == len(b"only part")

    async def test_multipart_abort(
        self, storage: StorageProvider, bucket: str, prefix: str, send: SendPresigned
    ) -> None:
        key = f"{prefix}/mp/aborted.mp4"
        upload, _ = await self._upload_parts(storage, send, bucket, key, [b"abc"])
        await storage.abort_multipart(upload)
        await storage.abort_multipart(upload)
        with pytest.raises(NotFoundError):
            await storage.list_parts(upload)
        assert not await storage.exists(bucket, key)
