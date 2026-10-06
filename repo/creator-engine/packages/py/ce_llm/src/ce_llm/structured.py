"""One structured LLM call with a targeted repair loop (§13).

The output is parsed by the stage's Pydantic model and then checked by the caller (closed
vocabularies, references, offsets). Failures go back to the model as a short list of problems,
at most `max_repairs` times; the repair turn carries the previous answer and the problems as data
blocks (I10), replacing the previous repair turn so the conversation does not grow. If the output
still fails, `StructuredOutputError` carries the last parsed value (when one parsed), so the caller
can map unknown labels to the nearest vocabulary item and record the assumption, or give up.
Every attempt is kept for `director_runs`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ce_contracts.models import LLMMessage, LLMRequest
from pydantic import BaseModel, ValidationError

from ce_llm.data import wrap_data
from ce_llm.provider import LLMOutputError, LLMProvider

__all__ = ["Attempt", "StructuredOutputError", "StructuredResult", "structured"]

MAX_PROBLEMS = 12


@dataclass
class Attempt:
    output: dict[str, Any] | None
    raw: str | None
    problems: list[str]
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "raw": self.raw,
            "problems": self.problems,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
        }


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    attempts: list[Attempt]
    provider: str
    model: str

    @property
    def status(self) -> str:
        return "succeeded" if len(self.attempts) == 1 else "repaired"

    @property
    def tokens_in(self) -> int:
        return sum(a.tokens_in for a in self.attempts)

    @property
    def tokens_out(self) -> int:
        return sum(a.tokens_out for a in self.attempts)

    @property
    def latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)


@dataclass
class StructuredOutputError(Exception):
    stage: str
    attempts: list[Attempt]
    value: BaseModel | None = None
    provider: str = ""
    model: str = ""
    problems: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.stage}: output still invalid after {len(self.attempts)} attempts: {self.problems[:3]}"


def _validation_problems(exc: ValidationError) -> list[str]:
    out = []
    for error in exc.errors()[:MAX_PROBLEMS]:
        where = "/".join(str(p) for p in error.get("loc", ())) or "(root)"
        out.append(f"{where}: {error.get('msg', 'invalid')}")
    return out


def _repair_message(previous: str, problems: Sequence[str]) -> str:
    """The targeted repair turn. The previous answer and the problems (which quote it) are model
    output, so both are wrapped as data (I10); only the instruction is outside the blocks."""
    listed = "\n".join(f"- {p}" for p in problems[:MAX_PROBLEMS])
    return (
        "Your previous answer did not pass validation.\n"
        f"Previous answer:\n{wrap_data(previous[:20_000], kind='model_output')}\n"
        f"Problems:\n{wrap_data(listed, kind='validation_problems')}\n"
        "Return the complete corrected JSON object only, following the schema. Use only the allowed "
        "vocabulary values."
    )


async def structured[T: BaseModel](
    provider: LLMProvider,
    *,
    stage: str,
    output_type: type[T],
    messages: Sequence[LLMMessage],
    scenario_id: str | None = None,
    check: Callable[[T], list[str]] | None = None,
    max_repairs: int = 2,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> StructuredResult[T]:
    history = list(messages)
    schema = output_type.model_json_schema()
    attempts: list[Attempt] = []
    last_value: T | None = None
    problems: list[str] = []
    for _ in range(max_repairs + 1):
        request = LLMRequest(
            messages=history,
            json_schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            stage=stage,
            scenario_id=scenario_id,
        )
        started = time.monotonic()
        output: dict[str, Any] | None = None
        raw: str | None = None
        tokens_in = tokens_out = 0
        try:
            result = await provider.complete(request)
            output, tokens_in, tokens_out = result.output, result.tokens_in, result.tokens_out
        except LLMOutputError as exc:
            raw = exc.raw
            problems = [f"the answer is not a JSON object ({exc})"]
        latency = int((time.monotonic() - started) * 1000)
        if output is not None:
            try:
                value = output_type.model_validate(output)
            except ValidationError as exc:
                problems = _validation_problems(exc)
            else:
                last_value = value
                problems = list(check(value)) if check else []
                if not problems:
                    attempts.append(Attempt(output, None, [], tokens_in, tokens_out, latency))
                    return StructuredResult(value, attempts, provider.key, provider.model)
        attempts.append(Attempt(output, raw, problems, tokens_in, tokens_out, latency))
        shown = json.dumps(output, ensure_ascii=False) if output is not None else (raw or "")
        history = [*messages, LLMMessage(role="user", content=_repair_message(shown, problems))]
    raise StructuredOutputError(stage, attempts, last_value, provider.key, provider.model, problems)
