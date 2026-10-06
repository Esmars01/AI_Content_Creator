"""The `LLMProvider` interface (§23 `llm.structured`) and its plugin registry.

Providers are `kind: provider` plugins with `provider.kind: llm` (`fixture`, `openai_compatible`,
`anthropic`); `LLM_PROVIDER` picks one. A provider returns the model's JSON output as a dict; text
that is not JSON raises `LLMOutputError` with the raw text, so the repair loop can show the model
what went wrong. `LLMUnavailable` (no key, network down, a fixture that does not exist) is not
repaired: the caller decides (the dev Director falls back to its template, production fails).
"""

from __future__ import annotations

import abc
import hashlib
import re
from typing import Any, ClassVar

from ce_contracts.common import HealthStatus
from ce_contracts.models import LLMRequest, LLMResult
from ce_contracts.plugins import discover

__all__ = [
    "FixtureMiss",
    "LLMError",
    "LLMOutputError",
    "LLMProvider",
    "LLMUnavailable",
    "create_llm_provider",
    "llm_provider_keys",
    "scenario_key",
]


class LLMError(Exception):
    """Base class of LLM provider errors."""


class LLMOutputError(LLMError):
    """The model answered with something that is not a JSON object."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class LLMUnavailable(LLMError):
    """The provider cannot answer at all (credentials, network, quota)."""


class FixtureMiss(LLMUnavailable):
    """The fixture provider has no recorded or authored response for (scenario, stage)."""


class LLMProvider(abc.ABC):
    """One configured LLM. `complete` returns the parsed JSON object of the answer."""

    key: ClassVar[str]
    model: str = ""

    @abc.abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResult: ...

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True)

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Releases network clients."""


_WS = re.compile(r"\s+")


def scenario_key(text: str) -> str:
    """A stable key for an input: fixtures are matched by scenario, never by prompt hash (§37)."""
    normalized = _WS.sub(" ", text).strip()
    return "in_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def llm_provider_keys(*, app_env: str | None) -> list[str]:
    return sorted(discover(app_env=app_env, include_mocks=True).providers("llm"))


def create_llm_provider(key: str, *, app_env: str | None, config: dict[str, Any] | None = None) -> LLMProvider:
    """Instantiates a registered provider plugin with its manifest defaults overlaid by `config`."""
    providers = discover(app_env=app_env, include_mocks=True).providers("llm")
    try:
        plugin = providers[key]
    except KeyError:
        raise ValueError(f"unknown LLM provider {key!r}; registered: {sorted(providers)}") from None
    factory = plugin.entrypoint()
    provider = factory({**plugin.manifest.defaults, **(config or {})})
    if not isinstance(provider, LLMProvider):
        raise TypeError(f"LLM provider {key!r} did not return an LLMProvider")
    return provider


def provider_from_settings(settings: Any) -> LLMProvider:
    """The provider `LLM_PROVIDER` selects, configured from the environment settings (§35). Secrets
    stay `SecretStr` until the plugin reads them; production refuses the fixture provider at startup."""

    def secret(value: Any) -> str | None:
        return value.get_secret_value() if value is not None and hasattr(value, "get_secret_value") else value

    key = str(settings.llm_provider)
    config: dict[str, Any] = {}
    if key == "anthropic":
        config = {"api_key": secret(settings.anthropic_api_key), "model": settings.llm_model}
    elif key == "openai_compatible":
        config = {
            "base_url": settings.openai_compat_base_url,
            "api_key": secret(settings.openai_compat_api_key),
            "model": settings.llm_model,
        }
    return create_llm_provider(key, app_env=settings.app_env, config={k: v for k, v in config.items() if v})
