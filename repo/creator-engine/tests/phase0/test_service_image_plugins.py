"""The execution-services image carries every plugin of the workspace (audit D6).

The orchestrator routes, and the scheduler accepts a registering worker's adapters, only for the
plugin manifests installed in its own image. Native mode installs the whole workspace; an image that
lacked a plugin (all GPU adapters, `captions_llm`) silently behaved differently: a GPU worker that
registered with the Compose scheduler had every adapter dropped and never leased a task.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra" / "docker" / "services.Dockerfile"


def plugin_packages() -> set[str]:
    names = set()
    for path in (ROOT / "plugins").rglob("pyproject.toml"):
        if "node_modules" in path.parts or ".venv" in path.parts:
            continue
        names.add(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"])
    return names


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_the_services_image_installs_every_plugin() -> None:
    packages = re.findall(r"--package (\S+)", DOCKERFILE.read_text(encoding="utf-8"))
    assert "ce-orchestrator" in packages and "ce-scheduler" in packages
    args = ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-header"]
    for name in packages:
        args += ["--package", name]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    installed = {line.rsplit("/", 1)[-1] for line in out.splitlines() if line.startswith(("./", "-e ./"))}
    folder_of = {
        tomllib.loads(p.read_text(encoding="utf-8"))["project"]["name"]: p.parent.name
        for p in (ROOT / "plugins").rglob("pyproject.toml")
        if "node_modules" not in p.parts
    }
    missing = sorted(name for name in plugin_packages() if name not in packages and folder_of[name] not in installed)
    assert not missing, f"add to infra/docker/services.Dockerfile (uv sync --package …): {missing}"
