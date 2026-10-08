"""Model-cache fetchers (§25, Phase 8): Hugging Face snapshots at a pinned revision, objects under
an S3 prefix (an owner's mirror bucket) and single pinned HTTPS files.

Every fetcher downloads into the cache's staging directory and fetches only the manifest's file
patterns (`files`, fnmatch globs; empty = everything). Integrity is the cache's job: it hashes the
staged tree and checks the manifest's `sha256` pins before the atomic rename (and refuses `url://`
sources without pins). Hugging Face revisions are commit SHAs, so a snapshot cannot change under
a pin; `HF_TOKEN` is sent for gated repositories when set."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from ce_worker.model_cache import FetchProgress, ModelCacheError, ModelFetchError, parse_uri

__all__ = ["huggingface_fetcher", "matches", "s3_fetcher", "url_fetcher"]

ClientFactory = Callable[[], httpx.AsyncClient]


def matches(name: str, patterns: Sequence[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(name, p) for p in patterns)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0), follow_redirects=True)


async def _download(
    client: httpx.AsyncClient,
    url: str,
    target: Path,
    headers: dict[str, str],
    progress: FetchProgress | None = None,
    expected_size: int | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    written = 0
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            raise ModelFetchError(f"GET {url}: HTTP {response.status_code}")
        with tmp.open("wb") as out:
            async for chunk in response.aiter_bytes(1 << 20):
                out.write(chunk)
                written += len(chunk)
                if progress is not None:
                    progress.add(len(chunk))
    if expected_size is not None and written != expected_size:
        tmp.unlink(missing_ok=True)
        raise ModelFetchError(f"GET {url}: {written} bytes, the listing said {expected_size} (interrupted)")
    os.replace(tmp, target)
    if progress is not None:
        progress.files_done += 1


def huggingface_fetcher(
    *, endpoint: str = "https://huggingface.co", token: str | None = None, client: ClientFactory = _client
) -> Any:
    """`hf://org/repo@<commit>` → the snapshot's files matching the patterns."""

    async def fetch(uri: str, dest: Path, files: Sequence[str] = (), *, progress: FetchProgress | None = None) -> None:
        scheme, rest, revision = parse_uri(uri)
        if scheme != "hf" or not revision:
            raise ModelCacheError(f"not a pinned Hugging Face URI: {uri}")
        repo = "/".join(rest.split("/")[:2])
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with client() as http:
            # `blobs=true` adds each file's size and, for LFS objects, its sha256: the progress total,
            # the free-disk check before anything is written, and a content check of every weight file
            listing = await http.get(
                f"{endpoint}/api/models/{repo}/revision/{revision}", params={"blobs": "true"}, headers=headers
            )
            if listing.status_code != 200:
                raise ModelFetchError(f"{repo}@{revision}: listing failed (HTTP {listing.status_code})")
            siblings = [s for s in listing.json().get("siblings", []) if isinstance(s, dict) and s.get("rfilename")]
            names = [str(s["rfilename"]) for s in siblings]
            by_name = {str(s["rfilename"]): s for s in siblings}
            wanted = [n for n in names if matches(n, files)]
            missing = [p for p in files if not any(fnmatch.fnmatch(n, p) for n in names)]
            if missing:
                raise ModelCacheError(f"{repo}@{revision}: no files match {missing}")
            for name in wanted:
                if ".." in Path(name).parts:
                    raise ModelCacheError(f"{repo}: unsafe file name {name!r}")
            sizes = {n: _size(by_name[n]) for n in wanted}
            if progress is not None:
                for name in wanted:
                    lfs = by_name[name].get("lfs")
                    if isinstance(lfs, dict) and isinstance(lfs.get("sha256"), str):
                        progress.remote_sha256[name] = str(lfs["sha256"])
                known = [v for v in sizes.values() if v is not None]
                progress.expect(sum(known) if len(known) == len(wanted) else None, len(wanted))
            for name in wanted:
                await _download(
                    http, f"{endpoint}/{repo}/resolve/{revision}/{name}", dest / name, headers, progress, sizes[name]
                )

    return fetch


def _size(sibling: dict[str, Any]) -> int | None:
    lfs = sibling.get("lfs")
    value = lfs.get("size") if isinstance(lfs, dict) else sibling.get("size")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def url_fetcher(*, client: ClientFactory = _client) -> Any:
    """`url://host/path@<revision>` → `https://host/path` saved as the manifest's single file name
    (the cache insists on sha256 pins for this scheme)."""

    async def fetch(uri: str, dest: Path, files: Sequence[str] = (), *, progress: FetchProgress | None = None) -> None:
        scheme, rest, _ = parse_uri(uri)
        if scheme != "url":
            raise ModelCacheError(f"not a url:// URI: {uri}")
        if len(files) > 1:
            raise ModelCacheError("a url:// source is a single file")
        name = files[0] if files else Path(rest).name
        async with client() as http:
            await _download(http, f"https://{rest}", dest / name, {}, progress)

    return fetch


def s3_fetcher(storage: Any) -> Any:
    """`s3://bucket/prefix` → the objects under the prefix matching the patterns, through a
    `StorageProvider` holding the model bucket's credentials."""

    async def fetch(uri: str, dest: Path, files: Sequence[str] = ()) -> None:
        scheme, rest, _ = parse_uri(uri)
        if scheme != "s3":
            raise ModelCacheError(f"not an s3:// URI: {uri}")
        bucket, _, prefix = rest.partition("/")
        prefix = prefix.rstrip("/") + "/" if prefix else ""
        objects = await storage.list(bucket, prefix, limit=10_000)
        names = [(o.key, o.key[len(prefix) :]) for o in objects if not o.key.endswith("/")]
        wanted = [(key, rel) for key, rel in names if matches(rel, files)]
        if not wanted:
            raise ModelCacheError(f"{uri}: no objects match {list(files) or ['*']}")
        for key, rel in wanted:
            if ".." in Path(rel).parts:
                raise ModelCacheError(f"{uri}: unsafe key {key!r}")
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            await storage.download(bucket, key, dest / rel)

    return fetch
