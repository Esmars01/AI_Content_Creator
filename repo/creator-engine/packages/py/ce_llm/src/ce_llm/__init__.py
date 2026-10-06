"""LLM access for the Director (§13, §23): the `LLMProvider` interface and registry (providers are
plugins under `plugins/providers/llm`, selected by `LLM_PROVIDER`, not by the router), structured
calls with a targeted repair loop, versioned prompt templates and data wrapping (I10).

Status: implemented and tested in Phase 4 —
- `provider`: `LLMProvider`, errors, `create_llm_provider` (plugin registry), `scenario_key`;
- `structured`: one structured call validated by Pydantic plus caller checks, repaired up to N times;
- `prompts`: `prompts/<stage>/<version>.md` Jinja2 templates with recorded versions;
- `data`: untrusted text (memory, sources, uploads, model outputs) wrapped as delimited data.
"""

from ce_llm.data import DATA_RULE, unwrap_check, wrap_data
from ce_llm.prompts import PromptLibrary, RenderedPrompt, prompts_root
from ce_llm.provider import (
    FixtureMiss,
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMUnavailable,
    create_llm_provider,
    llm_provider_keys,
    provider_from_settings,
    scenario_key,
)
from ce_llm.structured import Attempt, StructuredOutputError, StructuredResult, structured

__all__ = [
    "DATA_RULE",
    "Attempt",
    "FixtureMiss",
    "LLMError",
    "LLMOutputError",
    "LLMProvider",
    "LLMUnavailable",
    "PromptLibrary",
    "RenderedPrompt",
    "StructuredOutputError",
    "StructuredResult",
    "create_llm_provider",
    "llm_provider_keys",
    "prompts_root",
    "provider_from_settings",
    "scenario_key",
    "structured",
    "unwrap_check",
    "wrap_data",
]

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 4
