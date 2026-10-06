"""Chatterbox translators: the abstract ProsodyPlan → Chatterbox's parameters (§21, §15.7).

Chatterbox has no emotion-label control (the model reads emotion from the text and the reference
voice). What it exposes:

- `exaggeration` (expressiveness) and `cfg_weight` (adherence; lower is more expressive and slower)
  on Chatterbox and Multilingual — the plan's energy maps onto them [RV: uncalibrated];
- inline paralinguistic tags (`[laugh]`, `[chuckle]`, `[cough]`) on Turbo, which ignores
  exaggeration and CFG;
- rate: a time-stretch within ±10% (§21), pauses: inserted silence (always deterministic).

Everything else is reported unsupported with its reason, never dropped (§37). The only code that
knows Chatterbox's parameter names (I1)."""

from __future__ import annotations

from typing import Any, ClassVar

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.models import TTSRequest
from ce_plugin_kit.speech import stretch_factor
from ce_plugin_kit.translate import Translation
from pydantic import BaseModel

__all__ = ["ChatterboxMultilingualTranslator", "ChatterboxTranslator", "ChatterboxTurboTranslator"]

PARAMETRIC = {"native_parametric", "native_segment"}


class ChatterboxTranslator:
    version = "chatterbox_v1"
    energy_params: ClassVar[bool] = True  # exaggeration / cfg_weight honored
    tag_vocabulary: ClassVar[dict[str, str]] = {}  # nonverbal tag → inline syntax
    exaggeration_range: ClassVar[tuple[float, float]] = (0.3, 0.9)
    cfg_range: ClassVar[tuple[float, float]] = (0.3, 0.5)

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = TTSRequest.model_validate(base_request.model_dump())
        plan = compiled.prosody[0] if compiled.prosody else None
        t = Translation(compiled)
        tempo, clamped = stretch_factor(plan.rate if plan else 1.0)
        tags: dict[str, str] = {}
        if plan is not None:
            for entry in plan.nonverbal:
                syntax = self.tag_vocabulary.get(str(entry.get("tag")))
                if syntax is not None and entry.get("after_word") is not None:
                    tags[str(int(entry["after_word"]))] = syntax
        for r in compiled.realizations:
            if r.dimension == "prosody_pause" and r.method in PARAMETRIC:
                t.encode(r, control="inserted_silence")
            elif r.dimension == "prosody_rate" and r.method in PARAMETRIC:
                t.encode(r, control="time_stretch", tempo=tempo, clamped=clamped)
            elif r.dimension == "prosody_energy" and r.method in PARAMETRIC and self.energy_params:
                t.encode(r, control="exaggeration_cfg")
            elif r.dimension == "nonverbal_audio" and r.method == "audio_nonverbal" and tags:
                t.encode(r, control="inline_tag")
            elif r.dimension == "nonverbal_audio" and r.method == "audio_nonverbal":
                t.report(r, "unsupported:no_tag_for_this_sound")
            elif r.method in PARAMETRIC or r.method.startswith("text_prompt"):
                t.report(r, "unsupported")
            else:
                t.report(r)
        engine: dict[str, Any] = {
            "syntax": "chatterbox_params",
            "tempo": tempo,
            "pauses_ms": {str(p["after_word"]): int(p["ms"]) for p in plan.pauses} if plan else {},
            "tags": tags,
            "calibrated": False,
        }
        if self.energy_params:
            energy = float(plan.energy) if plan else 0.5
            lo, hi = self.exaggeration_range
            engine["exaggeration"] = round(lo + energy * (hi - lo), 4)
            c_lo, c_hi = self.cfg_range
            engine["cfg_weight"] = round(max(c_lo, c_hi - (c_hi - c_lo) * max(0.0, energy - 0.6) / 0.4), 4)
        return request.model_copy(update={"engine": t.engine(engine)})


class ChatterboxTurboTranslator(ChatterboxTranslator):
    version = "chatterbox_turbo_v1"
    energy_params = False  # Turbo ignores exaggeration and CFG (tts_turbo.py warns and drops them)
    tag_vocabulary: ClassVar[dict[str, str]] = {"laugh": "[laugh]", "light_laugh": "[chuckle]", "chuckle": "[chuckle]"}


class ChatterboxMultilingualTranslator(ChatterboxTranslator):
    version = "chatterbox_mtl_v1"
