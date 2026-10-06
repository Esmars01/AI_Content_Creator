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

from ce_worker.model_cache import ModelCacheError, ModelFetchError, parse_uri

__all__ = ["huggingface_fetcher", "matches", "s3_fetcher", "url_fetcher"]

ClientFactory = Callable[[], httpx.AsyncClient]


def matches(name: str, patterns: Sequence[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(name, p) for p in patterns)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0), follow_redirects=True)


async def _download(client: httpx.AsyncClient, url: str, target: Path, headers: dict[str, str]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            raise ModelFetchError(f"GET {url}: HTTP {response.status_code}")
        with tmp.open("wb") as out:
            async for chunk in response.aiter_bytes(1 << 20):
                out.write(chunk)
    os.replace(tmp, target)


def huggingface_fetcher(
    *, endpoint: str = "https://huggingface.co", token: str | None = None, client: ClientFactory = _client
) -> Any:
    """`hf://org/repo@<commit>` → the snapshot's files matching the patterns."""

    async def fetch(uri: str, dest: Path, files: Sequence[str] = ()) -> None:
        scheme, rest, revision = parse_uri(uri)
        if scheme != "hf" or not revision:
            raise ModelCacheError(f"not a pinned Hugging Face URI: {uri}")
        repo = "/".join(rest.split("/")[:2])
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with client() as http:
            listing = await http.get(f"{endpoint}/api/models/{repo}/revision/{revision}", headers=headers)
            if listing.status_code != 200:
                raise ModelFetchError(f"{repo}@{revision}: listing failed (HTTP {listing.status_code})")
            names = [s["rfilename"] for s in listing.json().get("siblings", [])]
            wanted = [n for n in names if matches(n, files)]
            missing = [p for p in files if not any(fnmatch.fnmatch(n, p) for n in names)]
            if missing:
                raise ModelCacheError(f"{repo}@{revision}: no files match {missing}")
            for name in wanted:
                if ".." in Path(name).parts:
                    raise ModelCacheError(f"{repo}: unsafe file name {name!r}")
                await _download(http, f"{endpoint}/{repo}/resolve/{revision}/{name}", dest / name, headers)

    return fetch


def url_fetcher(*, client: ClientFactory = _client) -> Any:
    """`url://host/path@<revision>` → `https://host/path` saved as the manifest's single file name
    (the cache insists on sha256 pins for this scheme)."""

    async def fetch(uri: str, dest: Path, files: Sequence[str] = ()) -> None:
        scheme, rest, _ = parse_uri(uri)
        if scheme != "url":
            raise ModelCacheError(f"not a url:// URI: {uri}")
        if len(files) > 1:
            raise ModelCacheError("a url:// source is a single file")
        name = files[0] if files else Path(rest).name
        async with client() as http:
            await _download(http, f"https://{rest}", dest / name, {})

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
