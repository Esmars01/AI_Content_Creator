"""Anthropic Messages API with a forced tool call (`tool_choice: {type: tool}`): the tool's input
schema is the stage's JSON schema, so the answer arrives as a JSON object. System messages are
joined into the `system` field. Errors map as in the OpenAI-compatible provider."""

from __future__ import annotations

import json
from typing import Any

import httpx
from ce_contracts.models import LLMRequest, LLMResult
from ce_llm.provider import LLMError, LLMOutputError, LLMProvider, LLMUnavailable

__all__ = ["AnthropicProvider", "create"]

UNAVAILABLE = {401, 403, 408, 409, 429, 529}
TOOL = "emit_structured_output"


def _secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter else value or "")


class AnthropicProvider(LLMProvider):
    key = "anthropic"

    def __init__(self, config: dict[str, Any]) -> None:
        self.model = str(config.get("model") or "")
        api_key = _secret(config.get("api_key"))
        if not self.model or not api_key:
            raise ValueError("anthropic needs ANTHROPIC_API_KEY and LLM_MODEL")
        self.client = httpx.AsyncClient(
            base_url=str(config.get("base_url") or "https://api.anthropic.com").rstrip("/"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": str(config.get("api_version") or "2023-06-01"),
                "content-type": "application/json",
            },
            timeout=float(config.get("timeout_s", 120)),
            transport=config.get("transport"),
        )

    async def complete(self, request: LLMRequest) -> LLMResult:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns = [{"role": m.role, "content": m.content} for m in request.messages if m.role != "system"]
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": turns,
            "tools": [
                {
                    "name": TOOL,
                    "description": f"Return the {request.stage or 'requested'} output as structured data.",
                    "input_schema": request.json_schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL},
        }
        if system:
            body["system"] = system
        try:
            response = await self.client.post("/v1/messages", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"anthropic: {type(exc).__name__}") from None
        if response.status_code in UNAVAILABLE or response.status_code >= 500:
            raise LLMUnavailable(f"anthropic: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise LLMError(f"anthropic: HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        blocks = data.get("content") or []
        tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
        if tool is None or not isinstance(tool.get("input"), dict):
            text = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
            raise LLMOutputError("no structured tool output in the answer", text or json.dumps(data)[:2000])
        usage = data.get("usage") or {}
        return LLMResult(
            output=dict(tool["input"]),
            provider=self.key,
            model=str(data.get("model") or self.model),
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def create(config: dict[str, Any]) -> AnthropicProvider:
    return AnthropicProvider(config)
