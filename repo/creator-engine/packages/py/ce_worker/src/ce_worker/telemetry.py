"""What a worker reports about itself (cutover §19): GPU utilization, VRAM, temperature, power,
driver, the model-cache disk and the cache.

Only measured values are reported. A value the host cannot measure (no `nvidia-smi`, `[N/A]` from
the driver, an unreadable disk) is left out, never sent as 0, so the console can say "not reported"
instead of showing a healthy-looking zero. `nvidia-smi` runs in a thread with a timeout, at most once
per `min_interval_s`, so telemetry never blocks the lease or heartbeat loop.

Python 3.10 compatible (the GPU family images import it).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import Any

__all__ = ["GPU_QUERY", "TelemetryCollector", "disk_snapshot", "gpu_snapshot", "parse_gpu_rows"]

GPU_QUERY = ("index", "name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw",
             "driver_version")  # fmt: skip
_NUMERIC = {"utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw"}
_KEYS = {
    "index": "index",
    "name": "name",
    "utilization.gpu": "util_pct",
    "memory.used": "vram_used_gb",
    "memory.total": "vram_total_gb",
    "temperature.gpu": "temp_c",
    "power.draw": "power_w",
    "driver_version": "driver_version",
}


def _number(text: str) -> float | None:
    text = text.strip()
    if not text or text.startswith("[") or text.lower() in ("n/a", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_gpu_rows(output: str) -> list[dict[str, Any]]:
    """`nvidia-smi --query-gpu=<GPU_QUERY> --format=csv,noheader,nounits` → one dict per GPU, MiB as
    GiB; fields the driver did not report are absent."""
    gpus: list[dict[str, Any]] = []
    for line in output.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(GPU_QUERY):
            continue
        gpu: dict[str, Any] = {}
        for field, cell in zip(GPU_QUERY, cells, strict=True):
            key = _KEYS[field]
            if field in _NUMERIC:
                value = _number(cell)
                if value is None:
                    continue
                gpu[key] = round(value / 1024, 2) if field.startswith("memory.") else round(value, 1)
            elif field == "index":
                index = _number(cell)
                if index is not None:
                    gpu[key] = int(index)
            elif cell and not cell.startswith("["):
                gpu[key] = cell
        gpus.append(gpu)
    return gpus


def gpu_snapshot(timeout_s: float = 5.0) -> list[dict[str, Any]] | None:
    """None without `nvidia-smi` or when it fails (no GPU telemetry at all, as opposed to zeros)."""
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, the binary resolved by shutil.which
            [binary, f"--query-gpu={','.join(GPU_QUERY)}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_gpu_rows(out) or None


def disk_snapshot(path: str) -> dict[str, Any] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    gib = 1024**3
    return {"path": path, "total_gb": round(usage.total / gib, 2), "free_gb": round(usage.free / gib, 2)}


class TelemetryCollector:
    """Caches the last snapshot for `min_interval_s`; `extra()` adds the runtime's own facts."""

    def __init__(self, cache_dir: str, *, min_interval_s: float = 10.0, started: float | None = None) -> None:
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.started = started if started is not None else time.monotonic()
        self._last: dict[str, Any] | None = None
        self._at = 0.0

    async def snapshot(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.monotonic()
        if self._last is None or now - self._at >= self.min_interval_s:
            gpus, disk = await asyncio.gather(
                asyncio.to_thread(gpu_snapshot), asyncio.to_thread(disk_snapshot, self.cache_dir)
            )
            measured: dict[str, Any] = {}
            if gpus:
                measured["gpus"] = gpus
                first = gpus[0]
                for key in ("util_pct", "vram_used_gb", "vram_total_gb", "temp_c", "power_w", "driver_version"):
                    if key in first:
                        measured[f"gpu_{key}" if not key.startswith(("vram", "driver")) else key] = first[key]
            if disk:
                measured["disk"] = disk
            self._last, self._at = measured, now
        out = dict(self._last)
        out["uptime_s"] = round(now - self.started, 1)
        for key, value in (extra or {}).items():
            if value is not None:
                out[key] = value
        return out
