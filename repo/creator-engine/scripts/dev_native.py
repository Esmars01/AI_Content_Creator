"""Runs the application services as host processes against the Compose infrastructure (Phase 14).

The native mode for hosts where the service images cannot be built (no access to the Debian
package mirror, a CI runner without Docker builds) or for debugging with a local interpreter:
`make infra-up` provides Postgres, Valkey, Temporal and SeaweedFS in containers; this script
starts the API, scheduler, orchestrator, render worker, a CPU worker and (optionally) the web app
on the host, each with its own metrics port, and keeps their pids and logs under `.data/native`.

    uv run python scripts/dev_native.py up [--no-web] [--web-dev]
    uv run python scripts/dev_native.py status
    uv run python scripts/dev_native.py down

Ports: API 8000 (metrics 9101), scheduler 8100 (metrics 9105), orchestrator metrics 9102,
render worker 9103, CPU worker 9104, web 3000. `infra/observability/prometheus.native.yml`
scrapes exactly these.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".data" / "native"
PIDS = STATE / "pids.json"

SERVICES: dict[str, dict[str, Any]] = {
    "api": {
        "cmd": [
            "uvicorn",
            "--factory",
            "ce_api.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--no-access-log",
        ],
        "env": {"METRICS_PORT": "9101"},
        "ready": "http://127.0.0.1:8000/readyz",
    },
    "scheduler": {
        "cmd": ["python", "-m", "ce_scheduler"],
        "env": {"SCHEDULER_PORT": "8100", "METRICS_PORT": "9105"},
        "ready": "http://127.0.0.1:8100/healthz",
    },
    "orchestrator": {
        "cmd": ["python", "-m", "ce_orchestrator"],
        "env": {"METRICS_PORT": "9102"},
        "ready": "http://127.0.0.1:9102/metrics",
    },
    "render-worker": {
        "cmd": ["python", "-m", "ce_render_worker"],
        "env": {"METRICS_PORT": "9103"},
        "ready": "http://127.0.0.1:9103/metrics",
    },
    "worker-cpu": {
        "cmd": ["python", "-m", "ce_gpu_worker"],
        "env": {
            "METRICS_PORT": "9104",
            "WORKER_RUNTIME_FAMILY": "cpu_model",
            "WORKER_NAME": "worker-cpu-native",
            "WORKER_CONCURRENCY": os.environ.get("WORKER_CPU_CONCURRENCY", "2"),  # as in Compose
            "SCHEDULER_URL": "http://127.0.0.1:8100",
        },
        "ready": "http://127.0.0.1:9104/metrics",
    },
}
WEB: dict[str, Any] = {
    "cmd": ["pnpm", "--filter", "@ce/web", "start"],
    "env": {"API_INTERNAL_URL": "http://127.0.0.1:8000"},
    "ready": "http://127.0.0.1:3000/login",
}


def _env_defaults() -> dict[str, str]:
    env = dict(os.environ)
    env_file = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            env.setdefault(key.strip(), value.strip())
    env.setdefault("CE_CONFIG_ROOT", str(ROOT / "config"))
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _load() -> dict[str, int]:
    if not PIDS.exists():
        return {}
    data: dict[str, int] = json.loads(PIDS.read_text())
    return {name: pid for name, pid in data.items() if _alive(pid)}


def _ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - fixed local URLs
            return bool(response.status == 200)
    except OSError:
        return False


def up(args: argparse.Namespace) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    running = _load()
    base = _env_defaults()
    services = dict(SERVICES)
    if not args.no_web:
        web = dict(WEB)
        if args.web_dev:
            web["cmd"] = ["pnpm", "--filter", "@ce/web", "dev"]
        services["web"] = web
    for name, spec in services.items():
        if name in running:
            print(f"{name}: already running (pid {running[name]})")
            continue
        if _ready(str(spec["ready"])):
            # Something else already answers on this service's port — typically a container left by
            # `make dev`. Starting anyway would report "ready" for the wrong process (audit D4).
            print(
                f"{name}: {spec['ready']} already answers but was not started here; stop that process "
                "first (`make dev-down`)",
                file=sys.stderr,
            )
            return 1
        env = {**base, **spec["env"]}
        cmd = list(spec["cmd"])
        if cmd[0] in ("uvicorn", "python"):
            cmd = ["uv", "run", "--no-sync", *cmd]
        log = (STATE / f"{name}.log").open("ab")
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        running[name] = proc.pid
        print(f"{name}: started (pid {proc.pid}, log .data/native/{name}.log)")
        PIDS.write_text(json.dumps(running, indent=2))
    deadline = time.monotonic() + args.timeout
    pending = {name: str(spec["ready"]) for name, spec in services.items()}
    while pending and time.monotonic() < deadline:
        for name, url in list(pending.items()):
            if name in running and not _alive(running[name]):  # before the URL: it may be someone else's
                print(f"{name}: exited — see .data/native/{name}.log", file=sys.stderr)
                return 1
            if _ready(url):
                print(f"{name}: ready ({url})")
                del pending[name]
        time.sleep(1.0)
    if pending:
        print(f"not ready after {args.timeout:.0f}s: {', '.join(pending)}", file=sys.stderr)
        return 1
    return 0


def down(_: argparse.Namespace) -> int:
    running = _load()
    for name, pid in running.items():
        try:
            os.killpg(pid, signal.SIGTERM)
            print(f"{name}: stopping (pid {pid})")
        except OSError:
            pass
    deadline = time.monotonic() + 20
    while any(_alive(p) for p in running.values()) and time.monotonic() < deadline:
        time.sleep(0.5)
    for name, pid in running.items():
        if _alive(pid):
            os.killpg(pid, signal.SIGKILL)
            print(f"{name}: killed")
    PIDS.unlink(missing_ok=True)
    return 0


def status(_: argparse.Namespace) -> int:
    running = _load()
    for name, spec in {**SERVICES, "web": WEB}.items():
        pid = running.get(name)
        state = "ready" if pid and _ready(str(spec["ready"])) else ("starting" if pid else "stopped")
        print(f"{name:14} {state:9} {pid or ''}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_up = sub.add_parser("up")
    p_up.add_argument("--no-web", action="store_true", help="API and services only")
    p_up.add_argument("--web-dev", action="store_true", help="`next dev` instead of the production build")
    p_up.add_argument("--timeout", type=float, default=120.0)
    sub.add_parser("down")
    sub.add_parser("status")
    args = parser.parse_args()
    return {"up": up, "down": down, "status": status}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
