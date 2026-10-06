"""OpenAPI (§30): published at /openapi.json; every endpoint has typed request and response models,
request examples that validate, and problem+json error responses."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import ce_api.routers
import pytest
from ce_api.app import create_app
from pydantic import BaseModel

ROUTER_MODULES = sorted(m.name for m in pkgutil.iter_modules(ce_api.routers.__path__))
NO_BODY_RESPONSE = {"204"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app().openapi()


def operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(m.upper(), p, op) for p, ops in spec["paths"].items() for m, op in ops.items() if p.startswith("/v1/")]


def model_named(name: str) -> type[BaseModel] | None:
    for module_name in ROUTER_MODULES:
        module = importlib.import_module(f"ce_api.routers.{module_name}")
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def test_openapi_is_published_at_the_spec_path() -> None:
    app = create_app()
    assert app.openapi_url == "/openapi.json"
    assert app.openapi()["info"]["title"] == "Creator Engine API"


def test_every_operation_declares_typed_responses_and_problems(spec: dict[str, Any]) -> None:
    missing = []
    for method, path, op in operations(spec):
        responses = op["responses"]
        success = [s for s in responses if s.startswith("2")]
        for status in success:
            if status in NO_BODY_RESPONSE or path == "/v1/events" or (path.endswith("/videos") and method == "POST"):
                continue
            schema = responses[status].get("content", {}).get("application/json", {}).get("schema")
            if not schema:
                missing.append((method, path, status, "no response schema"))
        if "404" not in responses or "application/problem+json" not in responses["404"]["content"]:
            missing.append((method, path, "404", "no problem response"))
    assert missing == []


def test_every_request_body_has_a_valid_example(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    problems = []
    for method, path, op in operations(spec):
        body = op.get("requestBody")
        if body is None:
            continue
        schema = body["content"]["application/json"]["schema"]
        ref = schema.get("$ref") or next((s.get("$ref") for s in schema.get("anyOf", []) if "$ref" in s), None)
        assert ref, (method, path)
        name = ref.rsplit("/", 1)[1]
        examples = schemas[name].get("examples")
        if not examples:
            problems.append((method, path, name, "no example"))
            continue
        model = model_named(name)
        if model is None:
            problems.append((method, path, name, "model not found"))
            continue
        for example in examples:
            try:
                model.model_validate(example)
            except Exception as exc:
                problems.append((method, path, name, str(exc)[:200]))
    assert problems == []


def test_operation_ids_are_unique(spec: dict[str, Any]) -> None:
    ids = [op["operationId"] for _, _, op in operations(spec)]
    assert len(ids) == len(set(ids))
