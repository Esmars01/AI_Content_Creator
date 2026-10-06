"""I9 — Honest UI (§4, §31): no behavior is shown as controllable or delivered unless coverage and
observation say so; a badge says "delivered" only for `*_CONFIRMED` outcomes.

The badge logic lives in the web app (`apps/web/src/lib/coverage.ts`); its Vitest contract test
enumerates every outcome from the generated JSON Schema. This test checks that the schema's
outcomes are the domain model's and runs the contract test, so `make test` covers I9.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from ce_core.enums import Outcome

pytestmark = [pytest.mark.invariant]

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "packages" / "ts" / "api-client" / "schema" / "behavior_coverage_report.schema.json"


def test_the_ui_contract_sees_every_domain_outcome() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$defs"]["Outcome"]["enum"] == [o.value for o in Outcome]
    confirmed = {o.value for o in Outcome if o.value.endswith("_CONFIRMED")}
    assert confirmed == {"HONORED_CONFIRMED", "APPROXIMATED_CONFIRMED"}


def test_coverage_badges_show_delivered_only_for_confirmed_outcomes() -> None:
    pnpm = shutil.which("pnpm")
    assert pnpm, "pnpm is required (make bootstrap installs the JS workspace)"
    result = subprocess.run(
        [pnpm, "--filter", "@ce/web", "run", "test:contract"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "19 passed" in result.stdout, result.stdout[-2000:]
