"""JSON Schemas and TS model types are generated from Pydantic and must be committed fresh (ADR 0015)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_committed_json_schemas_are_fresh() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_schema.py", "--check"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ts_model_types_exist_for_the_core_schemas() -> None:
    models = (ROOT / "packages" / "ts" / "api-client" / "src" / "models.ts").read_text(encoding="utf-8")
    for name in (
        "VideoSpec:",
        "CanonicalBehaviorSpec:",
        "WorldDNA:",
        "CreatorDNA:",
        "MemoryItem:",
        "BehaviorCoverageReport:",
    ):
        assert name in models, f"{name} missing from models.ts; run `make gen-schema`"


def test_committed_openapi_document_is_fresh() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_openapi.py", "--check"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ts_api_types_cover_the_api() -> None:
    api = (ROOT / "packages" / "ts" / "api-client" / "src" / "api.ts").read_text(encoding="utf-8")
    for path in ('"/v1/auth/login"', '"/v1/creators/{creator_id}"', '"/v1/assets:initiate-upload"', '"/v1/events"'):
        assert path in api, f"{path} missing from api.ts; run `make gen-client`"
