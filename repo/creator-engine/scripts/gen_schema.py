#!/usr/bin/env python3
"""Exports JSON Schemas from the Pydantic models (ADR 0015, §11).

Writes one `<name>.schema.json` per model plus `models.openapi.json` (all models as OpenAPI
components, the input for TypeScript generation) into packages/ts/api-client/schema/.
`--check` compares instead of writing and exits 1 when the committed schemas are stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ce_core.behavior.cbs import CanonicalBehaviorSpec
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_core.behavior.observed import ItemObservation, ObservedBehavior
from ce_core.behavior.plan_report import PlanReport
from ce_core.build import BuildManifest
from ce_core.edit.ops import EditOperations
from ce_core.edit.patch import SpecPatch
from ce_core.identity.creator import AppearanceDNA, CreatorDNA, VoiceDNA, WardrobeSpec
from ce_core.identity.memory import MemoryItem, MemorySnapshot
from ce_core.identity.world import WorldDNA
from ce_core.spec.videospec import VideoSpec
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packages" / "ts" / "api-client" / "schema"

MODELS: dict[str, type[BaseModel]] = {
    "videospec": VideoSpec,
    "cbs": CanonicalBehaviorSpec,
    "compiled_behavior": CompiledBehavior,
    "observed_behavior": ObservedBehavior,
    "item_observation": ItemObservation,
    "behavior_coverage_report": BehaviorCoverageReport,
    "plan_report": PlanReport,
    "build_manifest": BuildManifest,
    "creator_dna": CreatorDNA,
    "appearance_dna": AppearanceDNA,
    "voice_dna": VoiceDNA,
    "wardrobe_spec": WardrobeSpec,
    "world_dna": WorldDNA,
    "memory_item": MemoryItem,
    "memory_snapshot": MemorySnapshot,
    "edit_operations": EditOperations,
    "spec_patch": SpecPatch,
}


def render(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generate() -> dict[str, str]:
    files = {f"{name}.schema.json": render(model.model_json_schema(by_alias=True)) for name, model in MODELS.items()}
    _, combined = models_json_schema(
        [(model, "validation") for model in MODELS.values()], ref_template="#/components/schemas/{model}"
    )
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "Creator Engine models", "version": "1.0"},
        "paths": {},
        "components": {"schemas": combined.get("$defs", {})},
    }
    files["models.openapi.json"] = render(openapi)
    return files


def main() -> int:
    check = "--check" in sys.argv
    files = generate()
    stale = []
    for name, content in files.items():
        path = OUT / name
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if check and stale:
        print(f"stale schemas (run `make gen-schema`): {stale}")
        return 1
    print(f"{'checked' if check else 'wrote'} {len(files)} schema files in {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
