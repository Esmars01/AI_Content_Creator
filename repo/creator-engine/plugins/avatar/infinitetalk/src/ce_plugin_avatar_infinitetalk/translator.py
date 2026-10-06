"""InfiniteTalk's translator: the abstract VisualPlan → one global prompt and the audio CFG (§15.7).

InfiniteTalk takes a single text prompt per clip and infers expression and head motion from the
audio. So `text_prompt_global` items (the displayed emotion, posture and camera awareness of a state
that covers the clip) become prompt phrases built from the vocabulary descriptions; keyframe and
prosody inheritance need no syntax; the calibrated `motion_energy` knob maps onto
`audio_guide_scale` within the manifest's range; everything else (gaze, blink, gesture, timed
events) is reported unsupported with its reason, never dropped (§37). This is the only code that
knows InfiniteTalk's parameter names (I1).
"""

from __future__ import annotations

from typing import Any

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.models import AvatarRequest
from ce_plugin_kit.translate import global_prompt_translation, knob_mean, map_knob, own_manifest
from pydantic import BaseModel

__all__ = ["InfiniteTalkTranslator"]


class InfiniteTalkTranslator:
    version = "infinitetalk_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = AvatarRequest.model_validate(base_request.model_dump())
        manifest = own_manifest(__package__)
        cue = str(manifest.defaults.get("verbal_cue", ""))
        base = " ".join(part.strip() for part in (cue, request.prompt) if part.strip())
        translation, prompt = global_prompt_translation(compiled, base=base)
        engine: dict[str, Any] = {"syntax": "infinitetalk_global_prompt", "prompt": prompt, "audio_guide_scale": None}
        knob = manifest.knobs.get("motion_energy")
        energy = knob_mean(compiled, "motion_energy")
        # knob values reach the directives only after CalibrationWorkflow marked the knob calibrated (§15.7)
        if knob is not None and energy is not None:
            engine["audio_guide_scale"] = map_knob(energy, *knob.range)
        return request.model_copy(update={"engine": translation.engine(engine)})
