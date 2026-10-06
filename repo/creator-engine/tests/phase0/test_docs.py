"""Phase 0 documentation deliverables: ADRs, decisions index, invariants, roadmap, TODO, progress."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
from ce_testing import docs

from tests.phase0 import spec_index as spec

ROOT = spec.ROOT
# Project ADRs beyond §6: deviations from the spec and phase implementation records (rule 10, rule 14).
EXTRA_ADRS = {f"{n:04d}" for n in range(28, 61)}


def adr_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(docs.require("adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        files[path.name[:4]] = path.name
    return files


def test_spec_is_internally_consistent() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_spec.py", str(docs.require_spec())],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 errors" in result.stdout


def test_every_spec_adr_has_a_file() -> None:
    files = adr_files()
    missing = sorted(set(spec.adrs()) - set(files))
    assert not missing, f"ADRs from §6 without a file: {missing}"
    assert set(files) == set(spec.adrs()) | EXTRA_ADRS, f"unexpected ADR files: {sorted(set(files))}"


@pytest.mark.parametrize("number", sorted(set(spec.adrs()) | EXTRA_ADRS))
def test_adr_has_required_sections(number: str) -> None:
    text = (docs.require("adr") / adr_files()[number]).read_text(encoding="utf-8")
    assert text.startswith(f"# ADR {number}: "), f"ADR {number}: title line must be '# ADR {number}: …'"
    for heading in ("## Status", "## Context", "## Decision", "## Alternatives", "## Consequences"):
        assert heading in text, f"ADR {number} lacks '{heading}'"
    if number in spec.adrs():
        assert "§6" in text, f"ADR {number} must cite §6 of the spec"


def test_decisions_index_links_every_adr() -> None:
    decisions = docs.require("DECISIONS.md").read_text(encoding="utf-8")
    for number, name in adr_files().items():
        assert f"adr/{name}" in decisions, f"DECISIONS.md does not link ADR {number}"
    assert "## Deviations from the spec" in decisions


def test_invariants_doc_lists_each_invariant_with_phase() -> None:
    doc = docs.require("INVARIANTS.md").read_text(encoding="utf-8")
    for inv in spec.invariants():
        row = re.search(rf"^\| {inv} \|(.+)$", doc, re.M)
        assert row, f"INVARIANTS.md lacks a row for {inv}"
        assert re.search(r"Phase \d+", row.group(1)), f"{inv}: no phase in INVARIANTS.md"


def test_roadmap_covers_all_phases_and_tracks() -> None:
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    spec_text = spec.spec_text()
    phases = re.findall(r"^### Phase (\d+) ", spec_text, re.M)
    assert "13" not in phases  # removed from the plan by the owner (docs/SPEC_ERRATA.md)
    for n in phases:
        assert re.search(rf"^#+ Phase {n}\b", roadmap, re.M), f"ROADMAP.md lacks Phase {n}"
    for heading in ("M1", "M2", "V1", "V2", "Experimental", "Promotion gates"):
        assert heading in roadmap, f"ROADMAP.md lacks {heading}"


def test_todo_breaks_down_phases_0_to_3() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    for n in range(4):
        assert re.search(rf"^## Phase {n}\b", todo, re.M), f"TODO.md lacks a Phase {n} section"
    assert "native fallback" in todo.lower()


def test_environment_variables_doc_covers_section_35() -> None:
    doc = docs.require("ENVIRONMENT_VARIABLES.md").read_text(encoding="utf-8")
    missing = [v for v in spec.env_vars() if f"`{v}`" not in doc]
    assert not missing, f"undocumented env vars: {missing}"


def test_environment_doc_records_inspection() -> None:
    doc = docs.require("ENVIRONMENT.md").read_text(encoding="utf-8")
    for item in (
        "OS",
        "CPU",
        "RAM",
        "Disk",
        "GPU",
        "Docker",
        "Python",
        "uv",
        "Node",
        "pnpm",
        "FFmpeg",
        "HarfBuzz",
        "FriBidi",
    ):
        assert item in doc, f"ENVIRONMENT.md lacks {item}"
    for host in ("PyPI", "npm", "Hugging Face", "GitHub", "Docker Hub"):
        assert host in doc, f"ENVIRONMENT.md lacks network reachability for {host}"


def test_readme_has_mock_mode_quick_start() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in ("make bootstrap", "make infra-up", "make test", "make dev"):
        assert command in readme
