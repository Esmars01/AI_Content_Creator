"""`python -m ce_worker.multi`: one GPU instance, one worker per family (cutover §14, worker profiles).

A profile image (`worker-profile-<name>.Dockerfile`) holds several variant environments side by side
(InfiniteTalk's torch 2.4.1 next to Chatterbox's 2.6.0). This supervisor starts `python -m ce_worker`
once per family listed in `WORKER_COMPONENTS`, each with its own interpreter and identity:

- `WORKER_PYTHON_<FAMILY>`: that environment's Python (default: this interpreter);
- `WORKER_TOKEN_<FAMILY>`, `WORKER_ID_<FAMILY>`, `WORKER_NAME_<FAMILY>`, `WORKER_ADAPTERS_<FAMILY>`,
  `WORKER_PREPARE_<FAMILY>`, `WORKER_PYTHONPATH_<FAMILY>`: become that worker's `WORKER_TOKEN`, … — the
  fleet provisions one worker row and one enrollment token per family on the same instance;
- everything else (`SCHEDULER_URL`, `MODEL_CACHE_DIR`, `HF_TOKEN`, …) is shared: both workers use one
  model cache on the instance's disk.

SIGTERM and SIGINT go to every worker; when one exits, the others are stopped and the supervisor exits
with its code, so the instance's state shows the failure instead of half a profile running silently.
Standard library only (it runs under any of the image's environments).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

__all__ = ["child_environments", "main", "supervise"]

_PER_FAMILY = ("TOKEN", "ID", "NAME", "ADAPTERS", "PREPARE", "PREPARE_WARM", "PYTHONPATH", "PYTHON")


def child_environments(env: Mapping[str, str]) -> list[tuple[str, list[str], dict[str, str]]]:
    """(family, argv, environment) per component; raises ValueError on an incomplete profile."""
    families = [f.strip() for f in env.get("WORKER_COMPONENTS", "").split(",") if f.strip()]
    if not families:
        raise ValueError("WORKER_COMPONENTS is empty: name the families this instance serves")
    if len(set(families)) != len(families):
        raise ValueError("WORKER_COMPONENTS lists a family twice")
    suffixes = {f"_{f.upper()}" for f in families}
    shared = {
        k: v
        for k, v in env.items()
        if not any(k.startswith(f"WORKER_{name}_") and k[len(f"WORKER_{name}") :] in suffixes for name in _PER_FAMILY)
    }
    out = []
    for family in families:
        key = family.upper()
        token = env.get(f"WORKER_TOKEN_{key}", "")
        if not token and env.get("APP_ENV", "prod") == "prod":
            raise ValueError(f"WORKER_TOKEN_{key} is missing: the fleet gives each family its own enrollment token")
        child = dict(shared)
        child["WORKER_RUNTIME_FAMILY"] = family
        for name in _PER_FAMILY:
            value = env.get(f"WORKER_{name}_{key}")
            if value is None:
                continue
            if name == "PYTHONPATH":
                child["PYTHONPATH"] = value
            elif name != "PYTHON":
                child[f"WORKER_{name}"] = value
        if token:
            child["WORKER_TOKEN"] = token
        child.setdefault("WORKER_NAME", f"{env.get('WORKER_NAME', 'worker')}-{family}")
        python = env.get(f"WORKER_PYTHON_{key}") or sys.executable
        out.append((family, [python, "-m", "ce_worker"], child))
    return out


def supervise(
    children: Sequence[tuple[str, list[str], dict[str, str]]], *, poll_s: float = 1.0, grace_s: float = 30.0
) -> int:
    procs = [(family, subprocess.Popen(argv, env=env)) for family, argv, env in children]  # noqa: S603

    def stop(signum: int = signal.SIGTERM, _frame: object = None) -> None:
        for _, proc in procs:
            if proc.poll() is None:
                proc.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        for family, proc in procs:
            code = proc.poll()
            if code is not None:
                print(f"ce_worker.multi: the {family} worker exited with {code}; stopping the others", flush=True)
                stop()
                deadline = time.monotonic() + grace_s
                for _, other in procs:
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        other.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        other.kill()
                return code
        time.sleep(poll_s)


def main() -> None:
    try:
        children = child_environments(os.environ)
    except ValueError as exc:
        print(f"ce_worker.multi: refusing to start: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    raise SystemExit(supervise(children))


if __name__ == "__main__":
    main()
