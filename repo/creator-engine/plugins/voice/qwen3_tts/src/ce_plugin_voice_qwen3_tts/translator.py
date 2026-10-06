"""Qwen3-TTS translator: the abstract ProsodyPlan → the Base (cloning) model's controls (§21).

The cloning model takes no instruction (the README's model table: instruction control only on the
VoiceDesign and CustomVoice models), so emotion, energy, pitch and emphasis follow the text and the
reference voice and are reported unsupported; rate is a ±10% time-stretch and pauses are inserted
silence. The only code that knows Qwen3-TTS's controls (I1)."""

from __future__ import annotations

from typing import Any

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.models import TTSRequest
from ce_plugin_kit.speech import stretch_factor
from ce_plugin_kit.translate import Translation
from pydantic import BaseModel

__all__ = ["Qwen3TTSTranslator"]

PARAMETRIC = {"native_parametric", "native_segment"}


class Qwen3TTSTranslator:
    version = "qwen3_tts_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = TTSRequest.model_validate(base_request.model_dump())
        plan = compiled.prosody[0] if compiled.prosody else None
        tempo, clamped = stretch_factor(plan.rate if plan else 1.0)
        t = Translation(compiled)
        for r in compiled.realizations:
            if r.dimension == "prosody_pause" and r.method in PARAMETRIC:
                t.encode(r, control="inserted_silence")
            elif r.dimension == "prosody_rate" and r.method in PARAMETRIC:
                t.encode(r, control="time_stretch", tempo=tempo, clamped=clamped)
            elif r.method in PARAMETRIC or r.method.startswith("text_prompt") or r.method == "audio_nonverbal":
                t.report(r, "unsupported")
            else:
                t.report(r)
        engine: dict[str, Any] = {
            "syntax": "qwen3_tts_clone",
            "tempo": tempo,
            "pauses_ms": {str(p["after_word"]): int(p["ms"]) for p in plan.pauses} if plan else {},
        }
        return request.model_copy(update={"engine": t.engine(engine)})
