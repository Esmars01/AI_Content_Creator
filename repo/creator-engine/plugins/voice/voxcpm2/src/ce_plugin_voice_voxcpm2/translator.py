"""VoxCPM2 translator: the abstract ProsodyPlan → VoxCPM2's controls (§21).

VoxCPM2 takes a natural-language instruction in parentheses before the text, with or without a
reference voice (README "Controllable Voice Cloning": `"(slightly faster, cheerful tone)Text"`).
Emotion, energy and delivery items compiled to `text_prompt_segment` become that instruction,
written from the vocabulary descriptions the directives carry (never from raw labels when a
description exists). Rate is a ±10% time-stretch (deterministic; the instruction is not asked to
change the pace) and pauses are inserted silence. Everything else is reported with its reason.
The only code that knows VoxCPM2's instruction syntax (I1)."""

from __future__ import annotations

from typing import Any

from ce_contracts.behavior import BehaviorDirectives, ProsodyDirectives
from ce_contracts.models import TTSRequest
from ce_plugin_kit.speech import stretch_factor
from ce_plugin_kit.translate import Translation
from pydantic import BaseModel

__all__ = ["VoxCPM2Translator", "instruction_text"]

PARAMETRIC = {"native_parametric", "native_segment"}
TEXT = {"text_prompt_segment", "text_prompt_global"}
# dimension → the ProsodyDirectives description it is written from, in instruction order
INSTRUCTED = {"emotion_vocal": "emotion", "prosody_energy": "strategy", "delivery": "delivery"}
MAX_INSTRUCTION_CHARS = 160


def _fallback(plan: ProsodyDirectives, key: str) -> str | None:
    if key == "emotion" and plan.emotion:
        return plan.emotion.replace("_", " ") + " tone"
    if key == "strategy":
        return plan.strategy.replace("_", " ") + " delivery"
    if key == "delivery" and plan.delivery:
        return plan.delivery.replace("_", " ")
    return None


def instruction_text(plan: ProsodyDirectives, keys: list[str]) -> str:
    """The parenthesized instruction for the encoded dimensions: description phrases in a fixed
    order, lower-cased first letters, joined with commas, trimmed to a whole phrase under the cap."""
    phrases: list[str] = []
    for key in ("emotion", "strategy", "delivery"):
        if key not in keys:
            continue
        text = plan.descriptions.get(key) or _fallback(plan, key)
        if text:
            phrase = text.strip().rstrip(".")
            phrases.append(phrase[:1].lower() + phrase[1:])
    out = ""
    for phrase in phrases:
        candidate = f"{out}, {phrase}" if out else phrase
        if len(candidate) > MAX_INSTRUCTION_CHARS:
            break
        out = candidate
    return out


class VoxCPM2Translator:
    version = "voxcpm2_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = TTSRequest.model_validate(base_request.model_dump())
        plan = compiled.prosody[0] if compiled.prosody else None
        tempo, clamped = stretch_factor(plan.rate if plan else 1.0)
        t = Translation(compiled)
        keys: list[str] = []
        for r in compiled.realizations:
            if r.dimension == "prosody_pause" and r.method in PARAMETRIC:
                t.encode(r, control="inserted_silence")
            elif r.dimension == "prosody_rate" and r.method in PARAMETRIC:
                t.encode(r, control="time_stretch", tempo=tempo, clamped=clamped)
            elif r.dimension in INSTRUCTED and r.method in TEXT and plan is not None:
                t.encode(r, control="instruction")
                keys.append(INSTRUCTED[r.dimension])
            elif r.method in PARAMETRIC or r.method in TEXT or r.method == "audio_nonverbal":
                t.report(r, "unsupported")
            else:
                t.report(r)
        engine: dict[str, Any] = {
            "syntax": "voxcpm2_instruction",
            "instruction": instruction_text(plan, keys) if plan is not None else "",
            "tempo": tempo,
            "pauses_ms": {str(p["after_word"]): int(p["ms"]) for p in plan.pauses} if plan else {},
        }
        return request.model_copy(update={"engine": t.engine(engine)})
