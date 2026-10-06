"""Recording real-provider answers as fixtures (§37: fixtures are `recorded` or `authored`).

`RecordingProvider` wraps a real provider; every answer is appended to
`<fixtures_dir>/<scenario>.yaml` under `responses.<stage>` (`kind: recorded`), so a later fixture
run replays it. Recording is opt-in (CI with a key, or a developer); it never runs in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from ce_contracts.models import LLMRequest, LLMResult

from ce_llm.provider import LLMProvider

__all__ = ["RecordingProvider"]


class RecordingProvider(LLMProvider):
    key = "recording"

    def __init__(self, inner: LLMProvider, fixtures_dir: Path, *, inputs: dict[str, str] | None = None) -> None:
        self.inner = inner
        self.root = Path(fixtures_dir)
        self.model = inner.model
        self.inputs = dict(inputs or {})  # scenario id → raw input text

    async def complete(self, request: LLMRequest) -> LLMResult:
        result = await self.inner.complete(request)
        scenario = request.scenario_id or "unnamed"
        path = self.root / f"{scenario}.yaml"
        self.root.mkdir(parents=True, exist_ok=True)
        doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        doc = doc or {"id": scenario, "kind": "recorded", "inputs": [], "responses": {}}
        doc["kind"] = "recorded"
        doc["recorded_with"] = {"provider": self.inner.key, "model": result.model}
        if scenario in self.inputs and self.inputs[scenario] not in doc.setdefault("inputs", []):
            doc["inputs"].append(self.inputs[scenario])
        doc.setdefault("responses", {})[request.stage] = result.output
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()
