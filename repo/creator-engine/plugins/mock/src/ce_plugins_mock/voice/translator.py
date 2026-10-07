"""Translator of the mock voice engine: the abstract ProsodyPlan → its native parameters (§21).

Realizations the engine executes are listed in `encoded`; everything else (non-verbal sounds,
deliveries and accents the mock cannot synthesize, items realized by another node) is listed in
`unsupported` with a reason (§37 translator conformance)."""

from __future__ import annotations

from typing import Any

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.models import TTSRequest
from pydantic import BaseModel

__all__ = ["MockVoiceTranslator"]

EXECUTES = {"native_parametric", "native_segment", "text_prompt_segment"}


class MockVoiceTranslator:
    version = "mock_voice_v2"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = TTSRequest.model_validate(base_request.model_dump())
        encoded = [
            {"item_ref": r.item_ref, "dimension": r.dimension, "method": r.method}
            for r in compiled.realizations
            if r.method in EXECUTES
        ]
        unsupported = [
            {
                "item_ref": r.item_ref,
                "dimension": r.dimension,
                "reason": "unsupported" if r.method == "omit" else f"realized_elsewhere:{r.method}",
            }
            for r in compiled.realizations
            if r.method not in EXECUTES
        ]
        plan = compiled.prosody[0] if compiled.prosody else None
        engine: dict[str, Any] = {
            "syntax": "mock_voice_params",
            "rate": plan.rate if plan else 1.0,
            "energy": plan.energy if plan else 0.6,
            "pitch_variation": plan.pitch_variation if plan else 0.5,
            "pitch_semitones": plan.pitch_semitones if plan else 0.0,
            "pauses_ms": {str(p["after_word"]): int(p["ms"]) for p in plan.pauses} if plan else {},
            "emphasis": list(plan.emphasis_words) if plan else [],
            "emotion": plan.emotion if plan else None,
            "encoded": encoded,
            "unsupported": unsupported,
        }
        return request.model_copy(update={"engine": engine})
