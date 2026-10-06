"""Model cache (§25, `ce_worker.model_cache`): versioned local copies of model weights.

- `hf://repo@revision/path` and `s3://bucket/key` URIs resolve to `<root>/<scheme>/<…>@<revision>`;
  each is downloaded once per host by a registered fetcher;
- the SHA-256 of every file is verified on first use and recorded in `<root>/manifest.json`;
- `cached_models()` is reported to the scheduler at lease time;
- least-recently-used entries are evicted to stay under `max_gb`, never pinned ones;
- `root` may be a shared network volume (Phase 9): several workers — on one host or several —
  share it. A download takes an exclusive lock on its target (`<root>/.locks/`, `flock`), re-reads
  the manifest under it and reuses what another worker finished; staging directories are private
  to the downloading process and land with an atomic rename; the manifest is merged under its own
  lock on every write (another worker's entries are never lost), and eviction skips entries any
  worker used within `evict_grace_s`. `flock` on a network filesystem depends on that filesystem's
  lock support [RV per provider volume].

Fetchers (`ce_worker.fetchers`, Phase 8) receive the URI, a staging directory and the file
patterns to fetch; `url://` sources must come with pinned checksums (an unpinned download is
refused). `ce_worker.models` turns a plugin manifest into cache entries. Asking for an
unregistered scheme raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import inspect
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["CacheEntry", "Fetcher", "ModelCache", "ModelCacheError", "ModelFetchError", "parse_uri"]

# (uri, destination directory[, file patterns]) — two-argument fetchers fetch everything
Fetcher = Callable[..., Awaitable[None]]
CHECKSUM_REQUIRED = frozenset({"url"})  # schemes without content addressing: sha256 pins are mandatory


class ModelCacheError(RuntimeError):
    """A model that cannot be cached as declared (no fetcher, a checksum mismatch, an unpinned URL)."""


class ModelFetchError(ModelCacheError):
    """A transient download failure (HTTP status, interrupted transfer): the task is retried."""


def parse_uri(uri: str) -> tuple[str, str, str]:
    """`hf://org/repo@rev/path` → ("hf", "org/repo/path", "rev"); `s3://b/k` → ("s3", "b/k", "")."""
    scheme, sep, rest = uri.partition("://")
    if not sep or not rest or ".." in rest.split("/"):
        raise ModelCacheError(f"not a model URI: {uri!r}")
    revision = ""
    if "@" in rest:
        head, _, tail = rest.partition("@")
        revision, _, path = tail.partition("/")
        rest = f"{head}/{path}".rstrip("/")
    return scheme, rest, revision


@dataclass
class CacheEntry:
    model_key: str
    uri: str
    path: str
    size_bytes: int
    files: dict[str, str] = field(default_factory=dict)  # relative path → sha256
    last_used: float = 0.0
    pinned: bool = False


class ModelCache:
    def __init__(self, root: Path | str, *, max_gb: float = 200.0, evict_grace_s: float = 0.0) -> None:
        self.root = Path(root)
        self.max_bytes = int(max_gb * 1024**3)
        self.evict_grace_s = evict_grace_s
        self.fetchers: dict[str, Fetcher] = {}
        self._manifest = self.root / "manifest.json"
        self._removed: set[str] = set()
        self.entries: dict[str, CacheEntry] = self._load()

    # ------------------------------------------------------------------ locks (shared volumes)
    @contextlib.contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        """An exclusive advisory lock shared by every process that uses this cache root."""
        directory = self.root / ".locks"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(name.encode("utf-8")).hexdigest()[:24] + ".lock")
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.asynccontextmanager
    async def _alock(self, name: str) -> AsyncIterator[None]:
        """`_lock` without blocking the event loop while another worker holds it (a download)."""
        stack = contextlib.ExitStack()
        await asyncio.to_thread(stack.enter_context, self._lock(name))
        try:
            yield
        finally:
            stack.close()

    # ------------------------------------------------------------------ manifest
    def _read(self) -> dict[str, CacheEntry]:
        if not self._manifest.is_file():
            return {}
        data = json.loads(self._manifest.read_text(encoding="utf-8"))
        return {k: CacheEntry(**v) for k, v in data.get("entries", {}).items()}

    def _load(self) -> dict[str, CacheEntry]:
        return self._read()

    def refresh(self) -> None:
        """Picks up what other workers sharing the root added or evicted."""
        with self._lock("manifest"):
            self.entries = {k: v for k, v in self._read().items() if Path(v.path).exists()}

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock("manifest"):
            merged = self._read()
            for key in self._removed:
                merged.pop(key, None)
            for key, entry in self.entries.items():
                theirs = merged.get(key)
                if theirs is not None:  # keep the most recent use and any pin either side made
                    entry.last_used = max(entry.last_used, theirs.last_used)
                    entry.pinned = entry.pinned or theirs.pinned
                merged[key] = entry
            tmp = self._manifest.with_name(f"manifest.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(
                json.dumps({"entries": {k: asdict(v) for k, v in merged.items()}}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self._manifest)
            self.entries = merged
            self._removed.clear()

    # ------------------------------------------------------------------ API
    def register_fetcher(self, scheme: str, fetcher: Fetcher) -> None:
        self.fetchers[scheme] = fetcher

    def cached_models(self) -> list[str]:
        return sorted(self.entries)

    def path_for(self, uri: str, files: Sequence[str] = ()) -> Path:
        """`<root>/<scheme>/<rest>@<revision>`; a subset of a source (file patterns) gets its own
        directory (`…~<hash of the patterns>`) so two plugins needing different files of one
        snapshot never overwrite each other."""
        scheme, rest, revision = parse_uri(uri)
        base = self.root / scheme / (f"{rest}@{revision}" if revision else rest)
        if not files:
            return base
        digest = hashlib.sha256("\n".join(sorted(files)).encode("utf-8")).hexdigest()[:12]
        return base.with_name(f"{base.name}~{digest}")

    async def ensure(
        self,
        model_key: str,
        uri: str,
        *,
        expected: dict[str, str] | None = None,
        pin: bool = False,
        files: Sequence[str] = (),
    ) -> Path:
        """The local directory of `uri`, downloading (only `files`, glob patterns, when given) and
        verifying it on first use."""
        entry = self.entries.get(model_key)
        if entry is not None and Path(entry.path).exists():
            entry.last_used = time.time()
            entry.pinned = entry.pinned or pin
            self._save()
            return Path(entry.path)
        target = self.path_for(uri, files)
        shared = next((e for e in self.entries.values() if Path(e.path) == target and target.exists()), None)
        if shared is not None:  # the same source and files under another key (a dependency shared by plugins)
            for rel, digest in (expected or {}).items():
                if shared.files.get(rel) != digest:
                    raise ModelCacheError(f"{model_key}: {rel} failed its sha256 check")
            self.entries[model_key] = CacheEntry(
                model_key, uri, str(target), shared.size_bytes, dict(shared.files), time.time(), pin
            )
            self._save()
            return target
        scheme, _, _ = parse_uri(uri)
        fetcher = self.fetchers.get(scheme)
        if fetcher is None:
            raise ModelCacheError(f"no fetcher for {scheme}:// on this worker")
        if scheme in CHECKSUM_REQUIRED and not expected:
            raise ModelCacheError(f"{model_key}: {scheme}:// sources need pinned sha256 checksums in the manifest")
        async with self._alock(str(target)):
            # another worker sharing the root may have fetched it while this one waited
            done = next(
                (e for e in self._read().values() if Path(e.path) == target and target.exists()),
                None,
            )
            if done is not None:
                for rel, digest in (expected or {}).items():
                    if done.files.get(rel) != digest:
                        raise ModelCacheError(f"{model_key}: {rel} failed its sha256 check")
                self.entries[model_key] = CacheEntry(
                    model_key, uri, str(target), done.size_bytes, dict(done.files), time.time(), pin
                )
                self._save()
                return target
            return await self._fetch(model_key, uri, target, fetcher, expected=expected, pin=pin, files=files)

    async def _fetch(
        self,
        model_key: str,
        uri: str,
        target: Path,
        fetcher: Fetcher,
        *,
        expected: dict[str, str] | None,
        pin: bool,
        files: Sequence[str],
    ) -> Path:
        staging = target.with_name(f"{target.name}.partial-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        if len(inspect.signature(fetcher).parameters) >= 3:
            await fetcher(uri, staging, list(files))
        else:
            await fetcher(uri, staging)
        hashes = self._hash_tree(staging)
        for rel, digest in (expected or {}).items():
            if hashes.get(rel) != digest:
                shutil.rmtree(staging, ignore_errors=True)
                raise ModelCacheError(f"{model_key}: {rel} failed its sha256 check")
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
        size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        self.entries[model_key] = CacheEntry(model_key, uri, str(target), size, hashes, time.time(), pin)
        self.evict()
        self._save()
        return target

    def evict(self) -> list[str]:
        """Drops least-recently-used unpinned entries until the cache fits `max_bytes`."""
        removed: list[str] = []
        total = sum({e.path: e.size_bytes for e in self.entries.values()}.values())  # shared dirs once
        now = time.time()
        for entry in sorted(self.entries.values(), key=lambda e: e.last_used):
            if total <= self.max_bytes:
                break
            if entry.pinned or now - entry.last_used < self.evict_grace_s:
                continue  # pinned, or in use by some worker sharing the root
            others = [e for e in self.entries.values() if e.model_key != entry.model_key and e.path == entry.path]
            if not others:  # a directory shared with another key stays until its last user goes
                shutil.rmtree(entry.path, ignore_errors=True)
                total -= entry.size_bytes
            removed.append(entry.model_key)
            self._removed.add(entry.model_key)
            del self.entries[entry.model_key]
        if removed:
            self._save()
        return removed

    @staticmethod
    def _hash_tree(directory: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            sha = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    sha.update(chunk)
            out[str(path.relative_to(directory))] = sha.hexdigest()
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "bytes": sum({e.path: e.size_bytes for e in self.entries.values()}.values()),
            "max_bytes": self.max_bytes,
        }
