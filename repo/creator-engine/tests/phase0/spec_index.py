"""Reads the facts Phase 0 tests check out of docs/MASTER_BUILD_PROMPT.md itself.

The tests derive expectations from the spec (the §8 layout tree, the §4 invariants, the §6
ADRs, the §35 env block, the §36 make targets) instead of copying them, so the repository
and the spec cannot drift apart silently.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "MASTER_BUILD_PROMPT.md"


@cache
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


@cache
def sections() -> dict[str, str]:
    parts = re.split(r"^## (\d+)\. .*$", spec_text(), flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def layout_tree() -> str:
    block = re.search(r"```\n(creator-engine/.*?)```", sections()["8"], re.S)
    assert block, "§8 layout block not found"
    return block.group(1)


def layout_children(parent_line_prefix: str, depth_prefix: str) -> list[str]:
    """Names of the entries directly under the tree node whose line starts with parent_line_prefix."""
    lines = layout_tree().splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(parent_line_prefix):
            inside = True
            continue
        if inside:
            if not line.startswith(depth_prefix):
                break
            rest = line[len(depth_prefix) :]
            match = re.match(r"[├└]── ([A-Za-z0-9_.-]+/?)", rest)
            if match:
                out.append(match.group(1))
    return out


def top_level_entries() -> list[str]:
    return [m.group(1) for m in re.finditer(r"^[├└]── ([A-Za-z0-9_.-]+/?)", layout_tree(), re.M)]


def python_packages() -> list[str]:
    return re.findall(r"^│   │   [├└]── (ce_[a-z_]+)/", layout_tree(), re.M)


def apps() -> list[str]:
    return layout_children("├── apps/", "│   ")


def plugin_categories() -> list[str]:
    return [
        m.group(1)
        for m in re.finditer(
            r"^│   [├└]── ([a-z_]+)/", layout_tree().split("├── plugins/")[1].split("├── config/")[0], re.M
        )
    ]


def config_dirs() -> list[str]:
    block = layout_tree().split("├── config/")[1].split("├── prompts/")[0]
    return [m.group(1) for m in re.finditer(r"^│   [├└]── ([a-z_]+)/", block, re.M)]


def invariants() -> list[str]:
    return re.findall(r"^\| (I\d+) \|", sections()["4"], re.M)


def invariant_phase_schedule() -> str:
    match = re.search(r"\*\*Invariants\*\* \(`tests/invariants/`\):(.*)", spec_text())
    assert match
    return match.group(1)


def adrs() -> dict[str, str]:
    """ADR number -> bold decision title from the §6 table."""
    return {
        m.group(1): m.group(2).strip().rstrip(".")
        for m in re.finditer(r"^\| (\d{4}) \| \*\*(.+?)\*\*", sections()["6"], re.M)
    }


def env_vars() -> list[str]:
    block = re.search(r"```\nAPP_ENV=.*?```", sections()["35"], re.S)
    assert block, "§35 env block not found"
    return re.findall(r"^([A-Z][A-Z0-9_]+)=", block.group(0), re.M)


def make_targets() -> list[str]:
    line = next(ln for ln in sections()["36"].splitlines() if "**Makefile targets**" in ln)
    return [t.split()[0] for t in re.findall(r"`([a-z][a-z0-9-]*(?: [A-Z]+=…)?)`", line)]
