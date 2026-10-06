"""Local-filesystem `StorageProvider` plugin: the native fallback of Phase 0 (dev and test only, ADR 0030).

It keeps S3 semantics exactly (the shared contract suite runs against both providers):

- objects are stored under a hash of their key, so `a` and `a/b` can coexist like in S3;
  `<root>/<bucket>/objects/<h[:2]>/<h>.json` is the metadata, `<h>.<nonce>.bin` the bytes; an
  overwrite writes new bytes first and swaps the metadata file atomically;
- presigned URLs point at the API (`<base_url>/v1/storage/local/<bucket>/<key>?…`) and carry an
  HMAC-SHA256 signature over method, bucket, key, expiry and the signed headers; the API route
  hands the request to `serve_presigned`, which checks the signature before touching anything.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from uuid import uuid4

from ce_core.errors import InvalidInputError, Issue, NotFoundError
from ce_storage.base import (
    LOCAL_ROUTE_PREFIX,
    MAX_PART_NUMBER,
    MIN_PART_BYTES,
    SHA256_METADATA_KEY,
    CompletedPart,
    ListedObject,
    MultipartUpload,
    ObjectInfo,
    PresignedRequest,
    PresignedResponse,
    PutBody,
    StorageProvider,
    multipart_etag,
    normalize_etag,
)
from ce_storage.keys import metadata_issues, validate_bucket, validate_key

__all__ = ["LOCAL_ROUTE_PREFIX", "LocalFSStorage", "LocalResponse", "create"]

LocalResponse = PresignedResponse  # name used before the provider became a plugin
_CHUNK = 1024 * 1024
_SIGNING_CONTEXT = b"ce-storage-local-v1"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class _Written:
    path: Path
    size: int
    md5: str
    sha256: str


class LocalFSStorage(StorageProvider):
    name = "local_fs"

    def __init__(
        self,
        root: Path | str,
        *,
        base_url: str,
        signing_secret: str,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not signing_secret:
            raise ValueError("the local storage provider needs SECRET_KEY to sign URLs")
        self.root = Path(root).resolve()
        self.base_url = base_url.rstrip("/")
        self._signing_key = hmac.new(signing_secret.encode("utf-8"), _SIGNING_CONTEXT, hashlib.sha256).digest()
        self._clock = clock
        self._locks = [threading.Lock() for _ in range(64)]

    # ================================================================== paths and helpers
    def _bucket_dir(self, bucket: str, *, must_exist: bool = True) -> Path:
        path = self.root / validate_bucket(bucket)
        if must_exist and not path.is_dir():
            raise NotFoundError(f"bucket {bucket} does not exist", bucket=bucket)
        return path

    def _meta_path(self, bucket: str, key: str) -> Path:
        h = _key_hash(validate_key(key))
        return self._bucket_dir(bucket) / "objects" / h[:2] / f"{h}.json"

    def _upload_dir(self, upload: MultipartUpload) -> Path:
        if not upload.upload_id.isalnum():
            raise NotFoundError("multipart upload not found")
        path = self._bucket_dir(upload.bucket) / "uploads" / upload.upload_id
        if not (path / "upload.json").is_file():
            raise NotFoundError("multipart upload not found", upload_id=upload.upload_id)
        meta = json.loads((path / "upload.json").read_text(encoding="utf-8"))
        if meta["key"] != upload.key:
            raise NotFoundError("multipart upload not found", upload_id=upload.upload_id)
        return path

    @contextmanager
    def _lock(self, bucket: str, key: str) -> Iterator[None]:
        lock = self._locks[int(_key_hash(f"{bucket}/{key}")[:8], 16) % len(self._locks)]
        with lock:
            yield

    @staticmethod
    def _write_stream(chunks: Iterator[bytes], directory: Path, suffix: str) -> _Written:
        directory.mkdir(parents=True, exist_ok=True)
        md5 = hashlib.md5(usedforsecurity=False)
        sha = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in chunks:
                    out.write(chunk)
                    md5.update(chunk)
                    sha.update(chunk)
                    size += len(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return _Written(Path(tmp), size, md5.hexdigest(), sha.hexdigest())

    @staticmethod
    def _chunks(body: PutBody) -> Iterator[bytes]:
        if isinstance(body, bytes | bytearray | memoryview):
            yield bytes(body)
            return
        if isinstance(body, Path):
            with body.open("rb") as handle:
                yield from iter(lambda: handle.read(_CHUNK), b"")
            return
        stream: BinaryIO = body
        yield from iter(lambda: stream.read(_CHUNK), b"")

    @staticmethod
    def _atomic_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _read_meta(path: Path) -> dict[str, Any] | None:
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return data

    def _info(self, bucket: str, meta: dict[str, Any]) -> ObjectInfo:
        metadata = dict(meta["metadata"])
        sha256 = metadata.pop(SHA256_METADATA_KEY, None)
        return ObjectInfo(
            bucket=bucket,
            key=meta["key"],
            size=meta["size"],
            etag=meta["etag"],
            content_type=meta["content_type"],
            last_modified=datetime.fromisoformat(meta["last_modified"]),
            sha256=sha256,
            metadata=metadata,
        )

    def _commit(
        self,
        bucket: str,
        key: str,
        written: _Written,
        *,
        content_type: str,
        metadata: Mapping[str, str],
        etag: str,
        record_sha256: bool,
    ) -> ObjectInfo:
        """Move written bytes into place as `key` and swap the metadata file atomically.

        Like S3, only `put` records the sha256; presigned and multipart uploads do not (parity).
        """
        meta_path = self._meta_path(bucket, key)
        h = meta_path.stem
        data_name = f"{h}.{uuid4().hex}.bin"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(written.path, meta_path.parent / data_name)
        meta = {
            "key": key,
            "data": data_name,
            "size": written.size,
            "etag": etag,
            "content_type": content_type,
            "metadata": {**metadata, SHA256_METADATA_KEY: written.sha256} if record_sha256 else dict(metadata),
            "last_modified": self._clock().isoformat(),
        }
        with self._lock(bucket, key):
            previous = self._read_meta(meta_path)
            self._atomic_json(meta_path, meta)
            if previous is not None and previous["data"] != data_name:
                (meta_path.parent / previous["data"]).unlink(missing_ok=True)
        return self._info(bucket, meta)

    def _open_data(self, bucket: str, key: str) -> tuple[dict[str, Any], Path]:
        meta_path = self._meta_path(bucket, key)
        for _ in range(3):  # an overwrite may swap the data file between the two reads
            meta = self._read_meta(meta_path)
            if meta is None:
                break
            data = meta_path.parent / meta["data"]
            if data.is_file():
                return meta, data
        raise NotFoundError(f"object {key} not found", bucket=bucket, key=key)

    # ================================================================== buckets
    async def ensure_bucket(self, bucket: str) -> None:
        path = self._bucket_dir(bucket, must_exist=False)
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)

    # ================================================================== objects
    def _put_sync(
        self,
        bucket: str,
        key: str,
        body: PutBody,
        content_type: str,
        metadata: Mapping[str, str],
        record_sha256: bool = True,
    ) -> ObjectInfo:
        directory = self._meta_path(bucket, key).parent
        written = self._write_stream(self._chunks(body), directory, ".bin")
        return self._commit(
            bucket,
            key,
            written,
            content_type=content_type,
            metadata=metadata,
            etag=written.md5,
            record_sha256=record_sha256,
        )

    async def put(
        self,
        bucket: str,
        key: str,
        body: PutBody,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        extra = dict(metadata or {})
        if issues := metadata_issues(extra):
            raise InvalidInputError("invalid object metadata", issues=issues)
        return await asyncio.to_thread(self._put_sync, bucket, key, body, content_type, extra)

    async def get(self, bucket: str, key: str) -> bytes:
        def read() -> bytes:
            _, data = self._open_data(bucket, key)
            return data.read_bytes()

        return await asyncio.to_thread(read)

    async def download(self, bucket: str, key: str, dest: Path) -> ObjectInfo:
        def copy() -> ObjectInfo:
            meta, data = self._open_data(bucket, key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
            os.close(fd)
            try:
                shutil.copyfile(data, tmp)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return self._info(bucket, meta)

        return await asyncio.to_thread(copy)

    async def head(self, bucket: str, key: str) -> ObjectInfo:
        def read() -> ObjectInfo:
            meta, _ = self._open_data(bucket, key)
            return self._info(bucket, meta)

        return await asyncio.to_thread(read)

    async def delete(self, bucket: str, key: str) -> None:
        def remove() -> None:
            meta_path = self._meta_path(bucket, key)
            with self._lock(bucket, key):
                meta = self._read_meta(meta_path)
                if meta is None:
                    return
                meta_path.unlink(missing_ok=True)
                (meta_path.parent / meta["data"]).unlink(missing_ok=True)

        await asyncio.to_thread(remove)

    async def copy(self, bucket: str, src_key: str, dst_key: str, *, sha256: str | None = None) -> ObjectInfo:
        def run() -> ObjectInfo:
            meta, data = self._open_data(bucket, src_key)
            dst_meta = self._meta_path(bucket, dst_key)
            written = self._write_stream(self._chunks(data), dst_meta.parent, ".bin")
            metadata = dict(meta.get("metadata") or {})
            if sha256:
                metadata[SHA256_METADATA_KEY] = sha256
            return self._commit(
                bucket,
                dst_key,
                written,
                content_type=meta["content_type"] or "application/octet-stream",
                metadata=metadata,
                etag=meta["etag"],
                record_sha256=False,
            )

        return await asyncio.to_thread(run)

    async def list(self, bucket: str, prefix: str = "", *, limit: int = 1000) -> list[ListedObject]:
        def scan() -> list[ListedObject]:
            objects = self._bucket_dir(bucket) / "objects"
            found: list[ListedObject] = []
            for meta_path in objects.glob("*/*.json") if objects.is_dir() else []:
                meta = self._read_meta(meta_path)
                if meta is not None and meta["key"].startswith(prefix):
                    found.append(ListedObject(meta["key"], meta["size"], meta["etag"]))
            found.sort(key=lambda o: o.key.encode("utf-8"))  # S3 orders by UTF-8 bytes
            return found[:limit]

        return await asyncio.to_thread(scan)

    # ================================================================== presigning
    def _signature(self, params: Mapping[str, str], bucket: str, key: str) -> str:
        canonical = "\n".join(
            [
                "v1",
                bucket,
                key,
                *(f"{k}={params[k]}" for k in sorted(params)),
            ]
        )
        return hmac.new(self._signing_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    def _presign(
        self, method: str, bucket: str, key: str, ttl_s: int, extra: Mapping[str, str], headers: Mapping[str, str]
    ) -> PresignedRequest:
        validate_bucket(bucket)
        validate_key(key)
        if ttl_s <= 0:
            raise InvalidInputError("presigned URLs need a positive TTL")
        expires_at = self._clock() + timedelta(seconds=ttl_s)
        params = {"X-CE-Method": method, "X-CE-Expires": str(int(expires_at.timestamp())), **extra}
        params["X-CE-Signature"] = self._signature(params, bucket, key)
        url = f"{self.base_url}{LOCAL_ROUTE_PREFIX}/{bucket}/{quote(key, safe='/')}?{urlencode(params)}"
        return PresignedRequest(method, url, dict(headers), expires_at)

    async def presign_get(
        self, bucket: str, key: str, *, ttl_s: int, download_filename: str | None = None
    ) -> PresignedRequest:
        extra = {"X-CE-Filename": download_filename} if download_filename else {}
        return self._presign("GET", bucket, key, ttl_s, extra, {})

    async def presign_put(self, bucket: str, key: str, *, ttl_s: int, content_type: str) -> PresignedRequest:
        return self._presign(
            "PUT", bucket, key, ttl_s, {"X-CE-Content-Type": content_type}, {"Content-Type": content_type}
        )

    # ================================================================== multipart
    async def create_multipart(self, bucket: str, key: str, *, content_type: str) -> MultipartUpload:
        validate_key(key)
        upload_id = uuid4().hex

        def create() -> None:
            path = self._bucket_dir(bucket) / "uploads" / upload_id
            path.mkdir(parents=True)
            self._atomic_json(
                path / "upload.json",
                {"key": key, "content_type": content_type, "initiated": self._clock().isoformat()},
            )

        await asyncio.to_thread(create)
        return MultipartUpload(bucket, key, upload_id)

    async def presign_part(self, upload: MultipartUpload, part_number: int, *, ttl_s: int) -> PresignedRequest:
        if not 1 <= part_number <= MAX_PART_NUMBER:
            raise InvalidInputError(f"part numbers are 1..{MAX_PART_NUMBER}")
        extra = {"X-CE-Upload-Id": upload.upload_id, "X-CE-Part-Number": str(part_number)}
        return self._presign("PUT", upload.bucket, upload.key, ttl_s, extra, {})

    def _list_parts_sync(self, upload: MultipartUpload) -> builtins.list[tuple[CompletedPart, Path, int]]:
        path = self._upload_dir(upload)
        parts = []
        for meta_file in sorted(path.glob("part-*.json")):
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            parts.append((CompletedPart(meta["part_number"], meta["etag"]), path / meta["data"], int(meta["size"])))
        return parts

    async def list_parts(self, upload: MultipartUpload) -> builtins.list[CompletedPart]:
        return [p for p, _, _ in await asyncio.to_thread(self._list_parts_sync, upload)]

    def _complete_sync(self, upload: MultipartUpload, requested: Sequence[CompletedPart] | None) -> ObjectInfo:
        path = self._upload_dir(upload)
        received = {p.part_number: (p, data, size) for p, data, size in self._list_parts_sync(upload)}
        chosen = list(requested) if requested is not None else [received[n][0] for n in sorted(received)]
        issues: list[Issue] = []
        if not chosen:
            issues.append(Issue("storage_multipart", "a multipart upload needs at least one part"))
        numbers = [p.part_number for p in chosen]
        if numbers != sorted(set(numbers)):
            issues.append(Issue("storage_multipart", "parts must be listed once each, in ascending order"))
        for i, part in enumerate(chosen):
            got = received.get(part.part_number)
            if got is None or normalize_etag(got[0].etag) != normalize_etag(part.etag):
                issues.append(Issue("storage_multipart", "unknown part or ETag", detail={"part": part.part_number}))
            elif i < len(chosen) - 1 and got[2] < MIN_PART_BYTES:
                issues.append(Issue("storage_multipart", "part too small", detail={"part": part.part_number}))
        if issues:
            raise InvalidInputError("the multipart upload cannot be completed", issues=issues)
        meta = json.loads((path / "upload.json").read_text(encoding="utf-8"))

        def chunks() -> Iterator[bytes]:
            for part in chosen:
                yield from self._chunks(received[part.part_number][1])

        written = self._write_stream(chunks(), self._meta_path(upload.bucket, upload.key).parent, ".bin")
        info = self._commit(
            upload.bucket,
            upload.key,
            written,
            content_type=meta["content_type"],
            metadata={},
            etag=multipart_etag([p.etag for p in chosen]),
            record_sha256=False,
        )
        shutil.rmtree(path, ignore_errors=True)
        return info

    async def complete_multipart(
        self, upload: MultipartUpload, parts: Sequence[CompletedPart] | None = None
    ) -> ObjectInfo:
        return await asyncio.to_thread(self._complete_sync, upload, parts)

    async def abort_multipart(self, upload: MultipartUpload) -> None:
        def remove() -> None:
            with suppress(NotFoundError):
                shutil.rmtree(self._upload_dir(upload), ignore_errors=True)

        await asyncio.to_thread(remove)

    # ================================================================== serving presigned requests
    def _write_part_sync(self, upload: MultipartUpload, part_number: int, body: PutBody) -> str:
        path = self._upload_dir(upload)
        written = self._write_stream(self._chunks(body), path, ".part")
        data_name = f"part-{part_number:05d}.bin"
        os.replace(written.path, path / data_name)
        self._atomic_json(
            path / f"part-{part_number:05d}.json",
            {"part_number": part_number, "etag": written.md5, "size": written.size, "data": data_name},
        )
        return written.md5

    async def serve_presigned(
        self, method: str, url: str, headers: Mapping[str, str], body: PutBody | None = None
    ) -> LocalResponse:
        """Verify and execute a presigned request (called by the API route; never trusts the caller)."""
        parts = urlsplit(url)
        if not parts.path.startswith(LOCAL_ROUTE_PREFIX + "/"):
            return LocalResponse(404)
        bucket, _, quoted_key = parts.path[len(LOCAL_ROUTE_PREFIX) + 1 :].partition("/")
        key = unquote(quoted_key)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items() if len(v) == 1}
        signature = query.pop("X-CE-Signature", "")
        try:
            validate_bucket(bucket)
            validate_key(key)
        except InvalidInputError:
            return LocalResponse(400)
        expected = self._signature(query, bucket, key)
        if not hmac.compare_digest(signature, expected):
            return LocalResponse(403, body=b"signature does not match")
        if query.get("X-CE-Method") != method.upper():
            return LocalResponse(403, body=b"method does not match the signature")
        try:
            expires = int(query.get("X-CE-Expires", "0"))
        except ValueError:
            return LocalResponse(403)
        if self._clock().timestamp() > expires:
            return LocalResponse(403, body=b"request has expired")
        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            if method.upper() == "GET":
                meta, data = await asyncio.to_thread(self._open_data, bucket, key)
                out = {
                    "Content-Type": meta["content_type"] or "application/octet-stream",
                    "Content-Length": str(meta["size"]),
                    "ETag": f'"{meta["etag"]}"',
                }
                if filename := query.get("X-CE-Filename"):
                    out["Content-Disposition"] = f'attachment; filename="{quote(filename)}"'
                return LocalResponse(200, out, file=data)
            if method.upper() != "PUT" or body is None:
                return LocalResponse(405)
            if "X-CE-Part-Number" in query:
                upload = MultipartUpload(bucket, key, query.get("X-CE-Upload-Id", ""))
                etag = await asyncio.to_thread(self._write_part_sync, upload, int(query["X-CE-Part-Number"]), body)
                return LocalResponse(200, {"ETag": f'"{etag}"'})
            content_type = query.get("X-CE-Content-Type", "")
            if lowered.get("content-type", "") != content_type:
                return LocalResponse(403, body=b"content type does not match the signature")
            info = await asyncio.to_thread(self._put_sync, bucket, key, body, content_type, {}, False)
            return LocalResponse(200, {"ETag": f'"{info.etag}"'})
        except NotFoundError:
            return LocalResponse(404)


def create(settings: Any) -> LocalFSStorage:
    """Provider factory named by plugin.yaml (`STORAGE_PROVIDER=local_fs`, dev and test only)."""
    return LocalFSStorage(
        settings.local_storage_root,
        base_url=settings.api_base_url,
        signing_secret=settings.secret_key.get_secret_value(),
    )
