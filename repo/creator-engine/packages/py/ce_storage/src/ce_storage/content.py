"""Content-addressed artifact blobs (§12.2, §29): every artifact lives at `sha256/aa/bb/<hex>` in
the artifacts bucket, so a reference's sha256 locates it and re-uploading a hash is a no-op.

`StorageRunContext` is the `RunContext` of in-process execution (orchestrator CPU nodes and the
render worker): reads download by hash and verify it, writes upload by hash. Database rows for
written artifacts are registered by the caller (the orchestrator), never here.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ce_contracts.common import ArtifactRef, CancellationToken

from ce_storage.base import StorageProvider
from ce_storage.keys import content_key

__all__ = ["ContentStore", "IntegrityError", "StorageRunContext", "file_sha256", "guess_mime"]

_MIME = {
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".ass": "text/x-ssa",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".txt": "text/plain",
}


class IntegrityError(ValueError):
    """A blob's content does not match its sha256."""


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def guess_mime(path: Path) -> str:
    suffix = Path(path).suffix.lower()
    return _MIME.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class ContentStore:
    def __init__(self, storage: StorageProvider, bucket: str) -> None:
        self.storage = storage
        self.bucket = bucket

    def key(self, sha256: str) -> str:
        return content_key(sha256)

    async def has(self, sha256: str) -> bool:
        return await self.storage.exists(self.bucket, content_key(sha256))

    async def put_file(self, path: Path, *, mime: str | None = None) -> tuple[str, int]:
        """Uploads `path` under its hash unless that hash is stored already. Returns (sha256, size)."""
        sha = file_sha256(path)
        size = Path(path).stat().st_size
        if not await self.has(sha):
            await self.storage.put(self.bucket, content_key(sha), Path(path), content_type=mime or guess_mime(path))
        return sha, size

    async def put_bytes(self, data: bytes, *, mime: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        if not await self.has(sha):
            await self.storage.put(self.bucket, content_key(sha), data, content_type=mime)
        return sha

    async def fetch(self, sha256: str, dest: Path) -> Path:
        """Downloads a blob to `dest` and verifies its hash."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (dest.exists() and file_sha256(dest) == sha256):
            await self.storage.download(self.bucket, content_key(sha256), dest)
            if file_sha256(dest) != sha256:
                dest.unlink(missing_ok=True)
                raise IntegrityError(f"blob {sha256} failed its hash check")
        return dest

    async def read_bytes(self, sha256: str) -> bytes:
        data = await self.storage.get(self.bucket, content_key(sha256))
        if hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityError(f"blob {sha256} failed its hash check")
        return data

    async def adopt(self, bucket: str, key: str, sha256: str, *, mime: str) -> None:
        """Brings an object from another location (an uploaded asset) under its content key."""
        if await self.has(sha256):
            return
        with tempfile.TemporaryDirectory(prefix="ce-adopt-") as tmp:
            target = Path(tmp) / "blob"
            await self.storage.download(bucket, key, target)
            if file_sha256(target) != sha256:
                raise IntegrityError(f"{bucket}/{key} does not match sha256 {sha256}")
            await self.storage.put(self.bucket, content_key(sha256), target, content_type=mime)


class StorageRunContext:
    """`RunContext` over a `ContentStore` (see the module docstring)."""

    def __init__(
        self,
        store: ContentStore,
        scratch_dir: Path,
        *,
        seed: int = 0,
        logger: Any = None,
        cancel: CancellationToken | None = None,
    ) -> None:
        self.store = store
        self.scratch_dir = Path(scratch_dir)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.logger = logger or logging.getLogger("ce.run")
        self.cancel = cancel or CancellationToken()
        self.written: list[ArtifactRef] = []
        self.progress_log: list[tuple[float, str]] = []

    async def read_artifact(self, ref: ArtifactRef) -> Path:
        suffix = mimetypes.guess_extension(ref.mime) or ""
        suffix = {".mpga": ".wav", ".mp2": ".wav"}.get(suffix, suffix)
        if ref.mime == "audio/wav":
            suffix = ".wav"
        target = self.scratch_dir / "inputs" / f"{ref.sha256[:24]}{suffix}"
        return await self.store.fetch(ref.sha256, target)

    async def write_artifact(
        self, path: Path, kind: str, meta: dict[str, Any] | None = None, *, role: str = "", mime: str = ""
    ) -> ArtifactRef:
        mime = mime or guess_mime(Path(path))
        sha, size = await self.store.put_file(Path(path), mime=mime)
        ref = ArtifactRef(sha256=sha, kind=kind, mime=mime, bytes=size, role=role, meta=dict(meta or {}))
        self.written.append(ref)
        return ref

    async def progress(self, fraction: float, message: str = "") -> None:
        self.progress_log.append((fraction, message))

    def cleanup(self) -> None:
        shutil.rmtree(self.scratch_dir, ignore_errors=True)
