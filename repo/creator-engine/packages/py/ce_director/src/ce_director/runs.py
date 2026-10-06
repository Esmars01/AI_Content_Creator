"""`director_runs` records (§13): one per stage with the template version, provider, model, input,
output, tokens, latency, cost and status. Code-only stages (context, research, intent policies,
validation) are recorded too, with provider `code`, so a plan's every decision is traceable."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ce_llm import Attempt, StructuredOutputError, StructuredResult

__all__ = ["RunLog", "StageRun"]

Status = Literal["succeeded", "repaired", "failed"]


@dataclass
class StageRun:
    stage: str
    template_version: str
    provider: str
    model: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    status: Status
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        """Column values for `director_runs` (attempts are kept inside `output`)."""
        output = None if self.output is None else {**self.output}
        if self.attempts:
            output = {"value": self.output, "attempts": self.attempts}
        return {
            "stage": self.stage,
            "template_version": self.template_version,
            "provider": self.provider,
            "model": self.model,
            "input": self.input,
            "output": output,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "status": self.status,
        }


def _attempts(attempts: list[Attempt]) -> list[dict[str, Any]]:
    return [a.as_dict() for a in attempts]


@dataclass
class RunLog:
    runs: list[StageRun] = field(default_factory=list)

    def code(self, stage: str, input: dict[str, Any], output: dict[str, Any]) -> None:
        self.runs.append(StageRun(stage, "code/v1", "code", "-", input, output, "succeeded"))

    def template(self, stage: str, input: dict[str, Any], output: dict[str, Any]) -> None:
        self.runs.append(StageRun(stage, "template/v1", "template", "-", input, output, "succeeded"))

    def llm(self, stage: str, template_version: str, input: dict[str, Any], result: StructuredResult[Any]) -> None:
        self.runs.append(
            StageRun(
                stage,
                template_version,
                result.provider,
                result.model,
                input,
                result.value.model_dump(mode="json"),
                "repaired" if result.status == "repaired" else "succeeded",
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                latency_ms=result.latency_ms,
                attempts=_attempts(result.attempts) if len(result.attempts) > 1 else [],
            )
        )

    def failed(self, stage: str, template_version: str, input: dict[str, Any], error: StructuredOutputError) -> None:
        self.runs.append(
            StageRun(
                stage,
                template_version,
                error.provider,
                error.model,
                input,
                error.value.model_dump(mode="json") if error.value is not None else None,
                "failed",
                tokens_in=sum(a.tokens_in for a in error.attempts),
                tokens_out=sum(a.tokens_out for a in error.attempts),
                latency_ms=sum(a.latency_ms for a in error.attempts),
                attempts=_attempts(error.attempts),
            )
        )
