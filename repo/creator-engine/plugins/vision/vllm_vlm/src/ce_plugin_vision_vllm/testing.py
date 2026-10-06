"""CPU stand-in for the VLM (manifest `test_backend`). Never used by a worker: it returns the
schema's minimal valid instance (first enum value, empty strings and lists, 0.5 for numbers),
labelled `mock`-free so contract checks see the real shape, with confidence 0.1."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["FakeVLMBackend", "example_for", "fake_backend"]


def example_for(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object":
        props = schema.get("properties", {})
        required = schema.get("required", list(props))
        return {k: example_for(v) for k, v in props.items() if k in required}
    if kind == "array":
        return [example_for(schema["items"])] * int(schema.get("minItems", 0)) if "items" in schema else []
    return {"string": "", "number": 0.5, "integer": 0, "boolean": False, "null": None}.get(kind)


class FakeVLMBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def ask(self, kind: str, media: str, prompt: str, schema: dict[str, Any], seed: int) -> str:
        self.calls.append((kind, media, prompt))
        out = example_for(schema)
        out["confidence"] = 0.1
        return json.dumps(out)


def fake_backend(manifest: Any) -> FakeVLMBackend:
    return FakeVLMBackend()
