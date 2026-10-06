#!/usr/bin/env python3
"""Downloads the CPU model assets listed in scripts/cpu_assets.yaml into MODEL_CACHE_DIR.

Every entry must pin a URL and a SHA-256 and name its license (rule 15). Files that already
exist with the right checksum are skipped. `--check` only verifies what is present (exit 1 when
anything is missing or differs); `--plugin ID` limits the run to one plugin's files.

The real CPU engines (Kokoro [dev only], faster-whisper, MediaPipe, AuraFace, DINOv2, DNSMOS)
are used when their files are present (`CPU_REAL_ENGINES=auto`) or required (`on`).
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "scripts" / "cpu_assets.yaml"


def load_dotenv(path: Path) -> None:
    """Minimal .env reader so the script honors MODEL_CACHE_DIR without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify only; download nothing")
    parser.add_argument("--plugin", help="only the files this plugin lists")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    cache = Path(os.environ.get("MODEL_CACHE_DIR") or ROOT / ".cache" / "models")
    if not cache.is_absolute():
        cache = ROOT / cache
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    assets = [a for a in data.get("assets") or [] if not args.plugin or args.plugin in (a.get("plugins") or [])]
    if not assets:
        print("No CPU assets match. Nothing to fetch.")
        return 0
    failures = 0
    for asset in assets:
        for field in ("name", "url", "sha256", "dest", "license"):
            if not asset.get(field):
                print(f"ERROR asset {asset.get('name', '?')}: missing field '{field}'")
                failures += 1
                break
        else:
            dest = cache / asset["dest"]
            if dest.exists() and sha256(dest) == asset["sha256"]:
                print(f"ok      {asset['name']} (cached)")
                continue
            if args.check:
                print(f"MISSING {asset['name']} ({dest})")
                failures += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            print(f"fetch   {asset['name']} <- {asset['url']}")
            urllib.request.urlretrieve(asset["url"], tmp)  # noqa: S310 - URLs come from the pinned manifest
            actual = sha256(tmp)
            if actual != asset["sha256"]:
                tmp.unlink(missing_ok=True)
                print(f"ERROR   {asset['name']}: sha256 mismatch ({actual})")
                failures += 1
                continue
            tmp.replace(dest)
            print(f"ok      {asset['name']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
