"""The contract suite's backend for `captions_llm` (manifest `test_backend`): a deterministic
stand-in LLM that "translates" each given line by prefixing the target language. It exists so the
adapter's parsing, batching, checks and outputs run without a model; it is never routed."""

from __future__ import annotations

import json
import re
from typing import Any

from ce_contracts.models import LLMRequest, LLMResult
from ce_llm import LLMProvider

__all__ = ["EchoTranslator", "echo_backend"]

_BLOCK = re.compile(r"<<<DATA[^>]*>>>\n(.*?)\n<<<END DATA>>>", re.DOTALL)
_TARGET = re.compile(r"^Target language: (\S+)$", re.MULTILINE)


class EchoTranslator(LLMProvider):
    key = "echo_test_backend"
    model = "echo"

    async def complete(self, request: LLMRequest) -> LLMResult:
        user = next(m.content for m in request.messages if m.role == "user")
        lines = json.loads(_BLOCK.search(user).group(1))  # type: ignore[union-attr]
        target = _TARGET.search(user).group(1)  # type: ignore[union-attr]
        output = {"lines": [{"i": ln["i"], "text": f"[{target}] {ln['text']}"} for ln in lines]}
        return LLMResult(output=output, provider=self.key, model=self.model)


def echo_backend(manifest: Any) -> EchoTranslator:
    return EchoTranslator()
