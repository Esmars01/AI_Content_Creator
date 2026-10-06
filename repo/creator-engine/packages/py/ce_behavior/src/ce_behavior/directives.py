"""CompiledBehavior → `BehaviorDirectives`, the wire form a `BehaviorTranslator` receives (§15.7,
ADR 0032): word anchors become seconds relative to the request's audio; every realization travels
with its level so translators report what they cannot express instead of dropping it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ce_contracts.behavior import (
    BehaviorDirectives,
    DirectiveRealization,
    DirectiveSubSpan,
    EditorialDirective,
    ProsodyDirectives,
    VisualDirectives,
)
from ce_core.behavior.cbs import CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.canonical import content_digest

from ce_behavior.scene import SceneWords

__all__ = ["EDITORIAL_METHODS", "describe_label", "generation_digest", "item_labels", "to_directives"]

Word = tuple[str, int]

# Realized by editing, structure or post from spec elements — never by the generation engine.
EDITORIAL_METHODS = frozenset(
    {"editorial_cutaway", "editorial_punch", "caption_emphasis", "music_cue", "sfx_cue", "shot_split", "omit"}
)


def _fmt(value: float) -> str:
    return f"{value:g}"


def item_labels(cbs: CBSContent) -> dict[tuple[str, str], str]:
    """Vocabulary label per (item_ref, dimension): `serious@0.6`, `glance_away_and_return`,
    `look_away:down_left@0.5`. Translators turn labels into engine syntax; the labels themselves
    are model-independent vocabulary (I1)."""
    out: dict[tuple[str, str], str] = {}
    scene_prefix = next(
        (c.item_ref.split("/acting/", 1)[0] for c in cbs.requested_controls if "/acting/" in c.item_ref), ""
    )
    for state in cbs.trajectory:
        base = f"{scene_prefix}/acting/states[{state.key}]"
        displayed = state.emotion.displayed
        out[(f"{base}/emotion", "emotion_visual")] = f"{displayed.label}@{_fmt(displayed.intensity)}"
        out[(f"{base}/emotion", "emotion_vocal")] = f"{displayed.label}@{_fmt(displayed.intensity)}"
        for channel in ("prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness"):
            token = str(getattr(state.strategies, channel))
            for control in cbs.requested_controls:
                if control.item_ref == f"{base}/strategies/{channel}":
                    out[(control.item_ref, control.dimension)] = token
    for event in cbs.events:
        label = str(event.type) + (f":{event.direction}" if event.direction else "")
        if event.intensity is not None:
            label += f"@{_fmt(event.intensity)}"
        out[(f"{scene_prefix}/acting/events[{event.key}]", str(event.dimension))] = label
    return out


# Which descriptions.yaml category describes a dimension's labels (§15.2). Events are described by
# their `event_type` (plus `direction`) whatever their dimension.
_DESCRIPTION_CATEGORY = {
    "emotion_visual": "emotion",
    "emotion_vocal": "emotion",
    "facial_expression": "emotion",
    "gaze": "strategy.gaze",
    "gesture": "strategy.gesture",
    "posture": "strategy.posture",
    "reaction": "strategy.reaction",
    "camera_awareness": "strategy.camera_awareness",
    "prosody_rate": "strategy.prosody",
    "prosody_pitch": "strategy.prosody",
    "prosody_energy": "strategy.prosody",
    "prosody_emphasis": "strategy.prosody",
}


def _text(vocab: Any, category: str, token: str, *, vocal: bool = False) -> str | None:
    found = vocab.describe(category, token) if vocab is not None else None
    if found is None:
        return None
    if vocal and getattr(found, "vocal", None):
        return str(found.vocal)
    return str(getattr(found, "text", found))


def describe_label(vocab: Any, dimension: str, label: str, *, event: bool = False) -> str | None:
    """The model-agnostic description of a resolved label (`serious@0.6`, `look_away:down_left@0.5`)
    from config/vocab/descriptions.yaml, or None. Text-prompted translators build their prompts
    from these, so prompt wording lives in the vocabulary, not in plugins (§15.2, I1)."""
    token, _, _ = label.partition("@")
    if event:
        kind, _, direction = token.partition(":")
        text = _text(vocab, "event_type", kind)
        if text and direction:
            where = _text(vocab, "direction", direction)
            text = f"{text} ({where})" if where else text
        return text
    category = _DESCRIPTION_CATEGORY.get(dimension)
    return _text(vocab, category, token, vocal=dimension == "emotion_vocal") if category else None


def to_directives(
    compiled: CompiledBehavior,
    cbs: CBSContent,
    *,
    words: SceneWords,
    word_times: Mapping[Word, tuple[float, float]],
    window: tuple[float, float] = (0.0, float("inf")),
    vocab: Any = None,
) -> BehaviorDirectives:
    """`word_times` are relative to the request's audio; sub-spans are clipped to `window` and
    expressed relative to its start (a chunk of a long shot). With `vocab`, every label carries its
    model-agnostic description for text-prompted translators."""
    w0, w1 = window
    labels = item_labels(cbs)
    durations = {
        c.item_ref: c.duration_ms
        for c in cbs.requested_controls
        if c.duration_ms is not None and "/events[" in c.item_ref
    }
    dims = {(r.item_ref, r.dimension) for r in compiled.realizations if str(r.method) != "omit"}

    def rel(t: float) -> float:
        return round(min(max(t - w0, 0.0), max(w1 - w0, 0.0)), 4)

    visual: list[VisualDirectives] = []
    for plan in compiled.visual_plans:
        spans: list[DirectiveSubSpan] = []
        for sub in plan.sub_spans:
            first = (sub.span.start.segment_key, sub.span.start.word)
            last = (sub.span.end.segment_key, sub.span.end.word)
            if first not in word_times or last not in word_times:
                continue
            start = word_times[first][0]
            end = word_times[last][1]
            event_refs = [r for r in sub.item_refs if "/events[" in r]
            if event_refs and durations.get(event_refs[0]):
                end = start + float(durations[event_refs[0]] or 0) / 1000.0
            span_labels = {
                dim: labels[(ref, dim)] for (ref, dim) in sorted(dims) if ref in sub.item_refs and (ref, dim) in labels
            }
            span_descriptions: dict[str, str] = {}
            if vocab is not None:
                for ref, dim in sorted(dims):
                    if ref in sub.item_refs and (ref, dim) in labels:
                        text = describe_label(vocab, dim, labels[(ref, dim)], event="/events[" in ref)
                        if text:
                            span_descriptions[dim] = text
            spans.append(
                DirectiveSubSpan(
                    start_s=rel(start),
                    end_s=rel(end),
                    descriptors=list(sub.descriptors),
                    knobs={k: float(v) for k, v in sub.knobs.model_dump(exclude_none=True).items()},
                    item_refs=list(sub.item_refs),
                    labels=span_labels,
                    descriptions=span_descriptions,
                )
            )
        visual.append(
            VisualDirectives(
                shot_key=plan.shot_key, chunk=plan.chunk, character_key=plan.character_key, sub_spans=spans
            )
        )
    editorial: list[EditorialDirective] = []
    for action in compiled.editorial_actions:
        at_s: float | None = None
        until_s: float | None = None
        if action.span is not None:
            a = (action.span.start.segment_key, action.span.start.word)
            b = (action.span.end.segment_key, action.span.end.word)
            if a in word_times and b in word_times:
                at_s, until_s = rel(word_times[a][0]), rel(word_times[b][1])
        elif action.at is not None:
            a = (action.at.segment_key, action.at.word)
            if a in word_times:
                at_s = rel(word_times[a][0])
        editorial.append(
            EditorialDirective(
                kind=action.kind, item_ref=action.item_ref, start_s=at_s, end_s=until_s, detail=action.detail
            )
        )
    return BehaviorDirectives(
        cbs_content_digest=compiled.cbs_content_digest,
        route_digest=compiled.route_digest,
        target_key=compiled.target_key,
        realizations=[
            DirectiveRealization(
                item_ref=r.item_ref,
                dimension=r.dimension,
                level=str(r.level),  # type: ignore[arg-type]
                method=str(r.method),
                detail=r.detail,
            )
            for r in compiled.realizations
        ],
        prosody=[
            ProsodyDirectives(
                character_key=p.character_key,
                segment_key=p.segment_key,
                strategy=p.strategy,
                emotion=p.emotion,
                emotion_intensity=p.emotion_intensity,
                rate=p.rate,
                energy=p.energy,
                pitch_variation=p.pitch_variation,
                emphasis_words=list(p.emphasis_words),
                pauses=[{"after_word": x.after_word, "ms": x.ms} for x in p.pauses],
                nonverbal=list(p.nonverbal),
                delivery=p.delivery,
                descriptions=_prosody_descriptions(vocab, p.emotion, p.strategy, p.delivery),
            )
            for p in compiled.prosody_plans
        ],
        visual=visual,
        editorial=editorial,
    )


def _prosody_descriptions(vocab: Any, emotion: str | None, strategy: str, delivery: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if vocab is None:
        return out
    for key, category, token in (
        ("emotion", "emotion", emotion),
        ("strategy", "strategy.prosody", strategy),
        ("delivery", "annotation_tag.delivery", delivery),
    ):
        text = _text(vocab, category, token, vocal=True) if token else None
        if text:
            out[key] = text
    return out


def generation_digest(compiled: CompiledBehavior, cbs: CBSContent) -> str:
    """The digest of what a generation engine reads from a compile node's output (§12.2): the plans,
    the realizations an engine executes with their labels and durations, the target and route.
    It leaves out the CBS digest, items the engine cannot show (`omit`) and editorial realizations
    (post and the timeline execute those from spec elements), so an edit that changes none of these
    keeps every generation node cached (§12.9 `no_visible_effect`)."""
    labels = item_labels(cbs)
    durations = {
        c.item_ref: c.duration_ms
        for c in cbs.requested_controls
        if c.duration_ms is not None and "/events[" in c.item_ref
    }
    executed = sorted(
        (r.item_ref, r.dimension, str(r.level), str(r.method), r.detail)
        for r in compiled.realizations
        if str(r.method) not in EDITORIAL_METHODS
    )
    span_refs = {ref for plan in compiled.visual_plans for sub in plan.sub_spans for ref in sub.item_refs}
    # labels travel with executed realizations, and with any non-omitted item a visual sub-span covers
    labelled = {(ref, dim) for ref, dim, *_ in executed} | {
        (r.item_ref, r.dimension) for r in compiled.realizations if str(r.method) != "omit" and r.item_ref in span_refs
    }
    refs = {ref for ref, _ in labelled}
    return content_digest(
        {
            "target_key": compiled.target_key,
            "route_digest": compiled.route_digest,
            "realizations": [list(r) for r in executed],
            "labels": {f"{ref}|{dim}": labels[(ref, dim)] for ref, dim in sorted(labelled) if (ref, dim) in labels},
            "durations": {ref: durations[ref] for ref in sorted(refs) if ref in durations},
            "prosody": [p.model_dump(mode="json") for p in compiled.prosody_plans],
            "visual": [v.model_dump(mode="json") for v in compiled.visual_plans],
        }
    )
