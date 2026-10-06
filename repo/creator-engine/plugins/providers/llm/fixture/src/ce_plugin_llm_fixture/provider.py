"""Fixture replay. A fixture file is one scenario:

```yaml
id: explain_ai_agents          # also accepted as `scenario_id`
kind: authored                 # authored | recorded
inputs: ["Create a 30-second TikTok explaining …"]   # matched through `ce_llm.scenario_key`
responses:
  interpret: {...}             # the stage's JSON output
  script:                      # or a list: one entry per attempt (repair tests)
    - raw: "not json {"
    - {...}
```

A response `{raw: "..."}` is returned as non-JSON text (`LLMOutputError`). A scenario or stage
without a response raises `FixtureMiss`; the dev Director then uses its labeled template.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from ce_contracts.models import LLMRequest, LLMResult
from ce_llm.provider import FixtureMiss, LLMOutputError, LLMProvider, scenario_key

__all__ = ["FixtureLLMProvider", "create", "default_fixtures_dir"]


def default_fixtures_dir() -> Path:
    """`LLM_FIXTURES_DIR`, else `eval/llm_fixtures` next to the config root (images and checkouts)."""
    env = os.environ.get("LLM_FIXTURES_DIR")
    if env:
        return Path(env)
    config_root = os.environ.get("CE_CONFIG_ROOT")
    candidates = [Path(config_root).parent / "eval" / "llm_fixtures"] if config_root else []
    candidates += [parent / "eval" / "llm_fixtures" for parent in Path(__file__).resolve().parents]
    candidates.append(Path.cwd() / "eval" / "llm_fixtures")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


class FixtureLLMProvider(LLMProvider):
    key = "fixture"

    def __init__(self, config: dict[str, Any]) -> None:
        self.root = Path(config["fixtures_dir"]) if config.get("fixtures_dir") else default_fixtures_dir()
        self.model = "fixture"
        self._index: dict[str, Path] | None = None
        self._calls: Counter[tuple[str, str]] = Counter()

    def _load_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for path in sorted(self.root.glob("*.yaml")) if self.root.is_dir() else []:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            index[str(doc.get("id") or path.stem)] = path
            for text in doc.get("inputs") or []:
                index[scenario_key(str(text))] = path
        return index

    def scenario(self, scenario_id: str | None) -> dict[str, Any]:
        if self._index is None:
            self._index = self._load_index()
        path = self._index.get(scenario_id or "")
        if path is None:
            raise FixtureMiss(f"no LLM fixture for scenario {scenario_id!r} in {self.root}")
        doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return doc

    def has_scenario(self, scenario_id: str | None) -> bool:
        try:
            self.scenario(scenario_id)
        except FixtureMiss:
            return False
        return True

    async def complete(self, request: LLMRequest) -> LLMResult:
        doc = self.scenario(request.scenario_id)
        responses = (doc.get("responses") or {}).get(request.stage)
        if responses is None:
            raise FixtureMiss(f"fixture {doc.get('id')!r} has no response for stage {request.stage!r}")
        attempts = responses if isinstance(responses, list) else [responses]
        call = self._calls[(str(doc.get("id")), request.stage)]
        self._calls[(str(doc.get("id")), request.stage)] += 1
        answer = attempts[min(call, len(attempts) - 1)]
        if isinstance(answer, dict) and set(answer) == {"raw"}:
            raise LLMOutputError("fixture returns non-JSON text", str(answer["raw"]))
        if not isinstance(answer, dict):
            raise LLMOutputError("fixture answer is not an object", str(answer))
        return LLMResult(output=answer, provider=self.key, model=f"fixture:{doc.get('kind', 'authored')}")


def create(config: dict[str, Any]) -> FixtureLLMProvider:
    return FixtureLLMProvider(config)
