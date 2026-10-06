"""Behavior translators of the mock avatar engines (§15.7).

They are the only code that knows the mock engines' "native syntax": a global prompt for
`mock_avatar_global`, a segment list for `mock_avatar_segment`. Every realization of the
directives ends up either in `engine.encoded` (the engine executes it: natively, by text, by
inheriting the keyframe, or by coupling to the audio) or in `engine.unsupported` with a reason
(UNSUPPORTED items and items another node realizes, such as editorial cutaways). Nothing is
dropped silently (§37 translator conformance).
"""

from __future__ import annotations

from typing import Any

from ce_contracts.behavior import BehaviorDirectives, DirectiveRealization, DirectiveSubSpan
from ce_contracts.models import AvatarRequest
from pydantic import BaseModel

__all__ = ["GlobalPromptTranslator", "SegmentTranslator", "parse_label"]

NATIVE = {"native_segment", "native_parametric"}
INHERITED = {"keyframe_conditioning": "keyframe", "prosody_transfer": "audio"}


def parse_label(value: str) -> tuple[str, float | None]:
    """`serious@0.6` → ("serious", 0.6); `look_away:down_left` → ("look_away:down_left", None)."""
    label, _, intensity = value.partition("@")
    try:
        return label, float(intensity) if intensity else None
    except ValueError:
        return label, None


def _spans(compiled: BehaviorDirectives) -> list[DirectiveSubSpan]:
    return [span for visual in compiled.visual for span in visual.sub_spans]


def _locate(compiled: BehaviorDirectives, item_ref: str) -> DirectiveSubSpan | None:
    for span in _spans(compiled):
        if item_ref in span.item_refs:
            return span
    return None


def _entry(realization: DirectiveRealization, span: DirectiveSubSpan, control: str) -> dict[str, Any]:
    label, intensity = parse_label(span.labels.get(realization.dimension, realization.dimension))
    return {
        "item_ref": realization.item_ref,
        "dimension": realization.dimension,
        "label": label,
        "intensity": intensity if intensity is not None else 0.5,
        "start_s": span.start_s,
        "end_s": span.end_s,
        "control": control,
        "method": realization.method,
    }


def _unsupported(realization: DirectiveRealization, reason: str) -> dict[str, str]:
    return {"item_ref": realization.item_ref, "dimension": realization.dimension, "reason": reason}


def _reason(realization: DirectiveRealization) -> str:
    if realization.method == "omit":
        return "unsupported"
    return f"realized_elsewhere:{realization.method}"


# The mock avatars' `motion_energy` knob (their manifests: `param: mock_motion_gain, range: [0.5, 1.5]`).
# Knob values reach directives only once `CalibrationWorkflow` marked the knob calibrated (§15.7),
# or in a calibration sweep, which sets them directly.
MOTION_GAIN_RANGE = (0.5, 1.5)


def _motion_gain(compiled: BehaviorDirectives) -> float | None:
    values = [s.knobs["motion_energy"] for s in _spans(compiled) if "motion_energy" in s.knobs]
    if not values:
        return None
    lo, hi = MOTION_GAIN_RANGE
    return round(lo + min(max(sum(values) / len(values), 0.0), 1.0) * (hi - lo), 4)


class GlobalPromptTranslator:
    """One prompt per clip with the items compiled to `text_prompt_global` (the displayed emotion,
    posture and camera awareness of a state that covers the clip)."""

    version = "mock_global_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = AvatarRequest.model_validate(base_request.model_dump())
        encoded: list[dict[str, Any]] = []
        unsupported: list[dict[str, str]] = []
        for realization in compiled.realizations:
            located = _locate(compiled, realization.item_ref)
            if realization.method == "text_prompt_global" and located is not None:
                encoded.append(_entry(realization, located, "text"))
            elif realization.method in INHERITED and located is not None:
                encoded.append(_entry(realization, located, INHERITED[realization.method]))
            else:
                unsupported.append(_unsupported(realization, _reason(realization)))
        # The prompt carries only what this engine executes by text; keyframe and audio-coupled
        # items reach it through its inputs, and unsupported items are not smuggled into the text.
        text = [e for e in encoded if e["control"] == "text"]
        emotion = next((e for e in text if e["dimension"] == "emotion_visual"), None)
        terms = [f"{emotion['label']} expression"] if emotion else []
        terms += [str(e["label"]) for e in text if e["dimension"] != "emotion_visual" and e["label"] not in terms]
        engine: dict[str, Any] = {
            "syntax": "mock_global_prompt",
            "prompt": ", ".join(p for p in [request.prompt, *terms] if p),
            "emotion": emotion["label"] if emotion else None,
            "intensity": emotion["intensity"] if emotion else None,
            "encoded": encoded,
            "unsupported": unsupported,
        }
        gain = _motion_gain(compiled)
        if gain is not None:
            engine["mock_motion_gain"] = gain
        return request.model_copy(update={"engine": engine})


class SegmentTranslator:
    """A segment list with emotion per segment and timed gaze, blink and expression events."""

    version = "mock_segment_v1"

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel:
        request = AvatarRequest.model_validate(base_request.model_dump())
        segments: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        encoded: list[dict[str, Any]] = []
        unsupported: list[dict[str, str]] = []
        for realization in compiled.realizations:
            span = _locate(compiled, realization.item_ref)
            if span is None or (realization.method not in NATIVE and realization.method != "text_prompt_segment"):
                if realization.method in INHERITED and span is not None:
                    encoded.append(_entry(realization, span, INHERITED[realization.method]))
                else:
                    unsupported.append(_unsupported(realization, _reason(realization)))
                continue
            control = "text" if realization.method == "text_prompt_segment" else "native"
            entry = _entry(realization, span, control)
            is_event = "/events[" in realization.item_ref or realization.method == "native_parametric"
            (events if is_event else segments).append(entry)
            encoded.append(entry)
        engine: dict[str, Any] = {
            "syntax": "mock_segments_v1",
            "segments": segments,
            "events": events,
            "encoded": encoded,
            "unsupported": unsupported,
        }
        gain = _motion_gain(compiled)
        if gain is not None:
            engine["mock_motion_gain"] = gain
        return request.model_copy(update={"engine": engine})
