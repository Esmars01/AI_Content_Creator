"""Kokoro's translator: the abstract ProsodyPlan → speed and inserted silences (§21, §15.7).

Kokoro has one prosodic control, the speaking speed; pauses are rendered deterministically as
silence between separately synthesized chunks. Emotion, pitch, energy, emphasis, non-verbal sounds,
delivery and accent are reported `unsupported` (never dropped silently, §37)."""

from __future__ import annotations

from typing import Any

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.models import TTSRequest
from pydantic import BaseModel

__all__ = ["EXECUTES", "KokoroTranslator"]

EXECUTES = {("prosody_rate", "native_parametric"), ("prosody_pause", "native_parametric")}


class KokoroTranslator:
    version = "kokoro_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = TTSRequest.model_validate(base_request.model_dump())
        encoded = [
            {"item_ref": r.item_ref, "dimension": r.dimension, "method": r.method}
            for r in compiled.realizations
            if (r.dimension, r.method) in EXECUTES
        ]
        unsupported = [
            {
                "item_ref": r.item_ref,
                "dimension": r.dimension,
                "reason": "unsupported"
                if r.method in ("omit", "native_parametric", "native_segment", "text_prompt_segment")
                else f"realized_elsewhere:{r.method}",
            }
            for r in compiled.realizations
            if (r.dimension, r.method) not in EXECUTES
        ]
        plan = compiled.prosody[0] if compiled.prosody else None
        engine: dict[str, Any] = {
            "syntax": "kokoro_speed_and_silence",
            "rate": float(plan.rate) if plan else 1.0,
            "pauses_ms": {str(p["after_word"]): int(p["ms"]) for p in plan.pauses} if plan else {},
            "encoded": encoded,
            "unsupported": unsupported,
        }
        return request.model_copy(update={"engine": engine})
