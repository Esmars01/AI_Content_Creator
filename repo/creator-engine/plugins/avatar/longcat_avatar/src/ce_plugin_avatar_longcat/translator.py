"""LongCat-Video-Avatar's translator: the abstract VisualPlan → one descriptive global prompt (§15.7).

The 1.5 README recommends long, descriptive prompts with a verbal-action cue. `text_prompt_global`
items (displayed emotion, posture, gesture style, camera awareness of the state covering the clip)
become phrases from the vocabulary descriptions; keyframe and prosody inheritance need no syntax;
everything else is reported with its reason (§37). Distillation fixes both guidance scales, so no
knob is translated. The only code that knows LongCat's prompt conventions (I1)."""

from __future__ import annotations

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.models import AvatarRequest
from ce_plugin_kit.translate import global_prompt_translation, own_manifest
from pydantic import BaseModel

__all__ = ["LongCatAvatarTranslator"]


class LongCatAvatarTranslator:
    version = "longcat_avatar_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = AvatarRequest.model_validate(base_request.model_dump())
        cue = str(own_manifest(__package__).defaults.get("verbal_cue", ""))
        base = " ".join(part.strip() for part in (cue, request.prompt) if part.strip())
        translation, prompt = global_prompt_translation(compiled, base=base, max_chars=900)
        engine = translation.engine({"syntax": "longcat_avatar_global_prompt", "prompt": prompt})
        return request.model_copy(update={"engine": engine})
