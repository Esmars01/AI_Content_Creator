"""The labeled, deterministic template Director (§37).

For free-form dev input that matches no fixture scenario — and for any single LLM stage whose
output still fails validation after the repairs — these functions produce the same structured
outputs the LLM stages do, from the input text and `config/director.yaml`: a sentence-split
script, scenes from the strategy pack's structure, per-purpose scene intent and one acting state
per scene. Plans built this way are marked `planner: template`; a single template stage inside an
LLM plan is recorded as an assumption.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ce_config.loader import ConfigBundle
from ce_config.schemas import Mode, StrategyPack
from ce_core.identity.creator import CreatorDNA
from ce_voice import sentence_spans

from ce_director.models import (
    ActingOut,
    Beat,
    BriefOut,
    EmotionOut,
    FactCheckOut,
    PlanRequest,
    SceneActingOut,
    SceneIntentOut,
    SceneOut,
    ScenesOut,
    ScriptLine,
    ScriptOut,
    SituationOut,
    StateOut,
    StrategiesOut,
    StrategyOut,
    VideoIntentOut,
    WordAt,
)

__all__ = [
    "distribute",
    "template_acting",
    "template_brief",
    "template_fact_check",
    "template_scenes",
    "template_script",
    "template_strategy",
]

_EXACT_HINT = re.compile(r"\b(exact|verbatim|my (own )?script|word for word|without changing)\b", re.IGNORECASE)


def template_brief(request: PlanRequest, bundle: ConfigBundle) -> BriefOut:
    config = bundle.director
    assert config is not None
    raw = request.input
    mode = request.mode if request.mode in bundle.modes else config.template.mode
    input_mode = request.input_mode if request.input_mode != "auto" else "idea"
    words = raw.split()
    title = " ".join(words[:8]).strip(" .,:;!?") or "Untitled"
    span = None
    if input_mode == "exact_script":
        stripped = raw.strip()
        start = raw.index(stripped) if stripped else 0
        span = {"start": start, "end": start + len(stripped)}
    assumptions = ["Planned by the labeled template Director (no LLM plan for this input)."]
    if request.input_mode == "auto" and _EXACT_HINT.search(raw):
        assumptions.append("The input may ask for an exact script; set input_mode=exact_script to keep it verbatim.")
    return BriefOut.model_validate(
        {
            "input_mode": input_mode,
            "title": title[:120],
            "mode": mode,
            "language": request.language or "en-US",
            "target_duration_s": request.target_duration_s,
            "platform_targets": request.platform_targets or ["tiktok"],
            "audience": "",
            "angle": "",
            "assumptions": assumptions,
            "sources_policy": request.sources_policy or "open",
            "script_span": span,
        }
    )


def template_strategy(brief: BriefOut, mode: Mode, packs: dict[str, StrategyPack], bundle: ConfigBundle) -> StrategyOut:
    config = bundle.director
    assert config is not None
    pack_id = next((p for p in mode.strategy_packs if p in packs), next(iter(sorted(packs))))
    pack = packs[pack_id]
    first = brief.title
    return StrategyOut(
        strategy_pack=pack_id,
        audience=brief.audience,
        angle=brief.angle,
        hooks=[first],
        selected_hook=0,
        beats=[Beat(purpose=p, summary="") for p in pack.structure],
        video_intent=VideoIntentOut.model_validate(config.template.video_intent),
    )


def template_script(brief: BriefOut, raw: str, beats: int) -> ScriptOut:
    """AI mode: the input's sentences as lines. Exact mode: sentence boundaries of the span."""
    if brief.input_mode == "exact_script" and brief.script_span is not None:
        spans = sentence_spans(raw, brief.script_span.start, brief.script_span.end)
        starts = [s for s, _ in spans[1:]]
        return ScriptOut(boundaries=starts, segment_beats=distribute(len(spans), beats))
    spans = sentence_spans(raw, 0, len(raw))
    lines = [raw[s:e] for s, e in spans] or [raw.strip()]
    assignment = distribute(len(lines), beats)
    return ScriptOut(lines=[ScriptLine(beat=b, text=t) for t, b in zip(lines, assignment, strict=True)])


def distribute(n_items: int, n_beats: int) -> list[int]:
    """Beat index of each of `n_items` consecutive items: first item → first beat, last → last."""
    if n_items <= 0:
        return []
    if n_beats <= 1 or n_items == 1:
        return [0] * n_items
    if n_items <= n_beats:
        middle = list(range(1, n_beats - 1))[: max(0, n_items - 2)]
        return [0, *middle, n_beats - 1] if n_items >= 2 else [0]
    return [min(n_beats - 1, (i * n_beats) // n_items) for i in range(n_items)]


def template_fact_check() -> FactCheckOut:
    return FactCheckOut()


def template_scenes(segment_beats: Sequence[tuple[str, int]], beats: Sequence[Beat], bundle: ConfigBundle) -> ScenesOut:
    """One scene per beat that received segments, with the purpose's default intent."""
    config = bundle.director
    assert config is not None
    scenes: list[SceneOut] = []
    current: list[str] = []
    current_beat: int | None = None
    for key, beat in segment_beats:
        if current_beat is not None and beat != current_beat:
            scenes.append(_scene(current, beats[current_beat].purpose, bundle))
            current = []
        current.append(key)
        current_beat = beat
    if current and current_beat is not None:
        scenes.append(_scene(current, beats[current_beat].purpose, bundle))
    return ScenesOut(scenes=scenes)


def _scene(keys: list[str], purpose: str, bundle: ConfigBundle) -> SceneOut:
    config = bundle.director
    assert config is not None
    d = config.template.purposes[purpose]
    return SceneOut(
        segment_keys=keys,
        purpose=purpose,
        intent=SceneIntentOut(
            narrative_goal=d.narrative_goal,
            emotional_goal=d.emotional_goal,
            audience_effect=d.audience_effect,
            information_goal=d.information_goal,
            attention_goal=d.attention_goal,
        ),
    )


def template_acting(
    scenes: Sequence[tuple[str, str, WordAt]],
    *,
    bundle: ConfigBundle,
    dna: CreatorDNA,
    posture: dict[str, str],
) -> ActingOut:
    """One state per scene from the purpose defaults, clamped into the creator's DNA range.
    `scenes` = (scene key, purpose, first word)."""
    config = bundle.director
    assert config is not None
    out: list[SceneActingOut] = []
    for scene_key, purpose, first in scenes:
        d = config.template.purposes[purpose]
        lo, hi = dna.behavior.emotion_ranges.get(d.emotion, (0.0, 1.0))
        intensity = min(max(d.intensity, lo), hi)
        emotion = EmotionOut(label=d.emotion, intensity=intensity)
        out.append(
            SceneActingOut(
                scene_key=scene_key,
                situation=SituationOut(
                    kind=config.template.situation_kind, audience_stance=config.template.audience_stance
                ),
                states=[
                    StateOut(
                        start=first,
                        internal_state=d.internal_state,
                        social_goal=d.social_goal,
                        audience_goal=d.audience_goal,
                        performance_intent=d.performance_intent,
                        felt=emotion,
                        displayed=emotion,
                        strategies=StrategiesOut(
                            prosody=d.prosody,
                            gaze=d.gaze,
                            gesture=d.gesture,
                            posture=posture[scene_key],
                            reaction=d.reaction,
                            camera_awareness=d.camera_awareness,
                        ),
                    )
                ],
            )
        )
    return ActingOut(scenes=out)
