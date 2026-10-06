#!/usr/bin/env python3
"""Checks the toolchain required by `make bootstrap` (MASTER_BUILD_PROMPT §7, §36).

Errors (exit 1): missing uv, Python 3.12, Node >= 24, pnpm.
Warnings: Docker daemon not reachable (needed for `make infra-up`), FFmpeg missing or
built without libass/HarfBuzz/FriBidi (needed for rendering from Phase 2).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys


def run(*cmd: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    if not shutil.which("uv"):
        errors.append("uv not found (https://docs.astral.sh/uv/)")

    code, out = run("uv", "python", "find", "3.12")
    if code != 0:
        errors.append("Python 3.12 not available to uv (run: uv python install 3.12)")

    code, out = run("node", "--version")
    match = re.match(r"v(\d+)", out)
    if code != 0 or not match:
        errors.append("node not found (Node 24 LTS required)")
    elif int(match.group(1)) < 24:
        errors.append(f"Node {out} found; Node >= 24 required (put a Node 24 LTS bin dir first on PATH)")

    if not shutil.which("pnpm"):
        errors.append("pnpm not found (corepack enable, or npm i -g pnpm)")

    if not shutil.which("docker"):
        warnings.append("docker not found: `make infra-up` needs Docker (or the native fallback, see TODO.md)")
    else:
        code, _ = run("docker", "info")
        if code != 0:
            warnings.append("Docker daemon not reachable: start it before `make infra-up`")

    if not shutil.which("ffmpeg"):
        warnings.append("ffmpeg not found: rendering (Phase 2+) needs FFmpeg with libass, HarfBuzz and FriBidi")
    else:
        _, out = run("ffmpeg", "-hide_banner", "-version")
        for flag in ("libass", "libharfbuzz", "libfribidi"):
            if f"--enable-{flag}" not in out:
                warnings.append(f"ffmpeg built without --enable-{flag} (RTL captions need it, §27)")

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if not errors:
        print("prerequisites OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
