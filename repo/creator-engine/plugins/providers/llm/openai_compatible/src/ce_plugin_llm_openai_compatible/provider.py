"""OpenAI-compatible `chat/completions` with `response_format: json_schema`.

Credentials and the model come from the environment through the caller's config; nothing is
hard-coded. HTTP 401/403/408/429/5xx and network errors are `LLMUnavailable`; a 2xx answer whose
content is not a JSON object is `LLMOutputError` (the repair loop shows it to the model).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from ce_contracts.models import LLMRequest, LLMResult
from ce_llm.provider import LLMError, LLMOutputError, LLMProvider, LLMUnavailable

__all__ = ["OpenAICompatibleProvider", "create", "parse_json_object"]

UNAVAILABLE = {401, 403, 408, 409, 429}


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model's text answer (code fences tolerated)."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"invalid JSON: {exc.msg}", text) from None
    if not isinstance(value, dict):
        raise LLMOutputError("the JSON answer is not an object", text)
    return value


def _secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter else value or "")


class OpenAICompatibleProvider(LLMProvider):
    key = "openai_compatible"

    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        self.model = str(config.get("model") or "")
        if not self.base_url or not self.model:
            raise ValueError("openai_compatible needs OPENAI_COMPAT_BASE_URL and LLM_MODEL")
        headers = {"content-type": "application/json"}
        api_key = _secret(config.get("api_key"))
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=float(config.get("timeout_s", 120)),
            transport=config.get("transport"),
        )

    async def complete(self, request: LLMRequest) -> LLMResult:
        body = {
            "model": self.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.stage or "output", "schema": request.json_schema, "strict": False},
            },
        }
        try:
            response = await self.client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"openai_compatible: {type(exc).__name__}") from None
        if response.status_code in UNAVAILABLE or response.status_code >= 500:
            raise LLMUnavailable(f"openai_compatible: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise LLMError(f"openai_compatible: HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMOutputError("no message content in the answer", json.dumps(data)[:2000]) from None
        usage = data.get("usage") or {}
        return LLMResult(
            output=parse_json_object(content),
            provider=self.key,
            model=str(data.get("model") or self.model),
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def create(config: dict[str, Any]) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(config)
