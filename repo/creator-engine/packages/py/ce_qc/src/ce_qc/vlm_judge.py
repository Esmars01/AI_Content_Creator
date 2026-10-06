"""The VLM judge (§26): structured prompts with a JSON schema per check, batched per shot.

One request per take asks every check at once (eyes, teeth, hands, limbs, background warping) over
frames sampled at 2–4 fps; the answer is validated against the schema and every defect carries a
check, a severity, a time and a description. A defect counts only when the judge's confidence
reaches the tier's `vlm_judge.min_confidence`; critical defects fail the take when the tier says
`critical_defects_fail` (final), and warn otherwise (draft). Answers of a mock VLM are labelled
`mock`. Prompts are versioned (`JUDGE_VERSION`): a change re-judges cached takes because the
version enters the `qc.shot` parameters.

Full-resolution face and hand crops (from `face.landmarks` / `body.landmarks`) and the optional
cheaper triage tier are not wired yet: the judge sees the sampled frames of the take (D118).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CHECKS", "JUDGE_VERSION", "Judgement", "judge_question", "judge_schema", "parse_judgement"]

JUDGE_VERSION = "vlm-judge-v1"

CHECKS: dict[str, str] = {
    "eyes": "eyes: asymmetric or misaligned irises, missing or doubled pupils, unnatural blinking, gaze that "
    "jumps between frames",
    "teeth": "teeth and mouth: melted, merged or flickering teeth, a mouth interior that changes shape between "
    "frames, lips that tear",
    "hands": "hands: missing or extra fingers, fused fingers, impossible joints, hands that melt into objects",
    "limbs": "limbs and body: extra or missing limbs, impossible bends, a body that warps or stretches",
    "background_warping": "background: lines that bend or ripple near the person, objects that morph, "
    "flickering textures, a background that moves with the person",
}

SEVERITIES = ("critical", "minor")


def judge_schema(checks: Mapping[str, str] = CHECKS) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["defects"],
        "properties": {
            "defects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["check", "severity", "t_s", "description"],
                    "properties": {
                        "check": {"type": "string", "enum": sorted(checks)},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "t_s": {"type": "number", "minimum": 0},
                        "description": {"type": "string", "maxLength": 300},
                    },
                },
            },
            "notes": {"type": "string", "maxLength": 500},
        },
    }


def judge_question(checks: Mapping[str, str] = CHECKS) -> str:
    lines = "\n".join(f"- {text}" for _, text in sorted(checks.items()))
    return (
        "You are a strict video quality inspector for AI-generated footage of a person. Inspect the "
        "sampled frames for these defects only:\n"
        f"{lines}\n"
        "Report each defect you can see with the check it belongs to, `critical` when a viewer would "
        "notice it at normal speed and `minor` otherwise, the time in seconds where it is clearest, and a "
        "short description. Report nothing you cannot see; an empty list means no defects. Answer with "
        "JSON that matches the schema."
    )


@dataclass(frozen=True)
class Judgement:
    defects: list[dict[str, Any]] = field(default_factory=list)
    critical: bool = False
    confidence: float = 0.0
    counted: bool = False  # confidence reached the tier minimum
    valid: bool = True
    mock: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": JUDGE_VERSION,
            "defects": list(self.defects),
            "critical": self.critical,
            "confidence": self.confidence,
            "counted": self.counted,
            "valid": self.valid,
            "mock": self.mock,
            "reason": self.reason,
        }


def parse_judgement(answer: Mapping[str, Any], confidence: float, *, min_confidence: float) -> Judgement:
    """Validates the judge's answer; malformed items are dropped and an invalid answer never fails a
    take (it is reported as not judged)."""
    raw = answer.get("defects")
    mock = bool(answer.get("mock"))
    if not isinstance(raw, list):
        return Judgement(valid=False, confidence=confidence, mock=mock, reason="the answer has no `defects` list")
    defects: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        check, severity = item.get("check"), item.get("severity")
        if check not in CHECKS or severity not in SEVERITIES:
            continue
        try:
            t_s = max(0.0, float(item.get("t_s", 0.0)))
        except (TypeError, ValueError):
            t_s = 0.0
        defects.append(
            {
                "check": check,
                "severity": severity,
                "t_s": round(t_s, 3),
                "description": str(item.get("description", ""))[:300],
            }
        )
    counted = confidence >= min_confidence
    critical = counted and any(d["severity"] == "critical" for d in defects)
    reason = None if counted else f"judge confidence {confidence:.2f} < {min_confidence:.2f}: reported, not counted"
    return Judgement(defects, critical, confidence, counted, True, mock, reason)
