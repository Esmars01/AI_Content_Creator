"""The behavior compiler: `compile(cbs, target) -> CompiledBehavior` (§15.7, ADR 0023).

The compiler is engine-agnostic. For every requested control in the target's scope it walks the
dimension's method preference order (`config/vocab/behavior_dimensions.yaml`) and picks the first
method the target can execute:

- `native_parametric` / `native_segment`: the routed engine declares that control for the
  dimension at the needed temporal precision (HONORED; APPROXIMATED with a warning when the
  measured success rate of that control is below `qc/behavior.yaml` `unreliable_control_success_rate`);
- `text_prompt_segment` / `text_prompt_global`: text conditioning (APPROXIMATED); a global prompt
  is valid only when the item covers the whole shot (or segment);
- `shot_split`: plan-time proposal; at build time only when the shot already is a split planned
  for this item;
- `keyframe_conditioning`: the item is active at the shot's first word and the engine conditions
  on the first frame (`behavior.keyframe_state` → `image.keyframe`);
- `prosody_transfer`: the engine couples expression to the audio (`prosody_coupling`);
- `audio_nonverbal`: the TTS engine synthesizes non-verbal sounds (HONORED for audio items,
  APPROXIMATED for a visual reaction paired with a non-verbal annotation);
- editorial methods (`editorial_cutaway`, `editorial_punch`, `caption_emphasis`, `music_cue`,
  `sfx_cue`): only where the mode template allows them; the build-time pass uses what the spec
  already contains (it never changes spec structure), the plan-time pass proposes them. An
  existing overlay approximates an event it hides entirely or a state it hides for at least half
  of the state's words in the shot; cutaways are proposed only for events; nothing editorial
  stands in for the absence of a behavior (`reaction: none`);
- `pose_guided`, `post_expression`: HONORED once the plugin is validated, APPROXIMATED before;
- `omit`: UNSUPPORTED — the item stays in the CBS and is reported, never dropped.

Abstract knobs are used only when the manifest marks them calibrated. The compiler never writes
back into the CBS (I1, I4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ce_contracts.behavior import BehaviorMatrix, KnobSpec, precision_rank
from ce_core.behavior.cbs import CBSContent, RequestedControl
from ce_core.behavior.compiled import (
    CompiledBehavior,
    CoverageCounts,
    EditorialAction,
    Knobs,
    PlanPause,
    ProsodyPlan,
    Realization,
    VisualPlan,
    VisualSubSpan,
)
from ce_core.enums import CoverageLevel, RealizationMethod
from ce_core.spec.anchors import WordRef, WordSpan
from ce_core.vocab import Vocabulary

from ce_behavior.scene import SceneWords

__all__ = [
    "EDITORIAL",
    "CompileTarget",
    "EditorialContext",
    "Overlay",
    "Punch",
    "compile_behavior",
    "coverage_downgrades",
    "level_rank",
    "realized_methods",
    "realized_targets",
]

M = RealizationMethod
EDITORIAL = frozenset({M.EDITORIAL_CUTAWAY, M.EDITORIAL_PUNCH, M.CAPTION_EMPHASIS, M.MUSIC_CUE, M.SFX_CUE})
# An existing overlay approximates a state item only when it hides at least this share of the
# item's words in the shot (an event must be hidden entirely).
STATE_CUTAWAY_COVER = 0.5
# Strategy tokens that request the absence of a behavior (`reaction: none`): no editorial action
# can stand in for them.
ABSENCE_TOKENS = frozenset({"none"})
LEVEL_RANK = {CoverageLevel.UNSUPPORTED: 0, CoverageLevel.APPROXIMATED: 1, CoverageLevel.HONORED: 2}
STRONG_COUPLING = {"medium", "strong"}
VALIDATED = {"smoke_passed", "bench_passed"}
TEXT_CONTROLS = {"text_segment": M.TEXT_PROMPT_SEGMENT, "text_global": M.TEXT_PROMPT_GLOBAL}


def level_rank(level: str) -> int:
    return LEVEL_RANK[CoverageLevel(level)]


@dataclass(frozen=True)
class Overlay:
    shot_key: str
    first: int
    last: int
    derived_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Punch:
    shot_key: str
    move_key: str
    position: int
    kind: str


@dataclass(frozen=True)
class EditorialContext:
    """What the spec already contains that editorial and structural methods can use (build time)."""

    overlays: tuple[Overlay, ...] = ()
    punches: tuple[Punch, ...] = ()
    sfx_positions: tuple[tuple[str, int], ...] = ()
    music_positions: tuple[tuple[str, int], ...] = ()
    captions_emphasis: bool = False
    split_refs: frozenset[str] = frozenset()  # item refs a shot was split for (derived_from)


@dataclass(frozen=True)
class CompileTarget:
    node_kind: Literal["behavior.compile_voice", "behavior.compile_visual", "plan_time"]
    stage: Literal["plan_time", "build_time"]
    channel: Literal["audio", "visual"]
    target_key: str
    character_key: str
    scope: tuple[int, int]  # word positions the target renders (segment or shot chunk)
    shot: tuple[int, int] | None = None  # the whole shot (visual), for global prompts and keyframes
    shot_key: str | None = None
    chunk: int | None = None
    segment_key: str | None = None
    route_digest: str | None = None
    matrix: BehaviorMatrix | None = None
    knobs: Mapping[str, KnobSpec] = field(default_factory=dict)
    validation: str = "untested_on_gpu"
    unreliable: frozenset[str] = frozenset()
    editorial_methods: frozenset[str] = frozenset()
    editorial: EditorialContext = field(default_factory=EditorialContext)
    tts_matrix: BehaviorMatrix | None = None  # the character's voice route (audio_nonverbal for visuals)
    expression_matrix: BehaviorMatrix | None = None  # a validated expression editor (post_expression)
    expression_validation: str = "untested_on_gpu"


@dataclass
class _Choice:
    method: RealizationMethod
    level: CoverageLevel
    detail: str = ""
    proposal: EditorialAction | None = None


def _window(control: RequestedControl, words: SceneWords) -> tuple[int, int] | None:
    return words.range(control.span) if control.span is not None else None


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _cover_ratio(window: tuple[int, int], scope: tuple[int, int], overlay: tuple[int, int]) -> float:
    """Share of the item's words inside this target that the overlay hides."""
    first, last = max(window[0], scope[0]), min(window[1], scope[1])
    if last < first:
        return 0.0
    hidden = min(last, overlay[1]) - max(first, overlay[0]) + 1
    return max(0, hidden) / (last - first + 1)


def _label(control: RequestedControl) -> str:
    value = control.value.split(";", 1)[0]
    value = value.removeprefix("displayed:")
    return value.split("@", 1)[0].split(":", 1)[0]


class _Selector:
    def __init__(self, cbs: CBSContent, target: CompileTarget, vocab: Vocabulary, words: SceneWords) -> None:
        self.cbs = cbs
        self.target = target
        self.vocab = vocab
        self.words = words
        self.annotation_anchors = {
            (c.character_key, words.range(c.span)): c
            for c in cbs.requested_controls
            if c.dimension == "nonverbal_audio" and c.span is not None
        }

    def describe(self, rng: tuple[int, int]) -> str:
        a, b = self.words.order[rng[0]], self.words.order[rng[1]]
        if a == b:
            return f"{a[0]}.w{a[1]}"
        return f"{a[0]}.w{a[1]}–{b[0] + '.' if b[0] != a[0] else ''}w{b[1]}"

    def control(self, dimension: str) -> tuple[str, str]:
        matrix = self.target.matrix
        if matrix is None:
            return "none", "none"
        decl = matrix.control(dimension)
        return decl.control, decl.temporal_precision

    def native(self, method: RealizationMethod, c: RequestedControl) -> _Choice | None:
        control, precision = self.control(c.dimension)
        wanted = "parametric" if method == M.NATIVE_PARAMETRIC else "native_segment"
        if control != wanted or precision_rank(precision) < precision_rank(str(c.temporal_precision)):
            return None
        if c.dimension in self.target.unreliable:
            return _Choice(method, CoverageLevel.APPROXIMATED, "declared control measured unreliable for this route")
        return _Choice(method, CoverageLevel.HONORED)

    def text(self, method: RealizationMethod, c: RequestedControl, window: tuple[int, int]) -> _Choice | None:
        control, _ = self.control(c.dimension)
        if TEXT_CONTROLS.get(control) != method:
            return None
        if method == M.TEXT_PROMPT_GLOBAL:
            whole = self.target.shot if self.target.channel == "visual" and self.target.shot else self.target.scope
            if not (window[0] <= whole[0] and window[1] >= whole[1]):
                return None
            return _Choice(method, CoverageLevel.APPROXIMATED, "one prompt covers the whole shot")
        return _Choice(method, CoverageLevel.APPROXIMATED, "text conditioning of the sub-span")

    def choose(self, c: RequestedControl) -> _Choice:
        window = _window(c, self.words) or self.target.scope
        definition = self.vocab.dimensions.get(c.dimension)
        preference = list(definition.method_preference) if definition else [M.OMIT]
        reasons: list[str] = []
        for raw in preference:
            method = M(raw)
            choice = self.try_method(method, c, window)
            if choice is not None:
                if reasons and not choice.detail:
                    choice.detail = "; ".join(reasons)
                return choice
            if method in EDITORIAL and method not in self.target.editorial_methods:
                reasons.append(f"{method} not allowed by the mode")
        return _Choice(M.OMIT, CoverageLevel.UNSUPPORTED, "no method available on this route")

    def try_method(self, method: RealizationMethod, c: RequestedControl, window: tuple[int, int]) -> _Choice | None:
        t = self.target
        if method in (M.NATIVE_PARAMETRIC, M.NATIVE_SEGMENT):
            return self.native(method, c)
        if method in (M.TEXT_PROMPT_SEGMENT, M.TEXT_PROMPT_GLOBAL):
            return self.text(method, c, window)
        if method == M.OMIT:
            return _Choice(M.OMIT, CoverageLevel.UNSUPPORTED, "no method available on this route")
        if method in EDITORIAL and _label(c) in ABSENCE_TOKENS:
            return None  # nothing editorial stands in for the absence of a behavior
        if method in EDITORIAL and method not in t.editorial_methods:
            return None
        if method == M.SHOT_SPLIT:
            return self.shot_split(c, window)
        if method == M.KEYFRAME_CONDITIONING:
            matrix = t.matrix
            if (
                t.channel != "visual"
                or t.shot is None
                or matrix is None
                or not matrix.continuity.first_frame_conditioning
            ):
                return None
            if not (window[0] <= t.shot[0] <= window[1]):
                return None
            return _Choice(method, CoverageLevel.APPROXIMATED, "set by the shot's keyframe (start of the shot)")
        if method == M.PROSODY_TRANSFER:
            matrix = t.matrix
            if t.channel != "visual" or matrix is None:
                return None
            if matrix.prosody_coupling.expression_from_audio not in STRONG_COUPLING:
                return None
            return _Choice(method, CoverageLevel.APPROXIMATED, "carried by the voice performance (audio-driven)")
        if method == M.AUDIO_NONVERBAL:
            return self.audio_nonverbal(c, window)
        if method == M.EDITORIAL_CUTAWAY:
            return self.cutaway(c, window)
        if method == M.EDITORIAL_PUNCH:
            return self.punch(c, window)
        if method == M.CAPTION_EMPHASIS:
            if t.stage == "build_time" and not t.editorial.captions_emphasis:
                return None
            return _Choice(method, CoverageLevel.APPROXIMATED, "caption emphasis on the word")
        if method in (M.SFX_CUE, M.MUSIC_CUE):
            positions = t.editorial.sfx_positions if method == M.SFX_CUE else t.editorial.music_positions
            hit = next((key for key, pos in positions if window[0] <= pos <= window[1]), None)
            if hit is not None:
                return _Choice(method, CoverageLevel.APPROXIMATED, f"{hit} at {self.describe(window)}")
            if t.stage == "plan_time":
                kind = "sfx_cue" if method == M.SFX_CUE else "music_cue"
                return self.proposal(method, kind, c, window)
            return None
        if method == M.POSE_GUIDED:
            control, _ = self.control(c.dimension)
            if control != "pose_guided":
                return None
            level = CoverageLevel.HONORED if t.validation in VALIDATED else CoverageLevel.APPROXIMATED
            return _Choice(method, level, "" if level == CoverageLevel.HONORED else "pose control not validated yet")
        if method == M.POST_EXPRESSION:
            matrix = t.expression_matrix
            if matrix is None or matrix.control(c.dimension).control in ("none", "emergent"):
                return None
            validated = t.expression_validation in VALIDATED
            level = CoverageLevel.HONORED if validated else CoverageLevel.APPROXIMATED
            return _Choice(method, level, "" if validated else "expression editor not validated yet")
        return None

    def shot_split(self, c: RequestedControl, window: tuple[int, int]) -> _Choice | None:
        t = self.target
        if t.channel != "visual" or t.shot is None or t.matrix is None:
            return None
        control, _ = self.control(c.dimension)
        if control != "text_global":
            return None
        if t.stage == "build_time":
            if c.item_ref in t.editorial.split_refs and window[0] <= t.shot[0] and window[1] >= t.shot[1]:
                return _Choice(M.SHOT_SPLIT, CoverageLevel.APPROXIMATED, "shot split at the state boundary")
            return None
        if window[0] <= t.shot[0] and window[1] >= t.shot[1]:
            return None  # the state already covers the shot: a global prompt does it
        at = max(window[0], t.shot[0])
        if at == t.shot[0]:
            at = min(window[1], t.shot[1]) + 1
        if at > t.shot[1]:
            return None
        ref = self.words.order[at]
        return _Choice(
            M.SHOT_SPLIT,
            CoverageLevel.APPROXIMATED,
            f"split {t.shot_key} at {ref[0]}.w{ref[1]} so each part gets its own prompt",
            EditorialAction(kind="shot_split", item_ref=c.item_ref, at=WordRef(segment_key=ref[0], word=ref[1])),
        )

    def audio_nonverbal(self, c: RequestedControl, window: tuple[int, int]) -> _Choice | None:
        t = self.target
        if t.channel == "audio":
            matrix = t.matrix
            if matrix is None or matrix.control("nonverbal_audio").control in ("none", "emergent"):
                return None
            return _Choice(M.AUDIO_NONVERBAL, CoverageLevel.HONORED, "synthesized by the voice engine")
        paired = self.annotation_anchors.get((c.character_key, window))
        tts = t.tts_matrix
        if paired is None or tts is None or tts.control("nonverbal_audio").control in ("none", "emergent"):
            return None
        return _Choice(M.AUDIO_NONVERBAL, CoverageLevel.APPROXIMATED, f"paired with {paired.item_ref}")

    def cutaway(self, c: RequestedControl, window: tuple[int, int]) -> _Choice | None:
        t = self.target
        is_event = "/events[" in c.item_ref
        for overlay in t.editorial.overlays:
            if is_event:
                covers = overlay.first <= window[0] and overlay.last >= window[1]
            else:
                covers = _cover_ratio(window, t.scope, (overlay.first, overlay.last)) >= STATE_CUTAWAY_COVER
            if covers or c.item_ref in overlay.derived_refs:
                span = self.describe((overlay.first, overlay.last))
                return _Choice(
                    M.EDITORIAL_CUTAWAY, CoverageLevel.APPROXIMATED, f"overlay {overlay.shot_key} covers {span}"
                )
        if t.stage == "plan_time" and t.channel == "visual" and is_event:
            # Proposed only over a momentary behavior: cutting away for a whole state would hide
            # the performance the state asks for.
            return self.proposal(M.EDITORIAL_CUTAWAY, "cutaway", c, window)
        return None

    def punch(self, c: RequestedControl, window: tuple[int, int]) -> _Choice | None:
        t = self.target
        anchor = window[0]
        for punch in t.editorial.punches:
            same_shot = punch.shot_key == t.shot_key or t.shot_key is None
            if same_shot and abs(punch.position - anchor) <= 1 and window[0] - 1 <= punch.position <= window[1]:
                ref = self.words.order[punch.position]
                return _Choice(
                    M.EDITORIAL_PUNCH,
                    CoverageLevel.APPROXIMATED,
                    f"{punch.kind} {punch.move_key} at {ref[0]}.w{ref[1]}",
                )
        if t.stage == "plan_time" and t.channel == "visual":
            return self.proposal(M.EDITORIAL_PUNCH, "punch_in", c, window)
        return None

    def proposal(self, method: RealizationMethod, kind: str, c: RequestedControl, window: tuple[int, int]) -> _Choice:
        first, last = window
        span = self.words.span(first, last)
        ref = WordRef(segment_key=span.start.segment_key, word=span.start.word)
        action = EditorialAction(
            kind=kind,  # type: ignore[arg-type]
            item_ref=c.item_ref,
            at=ref if kind in ("punch_in", "punch_out", "sfx_cue", "music_cue") else None,
            span=span if kind == "cutaway" else None,
            detail="proposed at plan time",
        )
        return _Choice(method, CoverageLevel.APPROXIMATED, f"proposed {kind} at {self.describe(window)}", action)


def _in_scope(control: RequestedControl, target: CompileTarget, vocab: Vocabulary, words: SceneWords) -> bool:
    definition = vocab.dimensions.get(control.dimension)
    if definition is None or definition.channel != target.channel:
        return False
    if control.character_key != target.character_key:
        return False
    window = _window(control, words)
    return window is not None and _overlaps(window, target.scope)


def _calibrated(target: CompileTarget, dimension: str) -> dict[str, float]:
    """Abstract knob values for a sub-span; uncalibrated knobs are never used (§15.7)."""
    matrix = target.matrix
    if matrix is None:
        return {}
    decl = matrix.control(dimension)
    return {k: 0.5 for k in decl.knobs if k in target.knobs and target.knobs[k].calibrated}


def compile_behavior(
    cbs: CBSContent, target: CompileTarget, *, vocab: Vocabulary, words: SceneWords
) -> CompiledBehavior:
    """One CompiledBehavior for one target (a segment for voice, a shot chunk for visuals, or the
    plan-time pass over a shot)."""
    selector = _Selector(cbs, target, vocab, words)
    realizations: list[Realization] = []
    actions: list[EditorialAction] = []
    chosen: dict[tuple[str, str], _Choice] = {}
    for control in cbs.requested_controls:
        if not _in_scope(control, target, vocab, words):
            continue
        choice = selector.choose(control)
        chosen[(control.item_ref, control.dimension)] = choice
        realizations.append(
            Realization(
                item_ref=control.item_ref,
                dimension=control.dimension,
                level=choice.level,
                method=choice.method,
                detail=choice.detail,
            )
        )
        if choice.proposal is not None:
            actions.append(choice.proposal)
        elif choice.method in EDITORIAL and target.stage == "build_time":
            actions.append(_existing_action(choice, control, words))
    counts = CoverageCounts(
        honored=sum(1 for r in realizations if r.level == CoverageLevel.HONORED),
        approximated=sum(1 for r in realizations if r.level == CoverageLevel.APPROXIMATED),
        unsupported=sum(1 for r in realizations if r.level == CoverageLevel.UNSUPPORTED),
    )
    return CompiledBehavior.model_validate(
        {
            "node_kind": target.node_kind,
            "pass": target.stage,
            "cbs_content_digest": cbs.digest(),
            "route_digest": target.route_digest,
            "target_key": target.target_key,
            "realizations": [r.model_dump(mode="json") for r in realizations],
            "prosody_plans": [p.model_dump(mode="json") for p in _prosody_plans(cbs, target, words)],
            "visual_plans": [v.model_dump(mode="json") for v in _visual_plans(cbs, target, words, chosen)],
            "editorial_actions": [a.model_dump(mode="json") for a in _unique_actions(actions)],
            "predicted_coverage": counts.model_dump(mode="json"),
        }
    )


def _existing_action(choice: _Choice, control: RequestedControl, words: SceneWords) -> EditorialAction:
    kind = {
        M.EDITORIAL_CUTAWAY: "cutaway",
        M.EDITORIAL_PUNCH: "punch_in",
        M.CAPTION_EMPHASIS: "caption_emphasis",
        M.MUSIC_CUE: "music_cue",
        M.SFX_CUE: "sfx_cue",
    }[choice.method]
    span = control.span if isinstance(control.span, WordSpan) else None
    return EditorialAction(
        kind=kind,  # type: ignore[arg-type]
        item_ref=control.item_ref,
        at=span.start if span is not None and kind != "cutaway" else None,
        span=span if kind == "cutaway" else None,
        detail=choice.detail,
    )


def _unique_actions(actions: Iterable[EditorialAction]) -> list[EditorialAction]:
    seen: dict[str, EditorialAction] = {}
    for action in actions:
        seen.setdefault(f"{action.kind}|{action.item_ref}", action)
    return list(seen.values())


def _prosody_plans(cbs: CBSContent, target: CompileTarget, words: SceneWords) -> list[ProsodyPlan]:
    if target.channel != "audio" or target.segment_key is None:
        return []
    out: list[ProsodyPlan] = []
    for directive in cbs.prosody_directives:
        if directive.segment_key != target.segment_key or directive.character_key != target.character_key:
            continue
        seg = words.segment_range(directive.segment_key)
        emotion = None
        intensity = None
        if seg is not None:
            best = 0
            for state in cbs.trajectory:
                rng = words.range(state.span)
                if state.character_key != directive.character_key or rng is None:
                    continue
                overlap = min(seg[1], rng[1]) - max(seg[0], rng[0]) + 1
                if overlap > best:
                    best = overlap
                    emotion, intensity = str(state.emotion.displayed.label), float(state.emotion.displayed.intensity)
        out.append(
            ProsodyPlan(
                character_key=directive.character_key,
                segment_key=directive.segment_key,
                strategy=directive.strategy,
                emotion=emotion,
                emotion_intensity=intensity,
                rate=directive.rate,
                energy=directive.energy,
                pitch_variation=directive.pitch_variation,
                emphasis_words=list(directive.emphasis_words),
                pauses=[PlanPause(after_word=p.after_word, ms=p.ms) for p in directive.pauses],
                nonverbal=list(directive.nonverbal),
                delivery=str(directive.delivery[0]["tag"]) if directive.delivery else None,
            )
        )
    return out


def _visual_plans(
    cbs: CBSContent, target: CompileTarget, words: SceneWords, chosen: Mapping[tuple[str, str], _Choice]
) -> list[VisualPlan]:
    if target.channel != "visual" or target.shot_key is None:
        return []
    realized = {(ref, dim) for (ref, dim), choice in chosen.items() if choice.method != M.OMIT}
    sub_spans: list[VisualSubSpan] = []
    for state in cbs.trajectory:
        rng = words.range(state.span)
        if state.character_key != target.character_key or rng is None or not _overlaps(rng, target.scope):
            continue
        first, last = max(rng[0], target.scope[0]), min(rng[1], target.scope[1])
        refs = sorted(
            {ref for (ref, dim) in realized if ref.rsplit("/acting/states[", 1)[-1].startswith(f"{state.key}]")}
        )
        descriptors = [str(state.emotion.displayed.label), str(state.internal_state.label)]
        descriptors += [str(getattr(state.strategies, ch)) for ch in ("posture", "camera_awareness", "gesture")]
        knobs: dict[str, float] = {}
        for dim in ("head_motion", "gesture", "emotion_visual"):
            knobs.update(_calibrated(target, dim))
        sub_spans.append(
            VisualSubSpan(
                span=words.span(first, last),
                descriptors=list(dict.fromkeys(descriptors)),
                knobs=Knobs(**{k: v for k, v in knobs.items() if k in Knobs.model_fields}),
                item_refs=refs,
            )
        )
    for event in cbs.events:
        if event.character_key != target.character_key:
            continue
        span = event.span or (WordSpan(start=event.at, end=event.at) if event.at else None)
        rng = words.range(span)
        if rng is None or not _overlaps(rng, target.scope):
            continue
        ref = next((r for (r, _) in realized if r.endswith(f"/events[{event.key}]")), None)
        if ref is None:
            continue
        sub_spans.append(
            VisualSubSpan(
                span=words.span(max(rng[0], target.scope[0]), min(rng[1], target.scope[1])),
                descriptors=[str(event.type)],
                item_refs=[ref],
            )
        )
    return [
        VisualPlan(
            shot_key=target.shot_key, chunk=target.chunk, character_key=target.character_key, sub_spans=sub_spans
        )
    ]


def coverage_downgrades(planned: CompiledBehavior, actual: CompiledBehavior) -> list[dict[str, str]]:
    """Items whose level is lower on the actual route than on the planned one (§15.7 build-time)."""
    before = {(r.item_ref, r.dimension): r for r in planned.realizations}
    out: list[dict[str, str]] = []
    for r in actual.realizations:
        old = before.get((r.item_ref, r.dimension))
        if old is not None and level_rank(str(r.level)) < level_rank(str(old.level)):
            out.append(
                {
                    "item_ref": r.item_ref,
                    "dimension": r.dimension,
                    "planned": f"{old.level} ({old.method})",
                    "actual": f"{r.level} ({r.method})",
                }
            )
    return out


def realized_methods(compiled: Sequence[CompiledBehavior]) -> dict[tuple[str, str], Realization]:
    """The weakest realization of each item across the compile outputs that cover it."""
    return {k: r for k, (r, _) in realized_targets(compiled).items()}


def realized_targets(compiled: Sequence[CompiledBehavior]) -> dict[tuple[str, str], tuple[Realization, str]]:
    """`realized_methods` plus the target (`<segment key>` or `<shot key>:c<chunk>`) whose
    realization is the weakest — the engine the item's coverage is attributed to."""
    out: dict[tuple[str, str], tuple[Realization, str]] = {}
    for doc in compiled:
        for r in doc.realizations:
            key = (r.item_ref, r.dimension)
            if key not in out or level_rank(str(r.level)) < level_rank(str(out[key][0].level)):
                out[key] = (r, doc.target_key)
    return out
