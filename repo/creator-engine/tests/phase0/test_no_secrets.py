"""Rule 16: no secrets in code, logs, fixtures or commits.

Scans every file git would commit (tracked plus untracked-but-not-ignored) for credential
shapes, and checks that `.env` itself is ignored. Dev-only values in `.env.example` are
allowed because they only work against the local Compose stack.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.phase0 import spec_index as spec

ROOT = spec.ROOT

SECRET_PATTERNS = {
    "AWS access key id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI-style API key": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "RunPod API key": re.compile(r"\brpa_[A-Za-z0-9]{30,}"),
}


def _in_git_work_tree() -> bool:
    """Inside any git work tree: the repository root, or the AI_Content_Creator workspace, where the
    sources are `repo/creator-engine` of the outer repository (git commands then run on that subtree)."""
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


pytestmark = pytest.mark.skipif(not _in_git_work_tree(), reason="not a git checkout")


def committable_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [f for f in out.split("\0") if f and (ROOT / f).is_file()]


def test_env_file_is_ignored() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False)
    assert result.returncode == 0, ".env must be git-ignored"


def test_no_credential_shapes_in_committable_files() -> None:
    findings: list[str] = []
    for name in committable_files():
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}: {label}")
    assert not findings, "possible secrets: " + "; ".join(findings)
