#!/usr/bin/env python3
"""Renders docs/MODELS.md from the installed plugin manifests (§38: the model registry document).

The table is the manifests' own claims — status, validation, license closure, family, VRAM — so it
cannot drift from them; `--check` (run by the test suite) fails when the committed file is stale.
Evidence recorded later in the database (smoke runs, promotions) is in docs/GPU_VALIDATION.md and
`GET /v1/models`, not here.

Usage: uv run python scripts/gen_models_doc.py [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "MODELS.md"

HEADER = """# Models

The model registry as the installed plugin manifests declare it (§24, §39.7). **Generated** by
`scripts/gen_models_doc.py` from the manifests — edit the manifests, not this file. Recorded evidence
(smoke runs, benchmarks, promotions) lives in the database (`GET /v1/models`) and in
`docs/GPU_VALIDATION.md`. Licenses were read at the pinned revisions; `scripts/verify_licenses.py --online`
re-checks them (2026-10-04: 101 blocks, 89 checks re-verified online, 0 mismatches; the rest are flagged
metadata gaps).

Status values (§24): `production` routes normally; `sandbox` routes only in sandbox runs; `validation`
is `untested_on_gpu` until a real smoke run records `smoke_passed` (rule 5). Research-grade or
license-restricted engines say so in their notes.

"""


def _license_summary(manifest: Any) -> str:
    names: list[str] = []
    restricted = False
    for decl in manifest.models:
        blocks = [decl.license, *[d.license for d in decl.dependencies]]
        for lic in blocks:
            if lic.name not in names:
                names.append(lic.name)
            restricted = restricted or not lic.commercial_use or bool(lic.conditions)
    text = ", ".join(names)
    return text + (" ⚠" if restricted else "")


def _models(manifest: Any) -> str:
    out = []
    for decl in manifest.models:
        src = decl.source
        where = src.repo or src.uri or src.type
        rev = src.revision[:12] if len(src.revision) >= 40 else src.revision
        out.append(f"`{where}@{rev}`")
    return "<br>".join(out) or "—"


def render() -> str:
    from ce_contracts.plugins import discover

    registry = discover(app_env=None, include_mocks=False)
    rows = []
    for pid, plugin in sorted(registry.plugins.items(), key=lambda kv: (kv[1].manifest.runtime.family, kv[0])):
        m = plugin.manifest
        if m.kind == "provider":
            continue
        caps = ", ".join(c.id for c in m.capabilities)
        vram = f"{m.runtime.min_vram_gb:g}" if m.runtime.requires_gpu else "CPU"
        size = sum(d.size_gb for d in m.models)
        size_text = f"{size:g}" if size else "—"
        notes = (m.maturity_notes or "").replace("|", "\\|").replace("\n", " ")
        if len(notes) > 220:
            notes = notes[:217].rstrip() + "…"
        rows.append(
            f"| `{pid}` | {caps} | {m.runtime.family} | {vram} | {size_text} | {_models(m)} | {_license_summary(m)} | "
            f"{m.status} | {m.validation} | {notes} |"
        )
    table = (
        "| Adapter | Capabilities | Family | Min VRAM (GB) | Size (GB) | Model @ revision | License closure | Status | "
        "Validation | Notes |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"
    )
    legend = (
        "\n⚠ = a license in the closure is non-commercial or carries conditions (copyleft, attribution, use "
        "restrictions); the policy engine (`ce_policy.license`) decides per operator profile, and such engines "
        "stay out of production defaults (rule 15).\n\nMock adapters (`plugins/mock`, every capability) are not "
        "listed; they are documented in `plugins/mock/README.md`.\n"
    )
    return HEADER + table + legend


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = render()
    if not OUT.parent.is_dir():
        # the AI_Content_Creator workspace layout has no docs/ beside the sources (MODELS.md is in the phase bundles)
        print(f"{OUT.parent} not present: nothing to generate or check", file=sys.stderr)
        return 0
    if args.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print("docs/MODELS.md is stale: run scripts/gen_models_doc.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
