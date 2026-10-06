"""S3-API `StorageProvider` plugin (ADR 0010, ADR 0030): SeaweedFS locally and self-hosted, Cloudflare R2 managed.

boto3 is synchronous; every call runs in a worker thread (`asyncio.to_thread`) so the event loop
never blocks. Two clients share the credentials: one for requests from this process
(`S3_ENDPOINT_URL`) and one that signs presigned URLs for browsers and GPU workers
(`S3_PUBLIC_ENDPOINT_URL`, default the same). SigV4 signs the host, so a URL cannot be rewritten
from the internal to the public name after signing.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, TypeVar
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from ce_core.errors import CEError, InvalidInputError, Issue, NotFoundError
from ce_storage.base import (
    MAX_PART_NUMBER,
    SHA256_METADATA_KEY,
    CompletedPart,
    ListedObject,
    MultipartUpload,
    ObjectInfo,
    PresignedRequest,
    PutBody,
    StorageProvider,
    normalize_etag,
)
from ce_storage.keys import metadata_issues, validate_bucket, validate_key

__all__ = ["S3Storage", "StorageUnavailableError", "create"]

_CHUNK = 1024 * 1024
_NOT_FOUND = {"404", "NoSuchKey", "NoSuchBucket", "NoSuchUpload", "NotFound"}
_INVALID = {"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "MalformedXML", "InvalidArgument"}
T = TypeVar("T")


class StorageUnavailableError(CEError):
    code = "storage_unavailable"
    status = 503
    title = "Object storage is unavailable"


def _client_config() -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"max_attempts": 4, "mode": "standard"},
        # Only send/require checksums when an operation needs them: S3-compatible stores
        # (SeaweedFS, R2) differ in their support of the newer default integrity headers.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


def _hash_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest()


class S3Storage(StorageProvider):
    name = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        public_endpoint_url: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        session = boto3.session.Session(
            aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key, region_name=region
        )
        self.region = region
        self._client: Any = session.client("s3", endpoint_url=endpoint_url, config=_client_config())
        self._presigner: Any = (
            session.client("s3", endpoint_url=public_endpoint_url, config=_client_config())
            if public_endpoint_url and public_endpoint_url != endpoint_url
            else self._client
        )
        self._clock = clock

    # ================================================================== error mapping
    async def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND:
                raise NotFoundError("object storage: not found", code=code) from exc
            if code in _INVALID:
                raise InvalidInputError(
                    "object storage rejected the request", issues=[Issue("storage_" + code, str(exc))]
                ) from exc
            raise StorageUnavailableError(f"object storage error {code}") from exc
        except EndpointConnectionError as exc:
            raise StorageUnavailableError("object storage is unreachable") from exc
        except BotoCoreError as exc:
            raise StorageUnavailableError(f"object storage client error: {type(exc).__name__}") from exc

    def _info(self, bucket: str, key: str, head: Mapping[str, Any]) -> ObjectInfo:
        metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
        sha256 = metadata.pop(SHA256_METADATA_KEY, None)
        modified = head["LastModified"]
        return ObjectInfo(
            bucket=bucket,
            key=key,
            size=int(head["ContentLength"]),
            etag=normalize_etag(head["ETag"]),
            content_type=head.get("ContentType"),
            last_modified=modified if modified.tzinfo else modified.replace(tzinfo=UTC),
            sha256=sha256,
            metadata=metadata,
        )

    # ================================================================== buckets
    async def ensure_bucket(self, bucket: str) -> None:
        validate_bucket(bucket)
        try:
            await self._call(self._client.head_bucket, Bucket=bucket)
        except NotFoundError:
            kwargs: dict[str, Any] = {"Bucket": bucket}
            if self.region not in ("us-east-1", "auto"):
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            try:
                await self._call(self._client.create_bucket, **kwargs)
            except StorageUnavailableError:
                # Created concurrently by another process: fine if it exists now.
                await self._call(self._client.head_bucket, Bucket=bucket)

    # ================================================================== objects
    def _put_sync(
        self, bucket: str, key: str, body: PutBody, content_type: str, metadata: dict[str, str]
    ) -> dict[str, Any]:
        if isinstance(body, bytes | bytearray | memoryview):
            data = bytes(body)
            metadata[SHA256_METADATA_KEY] = hashlib.sha256(data).hexdigest()
            self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type, Metadata=metadata)
        else:
            path, temporary = (body, False) if isinstance(body, Path) else (self._spool(body), True)
            try:
                metadata[SHA256_METADATA_KEY] = _hash_file(path)
                self._client.upload_file(
                    str(path), bucket, key, ExtraArgs={"ContentType": content_type, "Metadata": metadata}
                )
            finally:
                if temporary:
                    path.unlink(missing_ok=True)
        result: dict[str, Any] = self._client.head_object(Bucket=bucket, Key=key)
        return result

    @staticmethod
    def _spool(stream: IO[bytes]) -> Path:
        fd, tmp = tempfile.mkstemp(prefix="ce-s3-")
        with os.fdopen(fd, "wb") as out:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                out.write(chunk)
        return Path(tmp)

    async def put(
        self,
        bucket: str,
        key: str,
        body: PutBody,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        validate_bucket(bucket)
        validate_key(key)
        extra = dict(metadata or {})
        if issues := metadata_issues(extra):
            raise InvalidInputError("invalid object metadata", issues=issues)
        head = await self._call(self._put_sync, bucket, key, body, content_type, extra)
        return self._info(bucket, key, head)

    async def get(self, bucket: str, key: str) -> bytes:
        validate_bucket(bucket)
        validate_key(key)

        def read() -> bytes:
            response = self._client.get_object(Bucket=bucket, Key=key)
            data: bytes = response["Body"].read()
            return data

        return await self._call(read)

    async def download(self, bucket: str, key: str, dest: Path) -> ObjectInfo:
        validate_bucket(bucket)
        validate_key(key)

        def fetch() -> dict[str, Any]:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
            os.close(fd)
            try:
                response = self._client.get_object(Bucket=bucket, Key=key)
                with open(tmp, "wb") as out:
                    for chunk in response["Body"].iter_chunks(_CHUNK):
                        out.write(chunk)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return {k: response[k] for k in ("ContentLength", "ETag", "ContentType", "LastModified", "Metadata")}

        return self._info(bucket, key, await self._call(fetch))

    async def head(self, bucket: str, key: str) -> ObjectInfo:
        validate_bucket(bucket)
        validate_key(key)
        return self._info(bucket, key, await self._call(self._client.head_object, Bucket=bucket, Key=key))

    async def delete(self, bucket: str, key: str) -> None:
        validate_bucket(bucket)
        validate_key(key)
        await self._call(self._client.delete_object, Bucket=bucket, Key=key)

    async def copy(self, bucket: str, src_key: str, dst_key: str, *, sha256: str | None = None) -> ObjectInfo:
        """CopyObject within the bucket (objects here are far below the 5 GB single-copy limit).
        `sha256` records a hash verified by the caller (presigned uploads carry no hash metadata)."""
        validate_bucket(bucket)
        validate_key(src_key)
        validate_key(dst_key)
        head = await self._call(self._client.head_object, Bucket=bucket, Key=src_key)
        metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
        if sha256:
            metadata[SHA256_METADATA_KEY] = sha256
        await self._call(
            self._client.copy_object,
            Bucket=bucket,
            Key=dst_key,
            CopySource={"Bucket": bucket, "Key": src_key},
            MetadataDirective="REPLACE",
            Metadata=metadata,
            ContentType=head.get("ContentType") or "application/octet-stream",
        )
        return await self.head(bucket, dst_key)

    async def list(self, bucket: str, prefix: str = "", *, limit: int = 1000) -> list[ListedObject]:
        validate_bucket(bucket)

        def scan() -> list[ListedObject]:
            found: list[ListedObject] = []
            paginator = self._client.get_paginator("list_objects_v2")
            pages: Iterator[dict[str, Any]] = paginator.paginate(
                Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": min(limit, 1000)}
            )
            for page in pages:
                for item in page.get("Contents", []):
                    found.append(ListedObject(item["Key"], int(item["Size"]), normalize_etag(item["ETag"])))
                    if len(found) >= limit:
                        return found
            return found

        return await self._call(scan)

    # ================================================================== presigning (local computation)
    def _presign(self, operation: str, params: dict[str, Any], ttl_s: int) -> tuple[str, datetime]:
        if ttl_s <= 0:
            raise InvalidInputError("presigned URLs need a positive TTL")
        url: str = self._presigner.generate_presigned_url(operation, Params=params, ExpiresIn=ttl_s)
        return url, self._clock() + timedelta(seconds=ttl_s)

    async def presign_get(
        self, bucket: str, key: str, *, ttl_s: int, download_filename: str | None = None
    ) -> PresignedRequest:
        validate_bucket(bucket)
        validate_key(key)
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if download_filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{quote(download_filename)}"'
        url, expires_at = self._presign("get_object", params, ttl_s)
        return PresignedRequest("GET", url, {}, expires_at)

    async def presign_put(self, bucket: str, key: str, *, ttl_s: int, content_type: str) -> PresignedRequest:
        validate_bucket(bucket)
        validate_key(key)
        params = {"Bucket": bucket, "Key": key, "ContentType": content_type}
        url, expires_at = self._presign("put_object", params, ttl_s)
        return PresignedRequest("PUT", url, {"Content-Type": content_type}, expires_at)

    # ================================================================== multipart
    async def create_multipart(self, bucket: str, key: str, *, content_type: str) -> MultipartUpload:
        validate_bucket(bucket)
        validate_key(key)
        response = await self._call(
            self._client.create_multipart_upload, Bucket=bucket, Key=key, ContentType=content_type
        )
        return MultipartUpload(bucket, key, str(response["UploadId"]))

    async def presign_part(self, upload: MultipartUpload, part_number: int, *, ttl_s: int) -> PresignedRequest:
        if not 1 <= part_number <= MAX_PART_NUMBER:
            raise InvalidInputError(f"part numbers are 1..{MAX_PART_NUMBER}")
        params = {"Bucket": upload.bucket, "Key": upload.key, "UploadId": upload.upload_id, "PartNumber": part_number}
        url, expires_at = self._presign("upload_part", params, ttl_s)
        return PresignedRequest("PUT", url, {}, expires_at)

    async def list_parts(self, upload: MultipartUpload) -> builtins.list[CompletedPart]:
        def scan() -> list[CompletedPart]:
            parts: list[CompletedPart] = []
            paginator = self._client.get_paginator("list_parts")
            for page in paginator.paginate(Bucket=upload.bucket, Key=upload.key, UploadId=upload.upload_id):
                parts += [CompletedPart(int(p["PartNumber"]), normalize_etag(p["ETag"])) for p in page.get("Parts", [])]
            return sorted(parts, key=lambda p: p.part_number)

        return await self._call(scan)

    async def complete_multipart(
        self, upload: MultipartUpload, parts: Sequence[CompletedPart] | None = None
    ) -> ObjectInfo:
        chosen = list(parts) if parts is not None else await self.list_parts(upload)
        if not chosen:
            raise InvalidInputError(
                "the multipart upload cannot be completed",
                issues=[Issue("storage_multipart", "a multipart upload needs at least one part")],
            )
        numbers = [p.part_number for p in chosen]
        if numbers != sorted(set(numbers)):
            raise InvalidInputError(
                "the multipart upload cannot be completed",
                issues=[Issue("storage_multipart", "parts must be listed once each, in ascending order")],
            )
        await self._call(
            self._client.complete_multipart_upload,
            Bucket=upload.bucket,
            Key=upload.key,
            UploadId=upload.upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p.part_number, "ETag": f'"{p.etag}"'} for p in chosen]},
        )
        return await self.head(upload.bucket, upload.key)

    async def abort_multipart(self, upload: MultipartUpload) -> None:
        try:
            await self._call(
                self._client.abort_multipart_upload, Bucket=upload.bucket, Key=upload.key, UploadId=upload.upload_id
            )
        except NotFoundError:
            return


def create(settings: Any) -> S3Storage:
    """Provider factory named by plugin.yaml (`STORAGE_PROVIDER=s3`)."""
    return S3Storage(
        endpoint_url=settings.s3_endpoint_url,
        public_endpoint_url=settings.s3_public_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id.get_secret_value(),
        secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
