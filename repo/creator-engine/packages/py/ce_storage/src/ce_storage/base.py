"""The `StorageProvider` interface (§23: put, get, presign_get, presign_put, multipart, exists, delete, list).

Every provider passes the same contract suite (`ce_storage.contract`), so code above this layer
never knows which one it talks to. Semantics follow the S3 API (ADR 0010):

- keys are exact; `a` and `a/b` can coexist; listing is lexicographic by key;
- `put` overwrites atomically; `delete` of a missing key is a no-op;
- a missing object raises `ce_core.errors.NotFoundError`;
- presigned requests carry the exact bucket, key and method, and expire (`storage.presign_ttl_s`);
- multipart parts are numbered from 1; every part but the last is at least `MIN_PART_BYTES`.
"""

from __future__ import annotations

import abc
import builtins
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

__all__ = [
    "LOCAL_ROUTE_PREFIX",
    "MAX_PART_NUMBER",
    "MIN_PART_BYTES",
    "SHA256_METADATA_KEY",
    "CompletedPart",
    "ListedObject",
    "MultipartUpload",
    "ObjectInfo",
    "PresignedEndpoint",
    "PresignedRequest",
    "PresignedResponse",
    "PutBody",
    "StorageProvider",
    "multipart_etag",
    "normalize_etag",
]

MIN_PART_BYTES = 5 * 1024 * 1024  # S3 minimum for every part but the last
MAX_PART_NUMBER = 10_000
SHA256_METADATA_KEY = "ce-sha256"  # user metadata written by `put` (x-amz-meta-ce-sha256)

PutBody = bytes | Path | BinaryIO

# Presigned URLs of providers that the API serves itself (the local-filesystem provider, ADR 0030).
LOCAL_ROUTE_PREFIX = "/v1/storage/local"


@dataclass(frozen=True)
class ObjectInfo:
    bucket: str
    key: str
    size: int
    etag: str  # without quotes
    content_type: str | None
    last_modified: datetime
    sha256: str | None = None  # hex; known when written by `put`, otherwise None until hashed
    metadata: Mapping[str, str] = field(default_factory=dict)  # user metadata, without SHA256_METADATA_KEY


@dataclass(frozen=True)
class ListedObject:
    key: str
    size: int
    etag: str


@dataclass(frozen=True)
class PresignedRequest:
    """What a client sends: `method` to `url` with exactly these `headers`, before `expires_at`."""

    method: str
    url: str
    headers: Mapping[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class MultipartUpload:
    bucket: str
    key: str
    upload_id: str


@dataclass(frozen=True)
class CompletedPart:
    part_number: int
    etag: str  # without quotes


@dataclass
class PresignedResponse:
    """What a provider answers to a presigned request the API forwards to it."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    file: Path | None = None


@runtime_checkable
class PresignedEndpoint(Protocol):
    """A provider whose presigned URLs point at the API (`LOCAL_ROUTE_PREFIX`); the API route hands
    each request to `serve_presigned`, which verifies the signature before touching anything."""

    async def serve_presigned(
        self, method: str, url: str, headers: Mapping[str, str], body: PutBody | None = None
    ) -> PresignedResponse: ...


def normalize_etag(etag: str) -> str:
    return etag.strip().strip('"')


def multipart_etag(part_etags: Sequence[str]) -> str:
    """The S3 convention: md5 of the concatenated binary part md5s, suffixed with the part count."""
    digest = hashlib.md5(usedforsecurity=False)
    for etag in part_etags:
        digest.update(bytes.fromhex(normalize_etag(etag)))
    return f"{digest.hexdigest()}-{len(part_etags)}"


class StorageProvider(abc.ABC):
    """Object storage. All methods are async; implementations must not block the event loop."""

    name: str

    # ------------------------------------------------------------------ buckets
    @abc.abstractmethod
    async def ensure_bucket(self, bucket: str) -> None:
        """Create the bucket if it does not exist (dev, test, `ce storage init`)."""

    # ------------------------------------------------------------------ objects
    @abc.abstractmethod
    async def put(
        self,
        bucket: str,
        key: str,
        body: PutBody,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        """Write (or overwrite) an object; records its sha256 in user metadata."""

    @abc.abstractmethod
    async def get(self, bucket: str, key: str) -> bytes:
        """The whole object in memory. Use `download` for large objects."""

    @abc.abstractmethod
    async def download(self, bucket: str, key: str, dest: Path) -> ObjectInfo:
        """Stream the object to `dest` (written atomically)."""

    @abc.abstractmethod
    async def head(self, bucket: str, key: str) -> ObjectInfo: ...

    async def exists(self, bucket: str, key: str) -> bool:
        from ce_core.errors import NotFoundError

        try:
            await self.head(bucket, key)
        except NotFoundError:
            return False
        return True

    @abc.abstractmethod
    async def delete(self, bucket: str, key: str) -> None:
        """Idempotent."""

    @abc.abstractmethod
    async def copy(self, bucket: str, src_key: str, dst_key: str, *, sha256: str | None = None) -> ObjectInfo:
        """Server-side copy within a bucket (overwrites `dst_key`); keeps content type, metadata and sha256.
        `sha256` records a hash the caller verified (presigned uploads carry none). Used to move worker
        outputs from staging keys to content-addressed keys (§25)."""

    @abc.abstractmethod
    async def list(self, bucket: str, prefix: str = "", *, limit: int = 1000) -> list[ListedObject]:
        """Objects whose key starts with `prefix`, ordered by key, at most `limit`."""

    # ------------------------------------------------------------------ presigned single requests
    @abc.abstractmethod
    async def presign_get(
        self, bucket: str, key: str, *, ttl_s: int, download_filename: str | None = None
    ) -> PresignedRequest: ...

    @abc.abstractmethod
    async def presign_put(self, bucket: str, key: str, *, ttl_s: int, content_type: str) -> PresignedRequest: ...

    # ------------------------------------------------------------------ multipart
    @abc.abstractmethod
    async def create_multipart(self, bucket: str, key: str, *, content_type: str) -> MultipartUpload: ...

    @abc.abstractmethod
    async def presign_part(self, upload: MultipartUpload, part_number: int, *, ttl_s: int) -> PresignedRequest: ...

    @abc.abstractmethod
    async def list_parts(self, upload: MultipartUpload) -> builtins.list[CompletedPart]:
        """Parts received so far, by part number."""

    @abc.abstractmethod
    async def complete_multipart(
        self, upload: MultipartUpload, parts: Sequence[CompletedPart] | None = None
    ) -> ObjectInfo:
        """Assemble the parts (all received parts when `parts` is None). The object appears atomically."""

    @abc.abstractmethod
    async def abort_multipart(self, upload: MultipartUpload) -> None:
        """Idempotent."""
