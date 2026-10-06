"""A filesystem `RunContext` for tests, the adapter contract suite and local tools.

Artifacts are stored content-addressed under `root/<sha256>` with a JSON sidecar; `read_artifact`
verifies the hash. Services use their own contexts (presigned URLs on workers, storage plus the
`artifacts` table in-process); this one has no database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from ce_contracts.common import ArtifactRef, CancellationToken

__all__ = ["LocalRunContext", "file_sha256"]

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".json": "application/json",
    ".ass": "text/x-ssa",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".txt": "text/plain",
}


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


class LocalRunContext:
    def __init__(self, root: Path, *, seed: int = 1234, logger: Any = None) -> None:
        self.root = Path(root)
        self.store = self.root / "artifacts"
        self.scratch_dir = self.root / "scratch"
        self.store.mkdir(parents=True, exist_ok=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.logger = logger or logging.getLogger("ce.local")
        self.cancel = CancellationToken()
        self.progress_log: list[tuple[float, str]] = []
        self.written: list[ArtifactRef] = []

    def _blob(self, sha: str) -> Path:
        return self.store / sha

    async def read_artifact(self, ref: ArtifactRef) -> Path:
        blob = self._blob(ref.sha256)
        if not blob.is_file():
            raise FileNotFoundError(f"artifact {ref.sha256} is not in the local store")
        meta = json.loads((self.store / f"{ref.sha256}.json").read_text(encoding="utf-8"))
        target = self.scratch_dir / "inputs" / f"{ref.sha256[:16]}{meta.get('suffix', '')}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(blob, target)
        if file_sha256(target) != ref.sha256:
            raise ValueError(f"artifact {ref.sha256} failed its hash check")
        return target

    async def write_artifact(
        self, path: Path, kind: str, meta: dict[str, Any] | None = None, *, role: str = "", mime: str = ""
    ) -> ArtifactRef:
        path = Path(path)
        sha = file_sha256(path)
        blob = self._blob(sha)
        if not blob.exists():
            shutil.copyfile(path, blob)
        size = blob.stat().st_size
        (self.store / f"{sha}.json").write_text(
            json.dumps({"kind": kind, "suffix": path.suffix, "meta": meta or {}}, sort_keys=True), encoding="utf-8"
        )
        ref = ArtifactRef(
            artifact_id=f"local:{sha[:16]}",
            sha256=sha,
            kind=kind,
            mime=mime or _MIME.get(path.suffix.lower(), "application/octet-stream"),
            bytes=size,
            role=role,
            meta=dict(meta or {}),
        )
        self.written.append(ref)
        return ref

    async def put_file(self, path: Path, kind: str, *, role: str = "") -> ArtifactRef:
        """Adds an input file (tests)."""
        return await self.write_artifact(path, kind, role=role)

    async def progress(self, fraction: float, message: str = "") -> None:
        self.progress_log.append((fraction, message))
