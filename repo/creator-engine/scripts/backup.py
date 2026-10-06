"""Backup and restore of the system of record (Phase 14): Postgres and the two object-store buckets.

What is backed up, and what is not (docs/OPERATIONS.md#backup-and-restore):

- **Postgres** — every row (orgs, specs, versions, jobs, ledger, memory, embeddings): `pg_dump
  --format=custom`, run with the server's own tools inside the Compose `postgres` container
  (`--pg-host-tools` uses the host's `pg_dump`/`pg_restore`, which must not be older than the
  server);
- **object storage** — the assets and artifacts buckets, object by object, each with its
  sha256 (computed while downloading) and content type in `manifest.json`;
- **not backed up**: Valkey (event streams and caches; clients reconnect and resync) and Temporal
  (workflow histories; a restore loses in-flight runs — their jobs show as failed and are re-run).

    uv run python scripts/backup.py backup  --out .data/backups/<name>
    uv run python scripts/backup.py restore --from .data/backups/<name> [--database NAME] [--bucket-suffix -restored]
    uv run python scripts/backup.py verify  --from .data/backups/<name> [--database NAME] [--bucket-suffix -restored]

`restore` refuses a non-empty target database unless `--clean` is given; `verify` compares the
restored database's row counts with the backup's and every object's sha256 with the manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "infra" / "compose" / "docker-compose.yml"),
    "--env-file",
    str(ROOT / ".env"),
]


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _identifier(name: str) -> str:
    """Database names go into SQL as identifiers: only plain names are accepted."""
    if not IDENTIFIER.match(name):
        raise SystemExit(f"not a plain database name: {name!r}")
    return name


def _env_defaults() -> None:
    env_file = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def _db() -> dict[str, str]:
    url = urlsplit(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1))
    return {
        "user": url.username or "",
        "password": url.password or "",
        "host": url.hostname or "localhost",
        "port": str(url.port or 5432),
        "name": url.path.lstrip("/"),
    }


def _pg(args: argparse.Namespace, tool: str, *extra: str, stdin: Any = None, stdout: Any = None) -> None:
    db = _db()
    if args.pg_host_tools:
        cmd = [tool, "-h", db["host"], "-p", db["port"], "-U", db["user"], *extra]
        env = {**os.environ, "PGPASSWORD": db["password"]}
    else:  # the server's own client tools, inside the Compose postgres container
        cmd = [*COMPOSE, "exec", "-T", "-e", f"PGPASSWORD={db['password']}", "postgres", tool, "-U", db["user"], *extra]
        env = dict(os.environ)
    result = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{tool} failed ({result.returncode}): {result.stderr.decode(errors='replace')[-2000:]}")


def _psql(args: argparse.Namespace, database: str, sql: str) -> str:
    db = _db()
    if args.pg_host_tools:
        cmd = ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", database, "-tAc", sql]
        env = {**os.environ, "PGPASSWORD": db["password"]}
    else:
        cmd = [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", db["user"], "-d", database, "-tAc", sql]
        env = dict(os.environ)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"psql failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


def _row_counts(args: argparse.Namespace, database: str) -> dict[str, int]:
    tables = _psql(
        args, database, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).splitlines()
    names = [t for t in tables if IDENTIFIER.match(t)]  # catalog names; anything odd is skipped, not quoted
    sql = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM public.\"{t}\"" for t in names)  # noqa: S608
    rows = _psql(args, database, sql).splitlines() if sql else []
    return {name: int(count) for name, count in (line.split("|") for line in rows)}


def _storage() -> tuple[Any, list[str]]:
    from ce_config.settings import load_effective
    from ce_storage import create_storage

    effective = load_effective(ROOT / "config")
    settings = effective.settings
    return create_storage(settings), [settings.s3_bucket_assets, settings.s3_bucket_artifacts]


async def _backup_storage(out: Path) -> dict[str, Any]:
    storage, buckets = _storage()
    manifest: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for bucket in buckets:
        entries = []
        for item in await storage.list(bucket, limit=10**9):
            dest = out / "storage" / bucket / item.key
            info = await storage.download(bucket, item.key, dest)
            sha = hashlib.sha256(dest.read_bytes()).hexdigest()
            entries.append({"key": item.key, "size": info.size, "sha256": sha, "content_type": info.content_type})
            total += info.size
        manifest[bucket] = entries
    return {"buckets": manifest, "bytes": total}


async def _restore_storage(src: Path, manifest: dict[str, Any], suffix: str) -> int:
    storage, _ = _storage()
    restored = 0
    for bucket, entries in manifest["buckets"].items():
        target = bucket + suffix
        await storage.ensure_bucket(target)
        for entry in entries:
            body = (src / "storage" / bucket / entry["key"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise SystemExit(f"backup corrupted: {bucket}/{entry['key']} does not match its manifest sha256")
            await storage.put(
                target, entry["key"], body, content_type=entry["content_type"] or "application/octet-stream"
            )
            restored += 1
    return restored


async def _verify_storage(manifest: dict[str, Any], suffix: str) -> list[str]:
    storage, _ = _storage()
    problems = []
    for bucket, entries in manifest["buckets"].items():
        for entry in entries:
            try:
                body = await storage.get(bucket + suffix, entry["key"])
            except Exception as exc:
                problems.append(f"{bucket + suffix}/{entry['key']}: {exc}")
                continue
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                problems.append(f"{bucket + suffix}/{entry['key']}: sha256 differs")
    return problems


def backup(args: argparse.Namespace) -> int:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=False)
    db = _db()
    started = time.monotonic()
    with (out / "database.dump").open("wb") as fh:
        _pg(args, "pg_dump", "-d", db["name"], "--format=custom", "--no-owner", stdout=fh)
    db_seconds = time.monotonic() - started
    counts = _row_counts(args, db["name"])
    started = time.monotonic()
    storage = asyncio.run(_backup_storage(out))
    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": db["name"],
        "alembic_revision": _psql(args, db["name"], "SELECT version_num FROM alembic_version"),
        "row_counts": counts,
        "database_dump_bytes": (out / "database.dump").stat().st_size,
        "database_seconds": round(db_seconds, 2),
        "storage_seconds": round(time.monotonic() - started, 2),
        **storage,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    objects = sum(len(v) for v in storage["buckets"].values())
    print(
        f"backup {out}: {sum(counts.values())} rows in {len(counts)} tables "
        f"({manifest['database_dump_bytes']} bytes dumped in {manifest['database_seconds']} s), "
        f"{objects} objects ({storage['bytes']} bytes in {manifest['storage_seconds']} s)"
    )
    return 0


def restore(args: argparse.Namespace) -> int:
    src: Path = args.from_
    manifest = json.loads((src / "manifest.json").read_text())
    target = _identifier(args.database or _db()["name"])
    query = f"SELECT 1 FROM pg_database WHERE datname = '{target}'"  # noqa: S608 - a checked plain identifier
    exists = _psql(args, "postgres", query) == "1"
    if not exists:
        _psql(args, "postgres", f'CREATE DATABASE "{target}"')
    elif not args.clean and _psql(args, target, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") != "0":
        raise SystemExit(f"database {target} is not empty; pass --clean to replace its contents")
    _psql(args, target, "CREATE EXTENSION IF NOT EXISTS vector")
    started = time.monotonic()
    extra = ["--clean", "--if-exists"] if args.clean else []
    with (src / "database.dump").open("rb") as fh:
        _pg(args, "pg_restore", "-d", target, "--no-owner", "--exit-on-error", *extra, stdin=fh)
    db_seconds = time.monotonic() - started
    started = time.monotonic()
    objects = asyncio.run(_restore_storage(src, manifest, args.bucket_suffix))
    print(
        f"restored {src} into database {target} in {db_seconds:.2f} s and {objects} objects into "
        f"buckets *{args.bucket_suffix} in {time.monotonic() - started:.2f} s"
    )
    return 0


def verify(args: argparse.Namespace) -> int:
    src: Path = args.from_
    manifest = json.loads((src / "manifest.json").read_text())
    target = _identifier(args.database or _db()["name"])
    counts = _row_counts(args, target)
    problems = [
        f"{table}: {manifest['row_counts'].get(table)} rows backed up, {counts.get(table)} restored"
        for table in sorted(set(counts) | set(manifest["row_counts"]))
        if counts.get(table) != manifest["row_counts"].get(table)
    ]
    revision = _psql(args, target, "SELECT version_num FROM alembic_version")
    if revision != manifest["alembic_revision"]:
        problems.append(f"alembic revision {revision} != {manifest['alembic_revision']}")
    problems += asyncio.run(_verify_storage(manifest, args.bucket_suffix))
    objects = sum(len(v) for v in manifest["buckets"].values())
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"verified: {len(counts)} tables, {sum(counts.values())} rows and {objects} objects match the backup")
    return 0


def main() -> int:
    _env_defaults()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pg-host-tools", action="store_true", help="use the host's pg_dump/pg_restore/psql")
    sub = parser.add_subparsers(dest="command", required=True)
    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--out", type=Path, required=True)
    for name in ("restore", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--from", dest="from_", type=Path, required=True)
        p.add_argument("--database", help="target database (default: the one in DATABASE_URL)")
        p.add_argument("--bucket-suffix", default="", help="restore into <bucket><suffix> (a drill next to live data)")
        if name == "restore":
            p.add_argument("--clean", action="store_true", help="drop and replace the target's objects")
    args = parser.parse_args()
    return {"backup": backup, "restore": restore, "verify": verify}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
