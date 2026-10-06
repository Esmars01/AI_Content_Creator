"""Testimonial guard (§32; FTC 16 CFR 465): detection with rules plus an `llm.structured`
classification. Phase 4 builds detection; the review UI and quote-consent verification are not implemented.

In the modes listed in `config/policy/testimonials.yaml` (UGC, testimonial and product modes), a
segment in which the synthetic creator makes a first-person experiential claim about a product
("I've used this for a month") passes only when

- it carries a real customer quote with a consent reference (`quote_source`), or
- the mode is a dramatization mode and the spec has an on-screen `disclosure` effect;

otherwise the plan gets a blocking, non-overridable `policy` finding asking for a rewrite as
non-experiential, a dramatization with disclosure, or a consented quote. The guard reads the final
spec, so exact-script mode cannot bypass it.

Decision per segment: the classifier decides when it answered with at least `min_confidence`
(it can clear a rule hit such as "I've used this approach for years" about a method, and it can
catch claims the English rules miss). When it did not answer — no provider, the provider was
unavailable or its output failed validation — the rules decide alone, so the guard fails closed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from ce_config.schemas import TestimonialPolicy
from ce_core.behavior.plan_report import Finding
from ce_core.spec.common import Effect
from ce_core.spec.videospec import Segment, VideoSpec
from ce_llm import LLMProvider, LLMUnavailable, PromptLibrary, StructuredOutputError, StructuredResult, structured
from pydantic import BaseModel, Field

__all__ = [
    "GuardResult",
    "SegmentAssessment",
    "TestimonialClassification",
    "TestimonialGuard",
    "TestimonialItem",
    "has_disclosure",
    "rule_hits",
]

STAGE = "testimonial_guard"
DISCLOSURE_EFFECT = "disclosure"
ClassifierStatus = Literal["succeeded", "repaired", "unavailable", "failed", "skipped", "not_applicable"]


class TestimonialItem(BaseModel):
    segment_key: str
    experiential: bool = Field(description="a first-person claim of having used or experienced a product")
    confidence: float = Field(ge=0, le=1)
    claim: str = Field(default="", description="the words that make the claim, copied from the segment")
    reason: str = ""


class TestimonialClassification(BaseModel):
    items: list[TestimonialItem]


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    start: int
    end: int
    text: str


@dataclass
class SegmentAssessment:
    segment_key: str
    rule_hits: list[RuleHit]
    classifier: TestimonialItem | None
    flagged: bool
    decided_by: Literal["classifier", "rules"]
    resolution: Literal["quote_source", "dramatization_disclosure"] | None = None


@dataclass
class GuardResult:
    applies: bool
    classifier_status: ClassifierStatus
    assessments: list[SegmentAssessment] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    run: StructuredResult[TestimonialClassification] | None = None
    error: StructuredOutputError | LLMUnavailable | None = None

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


def rule_hits(policy: TestimonialPolicy, text: str) -> list[RuleHit]:
    hits = []
    for rule_id, pattern in policy.experiential_patterns.items():
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            hits.append(RuleHit(rule_id, match.start(), match.end(), match.group(0)))
    return hits


def has_disclosure(effects: Sequence[Effect]) -> bool:
    return any(e.type == DISCLOSURE_EFFECT and str(e.params.get("text", "")).strip() for e in effects)


class TestimonialGuard:
    __test__ = False  # not a pytest class despite the name

    def __init__(
        self,
        policy: TestimonialPolicy,
        *,
        provider: LLMProvider | None = None,
        prompts: PromptLibrary | None = None,
    ) -> None:
        self.policy = policy
        self.provider = provider
        self.prompts = prompts

    def applies(self, mode: str) -> bool:
        return mode in self.policy.applies_to_modes

    async def classify(
        self, segments: Sequence[Segment], *, scenario_id: str | None, products: Sequence[str] = ()
    ) -> tuple[StructuredResult[TestimonialClassification] | None, ClassifierStatus, Exception | None]:
        if self.provider is None or self.prompts is None:
            return None, "skipped", None
        keys = {s.key for s in segments}
        rendered = self.prompts.render(
            STAGE,
            segments=[{"key": s.key, "text": s.text} for s in segments],
            products=list(products),
        )

        def check(value: TestimonialClassification) -> list[str]:
            problems = [f"unknown segment_key {i.segment_key!r}" for i in value.items if i.segment_key not in keys]
            missing = sorted(keys - {i.segment_key for i in value.items})
            if missing:
                problems.append(f"classify every segment; missing {missing}")
            return problems

        try:
            result = await structured(
                self.provider,
                stage=STAGE,
                output_type=TestimonialClassification,
                messages=rendered.messages(),
                scenario_id=scenario_id,
                check=check,
                temperature=0.0,
            )
        except LLMUnavailable as exc:
            return None, "unavailable", exc
        except StructuredOutputError as exc:
            return None, "failed", exc
        return result, cast(ClassifierStatus, result.status), None

    async def check(
        self, spec: VideoSpec, *, scenario_id: str | None = None, products: Sequence[str] = ()
    ) -> GuardResult:
        mode = spec.meta.mode
        segments = list(spec.script.segments)
        if not self.applies(mode) or not segments:
            return GuardResult(applies=False, classifier_status="not_applicable")
        run, status, error = await self.classify(segments, scenario_id=scenario_id, products=products)
        by_key = {i.segment_key: i for i in run.value.items} if run is not None else {}
        result = GuardResult(applies=True, classifier_status=status, run=run)
        if isinstance(error, (StructuredOutputError, LLMUnavailable)):
            result.error = error
        dramatized = mode in self.policy.dramatization_modes and has_disclosure(spec.effects)
        floor = self.policy.classifier.min_confidence
        for segment in segments:
            hits = rule_hits(self.policy, segment.text)
            item = by_key.get(segment.key)
            if item is not None and item.confidence >= floor:
                flagged, decided_by = item.experiential, "classifier"
            else:
                flagged, decided_by = bool(hits), "rules"
            assessment = SegmentAssessment(segment.key, hits, item, flagged, decided_by)  # type: ignore[arg-type]
            result.assessments.append(assessment)
            if not flagged:
                continue
            ref = f"/script/segments[{segment.key}]/text"
            claim = (item.claim if item is not None and item.claim else None) or (hits[0].text if hits else "")
            detail = {
                "id": f"testimonial:{segment.key}",
                "check": "testimonial_guard",
                "segment_key": segment.key,
                "claim": claim,
                "rules": sorted({h.rule_id for h in hits}),
                "decided_by": decided_by,
                "classifier_confidence": item.confidence if item is not None else None,
                "overridable": False,
            }
            if segment.quote_source is not None and self.policy.require_consent_for_real_quotes:
                assessment.resolution = "quote_source"
                result.findings.append(
                    Finding(
                        kind="policy",
                        severity="info",
                        message=f"{segment.key}: experiential claim backed by a real customer quote "
                        "(consent reference present; consent is not verified automatically)",
                        refs=[ref],
                        detail={**detail, "resolution": "quote_source"},
                    )
                )
            elif dramatized:
                assessment.resolution = "dramatization_disclosure"
                result.findings.append(
                    Finding(
                        kind="policy",
                        severity="info",
                        message=f"{segment.key}: dramatized testimonial with an on-screen disclosure",
                        refs=[ref],
                        detail={**detail, "resolution": "dramatization_disclosure"},
                    )
                )
            else:
                result.findings.append(
                    Finding(
                        kind="policy",
                        severity="blocking",
                        message=f"{segment.key}: a synthetic creator claims first-hand product experience "
                        f"({claim!r}). Rewrite it as non-experiential, make the video a dramatization "
                        "with disclosure, or attach a consented customer quote.",
                        refs=[ref],
                        detail={
                            **detail,
                            "resolutions": ["rewrite_non_experiential", "dramatize_with_disclosure", "quote_source"],
                        },
                    )
                )
        return result
