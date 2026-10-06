"""I1 schema lint: model-independent documents contain no engine names, engine parameters or
engine prompt syntax.

Covered: Creator DNA, memory items, World DNA, Director intent, the authored acting plan and the
CBS content — both their JSON Schemas (field names) and concrete documents (field names and
string values equal to an installed adapter id or model key, or a knob's engine parameter).
Engine parameters belong to translators and plugin manifests only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

__all__ = ["ENGINE_FIELD_NAMES", "engine_terms", "lint_document", "lint_schema"]

# Names that only make sense as engine parameters or engine prompt syntax.
ENGINE_FIELD_NAMES = frozenset(
    {
        "adapter",
        "adapter_id",
        "cfg",
        "cfg_scale",
        "checkpoint",
        "denoise",
        "denoising_strength",
        "engine",
        "engine_params",
        "guidance",
        "guidance_scale",
        "inference_steps",
        "lora",
        "lora_scale",
        "model",
        "model_id",
        "model_key",
        "negative",
        "negative_prompt",
        "num_inference_steps",
        "prompt",
        "prompt_extra",
        "sampler",
        "scheduler",
        "seed",
        "steps",
        "translator",
        "translator_version",
    }
)


def engine_terms(manifests: Iterable[Any]) -> tuple[frozenset[str], frozenset[str]]:
    """(engine parameter names, engine identifiers) from plugin manifests: knob parameters, the
    adapter ids and model keys of every installed engine plugin."""
    params: set[str] = set()
    names: set[str] = set()
    for manifest in manifests:
        if getattr(manifest, "kind", "") not in ("model_adapter", "analyzer"):
            continue
        names.add(str(manifest.id))
        names.update(str(m.key) for m in getattr(manifest, "models", []))
        params.update(str(k.param) for k in getattr(manifest, "knobs", {}).values())
    return frozenset(params), frozenset(names)


def _schema_fields(schema: Mapping[str, Any]) -> set[str]:
    fields: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for name in node.get("properties", {}) or {}:
                fields.add(str(name))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return fields


def lint_schema(model: type[BaseModel], extra_params: Iterable[str] = ()) -> list[str]:
    """Field names of `model` (recursively) that are engine parameters."""
    forbidden = ENGINE_FIELD_NAMES | set(extra_params)
    return sorted(_schema_fields(model.model_json_schema()) & forbidden)


def lint_document(document: Any, params: Iterable[str] = (), names: Iterable[str] = ()) -> list[str]:
    """Paths in a concrete document whose key is an engine parameter or whose string value is an
    engine identifier."""
    forbidden_keys = ENGINE_FIELD_NAMES | set(params)
    forbidden_values = set(names)
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key) in forbidden_keys:
                    problems.append(f"{path}/{key}")
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}/{i}")
        elif isinstance(node, str) and node in forbidden_values:
            problems.append(f"{path}={node}")

    walk(document, "")
    return problems
