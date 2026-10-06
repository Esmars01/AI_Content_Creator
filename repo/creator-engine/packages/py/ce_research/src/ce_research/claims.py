"""Claim checks that do not trust the model (Phase 12, §13 stage 6, §29 `claims`).

The fact-check stage asks the LLM for claims, verdicts and evidence ids. Model output is data
(I10): before a verdict reaches the claim ledger, code re-checks it against the cited evidence.

- **Support check** (`support`): a claim counts as supported by a piece of evidence only when every
  number in the claim appears in the evidence and enough of the claim's content words do
  (`min_term_overlap`). A model that cites unrelated evidence for a "supported" claim is downgraded.
- **Claim detection** (`detect_claims`): a deterministic detector for factual, checkable sentences
  (numbers, percentages, money, "studies show", "according to", superlative statistics). It runs
  even when the model reports no claims (or when planning without an LLM), so a statistic cannot
  slip through because the model missed it.
- **Closed book** (`enforce`): with `sources_policy = closed_book` the video may state only what the
  user's own sources support. Every detected or reported claim that the user-provided evidence
  does not support becomes `unsupported` and **blocks approval without an override** — no web
  evidence, no model knowledge, no "uncertain".

In open mode, an unsupported claim blocks approval but can be overridden with a reason (logged in
the ledger); an uncertain one warns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ce_research.retrieval import terms

Verdict = Literal["supported", "unsupported", "uncertain", "conflicting"]

__all__ = [
    "CheckedClaim",
    "ClaimEvidence",
    "Verdict",
    "detect_claims",
    "enforce",
    "numbers_in",
    "support",
]

_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,.]*(?:\s?%)?")
_CUE = re.compile(
    r"\b(percent|per cent|according to|studies|study|research shows|researchers|survey|statistic|data shows|"
    r"proven|on average|times (?:more|less|faster|slower)|million|billion|doubled|tripled|majority of)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def numbers_in(text: str) -> set[str]:
    """Numbers in a text, normalized (thousands separators and spaces removed; `%` kept)."""
    out = set()
    for match in _NUMBER.finditer(text):
        token = match.group(0).replace(",", "").replace(" ", "").rstrip(".")
        token = token.lstrip("+")
        if token and token not in ("-", "%"):
            out.add(token)
    return out


@dataclass(frozen=True)
class ClaimEvidence:
    id: str
    text: str
    trust: str  # user_provided | web


def support(claim: str, evidence: str, *, min_term_overlap: float = 0.5) -> float:
    """0..1: how well one piece of evidence supports the claim (0 when a number is missing)."""
    wanted = numbers_in(claim)
    have = numbers_in(evidence)
    if wanted and not {n.rstrip("%") for n in wanted} <= {n.rstrip("%") for n in have}:
        return 0.0
    claim_terms = set(terms(claim))
    if not claim_terms:
        return 1.0 if wanted else 0.0
    overlap = len(claim_terms & set(terms(evidence))) / len(claim_terms)
    return round(overlap, 4) if overlap >= min_term_overlap else 0.0


def detect_claims(segments: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """(segment key, sentence) for every factual-looking sentence of the script."""
    out: list[tuple[str, str]] = []
    for key, text in segments:
        for match in _SENTENCE.finditer(text):
            sentence = match.group(0).strip()
            if not sentence:
                continue
            if numbers_in(sentence) or _CUE.search(sentence):
                out.append((key, sentence))
    return out


@dataclass
class CheckedClaim:
    segment_key: str
    text: str
    verdict: Verdict
    evidence_ids: list[str]
    confidence: float
    blocking: bool
    overridable: bool
    reasons: list[str] = field(default_factory=list)
    detected: bool = False  # found by the detector, not reported by the model


def enforce(
    reported: Sequence[tuple[str, str, Verdict, Sequence[str]]],
    *,
    segments: Sequence[tuple[str, str]],
    evidence_of: Callable[[str], ClaimEvidence | None],
    search: Callable[[str], Sequence[ClaimEvidence]],
    closed_book: bool,
    min_support: float = 0.5,
) -> list[CheckedClaim]:
    """Re-checks the model's claims `(segment_key, text, verdict, evidence_ids)` and adds detected
    claims the model did not report. `evidence_of` looks an evidence id up (None: unknown id);
    `search` finds candidate evidence for a detected claim."""
    out: list[CheckedClaim] = []
    covered: list[tuple[str, set[str]]] = []
    for segment_key, text, verdict, ids in reported:
        known = [e for e in (evidence_of(i) for i in ids) if e is not None]
        usable = [e for e in known if e.trust == "user_provided"] if closed_book else known
        best = max((support(text, e.text, min_term_overlap=min_support) for e in usable), default=0.0)
        reasons: list[str] = []
        final: Verdict = verdict
        if verdict == "supported" and best <= 0.0:
            final = "unsupported" if closed_book else "uncertain"
            reasons.append(
                "the cited evidence does not contain the claim's numbers and terms"
                if usable
                else ("no user-provided evidence is cited (closed book)" if closed_book else "no evidence is cited")
            )
        if closed_book and final in ("uncertain", "conflicting"):
            reasons.append(f"closed book: a {final} claim is not allowed")
            final = "unsupported"
        out.append(
            _checked(
                segment_key,
                text,
                final,
                [e.id for e in usable if support(text, e.text) > 0] or [e.id for e in usable],
                best,
                closed_book,
                reasons,
            )
        )
        covered.append((segment_key, set(terms(text))))
    for segment_key, sentence in detect_claims(segments):
        sentence_terms = set(terms(sentence))
        if any(
            key == segment_key and sentence_terms and len(sentence_terms & seen) / len(sentence_terms) >= 0.6
            for key, seen in covered
        ):
            continue
        candidates = [e for e in search(sentence) if not closed_book or e.trust == "user_provided"]
        scored = sorted(
            ((support(sentence, e.text, min_term_overlap=min_support), e) for e in candidates),
            key=lambda p: (-p[0], p[1].id),
        )
        detected_verdict: Verdict
        if scored and scored[0][0] > 0:
            detected_verdict, ids, best = "supported", [e.id for s, e in scored if s > 0][:3], scored[0][0]
            reasons = ["detected claim supported by the sources"]
        else:
            detected_verdict = "unsupported" if closed_book else "uncertain"
            ids, best = [], 0.0
            reasons = ["detected factual claim without supporting evidence"]
        claim = _checked(segment_key, sentence, detected_verdict, ids, best, closed_book, reasons)
        claim.detected = True
        out.append(claim)
        covered.append((segment_key, sentence_terms))
    return out


def _checked(
    segment_key: str,
    text: str,
    verdict: Verdict,
    evidence_ids: list[str],
    best: float,
    closed_book: bool,
    reasons: list[str],
) -> CheckedClaim:
    blocking = verdict in ("unsupported", "conflicting")
    return CheckedClaim(
        segment_key=segment_key,
        text=text,
        verdict=verdict,
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        confidence=round(best, 4),
        blocking=blocking,
        overridable=blocking and not closed_book,
        reasons=reasons,
    )
