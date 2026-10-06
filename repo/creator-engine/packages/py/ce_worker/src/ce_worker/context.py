"""`RunContext` on a worker: artifact I/O through presigned URLs only (§25).

Inputs are fetched from the presigned GET URL the lease carries for their sha256 and verified;
outputs are hashed locally and PUT to a presigned staging slot. The scheduler verifies each
output's hash and moves it to its content key before the attempt counts as complete.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from ce_contracts.common import ArtifactRef, CancellationToken

from ce_worker.protocol import OutputDescriptor, UploadSlot

__all__ = ["ArtifactIOError", "PresignedRunContext", "file_sha256"]

_MIME = {
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".ass": "text/x-ssa",
    ".txt": "text/plain",
}


class ArtifactIOError(RuntimeError):
    """An input could not be fetched or verified, or an output could not be uploaded."""


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _suffix(mime: str) -> str:
    if mime == "audio/wav":
        return ".wav"
    return mimetypes.guess_extension(mime) or ""


class PresignedRunContext:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        inputs: dict[str, str],
        slots: list[UploadSlot],
        more_slots: Callable[[int], Awaitable[list[UploadSlot]]],
        scratch_dir: Path,
        seed: int,
        logger: Any = None,
        cancel: CancellationToken | None = None,
        on_progress: Callable[[float, str], Awaitable[None]] | None = None,
    ) -> None:
        self.http = http
        self.inputs = dict(inputs)
        self.slots = list(slots)
        self.more_slots = more_slots
        self.scratch_dir = Path(scratch_dir)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.logger = logger or logging.getLogger("ce.worker.run")
        self.cancel = cancel or CancellationToken()
        self.on_progress = on_progress
        self.outputs: list[OutputDescriptor] = []
        self._uploaded: dict[str, OutputDescriptor] = {}

    async def read_artifact(self, ref: ArtifactRef) -> Path:
        self.cancel.raise_if_cancelled()
        url = self.inputs.get(ref.sha256)
        if url is None:
            raise ArtifactIOError(f"input {ref.sha256} was not granted to this task")
        target = self.scratch_dir / "inputs" / f"{ref.sha256[:24]}{_suffix(ref.mime)}"
        if target.exists() and file_sha256(target) == ref.sha256:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        async with self.http.stream("GET", url) as response:
            if response.status_code != 200:
                raise ArtifactIOError(f"input {ref.sha256}: GET returned {response.status_code}")
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(1 << 20):
                    handle.write(chunk)
        if file_sha256(partial) != ref.sha256:
            partial.unlink(missing_ok=True)
            raise ArtifactIOError(f"input {ref.sha256} failed its hash check")
        partial.replace(target)
        return target

    async def _slot(self) -> UploadSlot:
        if not self.slots:
            self.slots = await self.more_slots(4)
            if not self.slots:
                raise ArtifactIOError("the scheduler granted no upload slots")
        return self.slots.pop(0)

    async def write_artifact(
        self, path: Path, kind: str, meta: dict[str, Any] | None = None, *, role: str = "", mime: str = ""
    ) -> ArtifactRef:
        self.cancel.raise_if_cancelled()
        path = Path(path)
        mime = mime or _MIME.get(path.suffix.lower()) or "application/octet-stream"
        sha = file_sha256(path)
        size = path.stat().st_size
        if sha not in self._uploaded:
            slot = await self._slot()

            async def body() -> Any:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        yield chunk

            headers = {**slot.headers, "Content-Length": str(size)}
            response = await self.http.request(slot.method, slot.url, content=body(), headers=headers)
            if response.status_code not in (200, 201, 204):
                raise ArtifactIOError(f"upload of {path.name} returned {response.status_code}")
            descriptor = OutputDescriptor(slot_key=slot.key, sha256=sha, bytes=size, mime=mime, kind=kind, role=role)
            self._uploaded[sha] = descriptor
            self.outputs.append(descriptor)
        return ArtifactRef(sha256=sha, kind=kind, mime=mime, bytes=size, role=role, meta=dict(meta or {}))

    async def progress(self, fraction: float, message: str = "") -> None:
        if self.on_progress is not None:
            await self.on_progress(max(0.0, min(1.0, fraction)), message)

    def cleanup(self) -> None:
        shutil.rmtree(self.scratch_dir, ignore_errors=True)
