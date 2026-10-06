"""The CBS resolver: `behavior.resolve` (§15.6, §10.6, ADR 0018).

`resolve()` turns one scene's acting plan, intent and annotations, each cast member's Creator DNA
and pinned memory snapshot, and the bound world's behavior-relevant subset into the
model-independent `CBSContent`. It is a pure function of its inputs (deterministic, no engine
knowledge): the same inputs give the same content digest across versions and engine swaps (I1).

Resolution order (§10.6): Creator DNA ⊳ active memory habits from the pinned snapshot (they move
DNA defaults by at most `behavior.memory_shift_max`, inside DNA bounds) ⊳ scene acting states
(absolute values, clamped to DNA bounds unless `out_of_character.allowed`) ⊳ user tags and edits
(already normalized into states, events and annotations by the tag parser). Avoidances are the
union of DNA and memory avoidances; a habit never overrides an avoidance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ce_config.schemas import BehaviorConfig
from ce_core.behavior.cbs import (
    Baseline,
    BehaviorProfile,
    CanonicalBehaviorSpec,
    CastRefs,
    CBSCastMember,
    CBSContent,
    CBSContinuity,
    CBSEnvelope,
    CBSEvent,
    CBSInternalState,
    CBSSituation,
    CBSStimulus,
    CBSWorld,
    Constraints,
    ContinuityRequirement,
    EnvelopeRefs,
    Habit,
    Pause,
    ProsodyDirective,
    ProvenanceEntry,
    RequestedControl,
    Scope,
    TrajectoryState,
    WorldAffordances,
    WorldRefs,
)
from ce_core.enums import AnnotationType, Priority
from ce_core.identity.creator import CreatorDNA
from ce_core.identity.world import WorldDNA
from ce_core.spec.acting import EmotionSpec, EmotionValue
from ce_core.spec.anchors import WordSpan
from ce_core.spec.intent import SceneIntent
from ce_core.spec.videospec import Scene, VideoSpec
from ce_core.vocab import Vocabulary

from ce_behavior.scene import (
    ResolvedState,
    SceneWords,
    annotation_path,
    covering,
    event_path,
    resolved_states,
    state_path,
)

__all__ = ["CastInput", "WorldInput", "build_envelope", "reliability_of", "resolve"]

HABIT_CATEGORIES = ("gaze_habit", "reaction_habit", "gesture_habit")
STRATEGY_CHANNELS = ("prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness")
PRIORITY_RANK = {Priority.NICE: 0, Priority.SHOULD: 1, Priority.MUST: 2}
RELIABILITY_RANK = {"low": 0, "medium": 1, "high": 2}
USER_SOURCES = {"user_tag", "user_edit"}
DEFAULT_BASELINE = {"energy": 0.5, "confidence": 0.5, "warmth": 0.5, "expressivity": 0.5}


@dataclass(frozen=True)
class CastInput:
    """One cast member as the resolver sees it (the graph keys the node on the same digests)."""

    character_key: str
    dna: CreatorDNA | None
    dna_behavior_digest: str
    snapshot_items: tuple[Mapping[str, Any], ...] = ()
    creator_version_id: UUID | None = None
    appearance_version_id: UUID | None = None
    voice_version_id: UUID | None = None
    memory_snapshot_id: UUID | None = None


@dataclass(frozen=True)
class WorldInput:
    dna: WorldDNA
    behavior_digest: str


@dataclass
class _Items:
    controls: list[RequestedControl] = field(default_factory=list)
    provenance: dict[str, ProvenanceEntry] = field(default_factory=dict)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(min(max(value, low), high), 4)


def _fmt(value: float) -> str:
    return f"{value:g}"


def reliability_of(vocab: Vocabulary, dimension: str, label: str) -> str:
    """The best initial reliability class among the proxies measuring `label` on `dimension`."""
    best = "low"
    for proxy in vocab.proxies.values():
        matches = proxy.dimension == dimension and label in proxy.labels
        if matches and RELIABILITY_RANK[str(proxy.reliability)] > RELIABILITY_RANK[best]:
            best = str(proxy.reliability)
    return best


# ---------------------------------------------------------------------- cast: DNA ⊳ memory


def _shift(dna_value: float, memory_value: float, limit: float) -> float:
    return _clamp(dna_value + max(-limit, min(limit, memory_value - dna_value)))


def _profile(member: CastInput, config: BehaviorConfig) -> tuple[BehaviorProfile, float]:
    dna = member.dna
    baseline = dict(dna.behavior.baseline.model_dump() if dna else DEFAULT_BASELINE)
    ranges: dict[str, tuple[float, float]] = {
        k: (float(v[0]), float(v[1])) for k, v in (dna.behavior.emotion_ranges if dna else {}).items()
    }
    habits: list[Habit] = []
    for item in sorted(member.snapshot_items, key=lambda i: (str(i.get("kind")), str(i.get("item_id")))):
        kind = str(item.get("kind", ""))
        value = dict(item.get("value") or {})
        category = kind.split(".", 1)[0]
        if category in HABIT_CATEGORIES:
            habits.append(
                Habit(
                    memory_item_id=UUID(str(item["item_id"])),
                    kind=kind,
                    value=value,
                    confidence=float(item.get("confidence", 1.0)),
                )
            )
        elif kind == "social_behavior.warmth" and "level" in value:
            baseline["warmth"] = _shift(baseline["warmth"], float(value["level"]), config.memory_shift_max)
        elif kind == "emotional_tendency.emotional_range" and {"label", "min", "max"} <= set(value):
            label, low, high = str(value["label"]), float(value["min"]), float(value["max"])
            if label in ranges:  # inside DNA bounds; an empty intersection keeps the DNA range (§18.7)
                dna_low, dna_high = ranges[label]
                if max(dna_low, low) <= min(dna_high, high):
                    ranges[label] = (max(dna_low, low), min(dna_high, high))
            else:
                ranges[label] = (low, high)
    profile = BehaviorProfile(
        baseline=Baseline(**{k: _clamp(float(v)) for k, v in baseline.items()}),
        emotion_ranges={k: (_clamp(lo), _clamp(hi)) for k, (lo, hi) in sorted(ranges.items())},
        habits=habits,
    )
    max_intensity = max((hi for _, hi in ranges.values()), default=1.0)
    return profile, max_intensity


def _avoidances(cast: Iterable[CastInput]) -> list[str]:
    out: set[str] = set()
    for member in cast:
        if member.dna is not None:
            out.update(f"gesture:{g}" for g in member.dna.avoidances.gestures)
            out.update(f"emotion_visual:{c.label}>{_fmt(c.max_intensity)}" for c in member.dna.avoidances.emotions)
        for item in member.snapshot_items:
            kind, value = str(item.get("kind", "")), dict(item.get("value") or {})
            if kind == "avoidance.gesture" and value.get("gesture"):
                out.add(f"gesture:{value['gesture']}")
            elif kind == "avoidance.emotional_state" and value.get("label"):
                out.add(f"emotion_visual:{value['label']}>{_fmt(float(value.get('max_intensity', 0.0)))}")
    return sorted(out)


def _emotion_cap(avoidances: list[str], label: str) -> float | None:
    for entry in avoidances:
        if entry.startswith(f"emotion_visual:{label}>"):
            return float(entry.rsplit(">", 1)[1])
    return None


# ---------------------------------------------------------------------- world affordances


def _affordances(scene: Scene, world: WorldInput, config: BehaviorConfig, referenced: set[str]) -> WorldAffordances:
    dna = world.dna
    overrides = scene.world.overrides if scene.world is not None else None
    hidden = set(overrides.hide_elements) if overrides else set()
    elements = [(e.key, str(e.kind), e.label) for e in dna.elements if e.key not in hidden]
    elements += [(a.key, str(a.kind), a.label) for a in (overrides.add_elements if overrides else [])]
    attention = {key for key, kind, _ in elements if kind in config.attention_element_kinds}
    attention |= {key for key, _, _ in elements if key in referenced}
    placement = next((c.placement for c in scene.cast if c.placement), None)
    zone = dna.zone(placement) if placement else None
    postures = list(zone.allowed_postures) if zone else []
    seated = any(p.startswith("seated") for p in postures)
    hand_space = "none"
    for word, space in (("desk", "desk_surface"), ("table", "table"), ("counter", "counter")):
        if any(kind == "furniture" and word in f"{key} {label}".lower() for key, kind, label in elements):
            hand_space = space
            break
    if hand_space == "none" and seated:
        hand_space = "lap"
    return WorldAffordances(
        attention_targets=["camera", *sorted(attention)],
        seated=seated,
        allowed_postures=postures,
        hand_space=hand_space,
    )


def _element_bindings(
    scene: Scene, world: WorldInput | None
) -> tuple[dict[str, str], dict[str, tuple[float, float, float]]]:
    """Bound states of stateful elements (override, else default) and moved positions."""
    states: dict[str, str] = {}
    positions: dict[str, tuple[float, float, float]] = {}
    if world is None:
        return states, positions
    overrides = scene.world.overrides if scene.world is not None else None
    for element in world.dna.elements:
        if element.default_state is not None:
            states[element.key] = str(element.default_state)
    if overrides is not None:
        states.update({k: str(v) for k, v in overrides.element_states.items()})
        positions.update({m.key: tuple(m.position) for m in overrides.move_elements})  # type: ignore[misc]
    return states, positions


# ---------------------------------------------------------------------- requested controls


def _priority_for_strategy(state_priority: Priority, channel: str, token: str, config: BehaviorConfig) -> Priority:
    cap = Priority(config.strategy_priority_cap)
    if token in config.low_salience.get(channel, []):
        cap = Priority.NICE
    return state_priority if PRIORITY_RANK[state_priority] <= PRIORITY_RANK[cap] else cap


def _confidence(kind: str, priority: Priority, source: str, supported: bool, config: BehaviorConfig) -> float:
    pc = config.planner_confidence
    value = pc.base[kind] + pc.priority_delta[str(priority)]  # type: ignore[index]
    if source in USER_SOURCES:
        value += pc.user_source_bonus
    if supported:
        value += pc.supported_bonus
    return _clamp(value, 0.05, 0.99)


def _add(
    items: _Items,
    *,
    item_ref: str,
    character: str,
    dimension: str,
    value: str,
    precision: str,
    priority: Priority,
    kind: str,
    source: str,
    supported_by: list[str],
    reliability: str,
    config: BehaviorConfig,
    span: Any = None,
    duration_ms: int | None = None,
) -> None:
    items.controls.append(
        RequestedControl(
            item_ref=item_ref,
            character_key=character,
            dimension=dimension,
            value=value,
            temporal_precision=precision,  # type: ignore[arg-type]
            priority=priority,
            planner_confidence=_confidence(
                kind, priority, source, any(s.startswith(("memory:", "dna:")) for s in supported_by), config
            ),
            observation_reliability=reliability,  # type: ignore[arg-type]
            span=span,
            duration_ms=duration_ms,
        )
    )
    if item_ref not in items.provenance:
        items.provenance[item_ref] = ProvenanceEntry(
            item_ref=item_ref,
            source=source,  # type: ignore[arg-type]
            supported_by=sorted(set(supported_by)),
        )


def _memory_support(
    member: CastInput,
    *,
    gaze_direction: str | None = None,
    expression: str | None = None,
    gesture: str | None = None,
    posture: str | None = None,
) -> list[str]:
    out: list[str] = []
    for item in member.snapshot_items:
        kind, value = str(item.get("kind", "")), dict(item.get("value") or {})
        ref = f"memory:{item.get('item_id')}"
        if (
            (gaze_direction and kind == "gaze_habit.thinking_glance" and value.get("direction") == gaze_direction)
            or (expression and kind.startswith("reaction_habit.") and value.get("expression") == expression)
            or (gesture and kind == "gesture_habit.common_gesture" and value.get("gesture") == gesture)
            or (posture and kind == "gesture_habit.posture" and value.get("posture") == posture)
        ):
            out.append(ref)
    dna = member.dna
    if dna is not None:
        if gaze_direction and gaze_direction in dna.gaze.look_away_directions:
            out.append("dna:/gaze/look_away_directions")
        if expression:
            out += [
                f"dna:/behavior/reaction_habits/{k}"
                for k, h in dna.behavior.reaction_habits.items()
                if h.expression == expression
            ]
        if gesture and any(p.gesture == gesture for p in dna.gesture.preferred_gestures):
            out.append("dna:/gesture/preferred_gestures")
        if posture and dna.gesture.default_posture == posture:
            out.append("dna:/gesture/default_posture")
    return out


def _intent_support(scene: Scene, source: str) -> list[str]:
    return [f"intent:/scenes[{scene.key}]/intent"] if source == "intent_policy" else []


def _emotion_value(emotion: EmotionSpec) -> str:
    d, f = emotion.displayed, emotion.felt
    if emotion.masking:
        return f"displayed:{d.label}@{_fmt(d.intensity)};felt:{f.label}@{_fmt(f.intensity)};masking"
    return f"{d.label}@{_fmt(d.intensity)}"


def _state_items(
    items: _Items,
    scene: Scene,
    words: SceneWords,
    resolved: ResolvedState,
    emotion: EmotionSpec,
    member: CastInput,
    vocab: Vocabulary,
    config: BehaviorConfig,
) -> None:
    values, state = resolved.values, resolved.state
    base = state_path(scene.key, state.key)
    source = str(state.source)
    priority = Priority(state.priority)
    character = state.character_key
    transition = state.transition_in
    mid_segment = bool(
        transition and transition.style == "sudden" and transition.trigger and transition.trigger.at.word != 0
    )
    displayed = str(emotion.displayed.label)
    reliability = "low" if emotion.masking else reliability_of(vocab, "emotion_visual", displayed)
    common: dict[str, Any] = {
        "item_ref": f"{base}/emotion",
        "character": character,
        "kind": "emotion",
        "source": source,
        "config": config,
        "span": state.span,
    }
    _add(
        items,
        dimension="emotion_visual",
        value=_emotion_value(emotion),
        priority=priority,
        precision="word" if mid_segment else "segment",
        supported_by=_intent_support(scene, source),
        reliability=reliability,
        **common,
    )
    _add(
        items,
        dimension="emotion_vocal",
        value=_emotion_value(emotion),
        priority=priority,
        precision="segment",
        supported_by=_intent_support(scene, source),
        reliability="low" if emotion.masking else reliability_of(vocab, "emotion_vocal", displayed),
        **common,
    )
    strategies = values.strategies
    if strategies is None:
        return
    for channel in STRATEGY_CHANNELS:
        token = str(getattr(strategies, channel))
        support = _intent_support(scene, source)
        if channel == "posture":
            support += _memory_support(member, posture=token)
        for dimension in vocab.strategy_dimensions.get(channel, ()):
            _add(
                items,
                item_ref=f"{base}/strategies/{channel}",
                character=character,
                dimension=dimension,
                value=token,
                precision="segment",
                priority=_priority_for_strategy(priority, channel, token, config),
                kind="prosody" if channel == "prosody" else "strategy",
                source=source,
                supported_by=support,
                reliability=reliability_of(vocab, dimension, token),
                config=config,
                span=state.span,
            )


def _resolve_emotion(
    emotion: EmotionSpec, profile: BehaviorProfile, max_intensity: float, avoidances: list[str], allowed_out: bool
) -> EmotionSpec:
    def clamp(value: EmotionValue, displayed: bool) -> EmotionValue:
        intensity = float(value.intensity)
        if not allowed_out:
            bounds = profile.emotion_ranges.get(str(value.label))
            if bounds is not None and displayed:
                intensity = min(max(intensity, bounds[0]), bounds[1])
            cap = _emotion_cap(avoidances, str(value.label))
            if cap is not None and displayed:
                intensity = min(intensity, cap)
            intensity = min(intensity, max_intensity)
        return EmotionValue(label=value.label, intensity=_clamp(intensity))

    return EmotionSpec(
        felt=clamp(emotion.felt, False), displayed=clamp(emotion.displayed, True), masking=emotion.masking
    )


# ---------------------------------------------------------------------- the resolver


def resolve(
    spec: VideoSpec,
    scene: Scene,
    *,
    cast: Mapping[str, CastInput],
    world: WorldInput | None,
    vocab: Vocabulary,
    config: BehaviorConfig,
    previous_scene_characters: Iterable[str] = (),
) -> CBSContent:
    words = SceneWords.of(spec, scene)
    characters = list(
        dict.fromkeys(
            [c.character_key for c in scene.cast] + [spec.script.segment(k).speaker_key for k in scene.segment_keys]
        )
    )
    members = {c: cast.get(c) or CastInput(c, None, "sha256:" + "0" * 64) for c in characters}
    profiles = {c: _profile(m, config) for c, m in members.items()}
    avoidances = _avoidances(members.values())
    max_intensity = max((mi for _, mi in profiles.values()), default=1.0)
    items = _Items()
    acting = scene.acting

    # -- trajectory and state items
    trajectory: list[TrajectoryState] = []
    out_of_character: list[str] = []
    for character in characters:
        profile, _ = profiles[character]
        confidence = profile.baseline.confidence
        for resolved in resolved_states(scene, words, character):
            values, state = resolved.values, resolved.state
            required = (
                values.emotion,
                values.strategies,
                values.internal_state,
                values.social_goal,
                values.audience_goal,
                values.performance_intent,
                values.attention_target,
            )
            if any(v is None for v in required):
                continue  # a carried state without a previous state is a validation error (§15.3)
            assert values.emotion and values.strategies and values.internal_state
            allowed_out = bool(values.out_of_character and values.out_of_character.allowed)
            if allowed_out:
                out_of_character.append(state_path(scene.key, state.key))
            confidence = _clamp(confidence + float(state.confidence_delta))
            emotion = _resolve_emotion(values.emotion, profile, max_intensity, avoidances, allowed_out)
            trajectory.append(
                TrajectoryState(
                    key=state.key,
                    character_key=character,
                    span=state.span,
                    internal_state=CBSInternalState(label=values.internal_state.label),
                    social_goal=str(values.social_goal),
                    audience_goal=str(values.audience_goal),
                    performance_intent=str(values.performance_intent),
                    emotion=emotion,
                    confidence=confidence,
                    attention_target=str(values.attention_target),
                    strategies=values.strategies,
                    transition_in=state.transition_in,
                    priority=state.priority,
                )
            )
            _state_items(items, scene, words, resolved, emotion, members[character], vocab, config)

    # -- events
    events: list[CBSEvent] = []
    referenced: set[str] = set()
    for resolved in resolved_states(scene, words):
        target = resolved.values.attention_target
        if target and target.startswith("el_"):
            referenced.add(target)
    for event in acting.events if acting else []:
        definition = vocab.events.get(str(event.type))
        dimension = definition.dimension if definition else "gaze"
        duration = event.duration_ms or (definition.default_duration_ms if definition else 600)
        if event.target and event.target.startswith("el_"):
            referenced.add(event.target)
        events.append(
            CBSEvent(
                key=event.key,
                character_key=event.character_key,
                type=event.type,
                dimension=dimension,
                at=event.at,
                span=event.span,
                duration_ms=duration,
                direction=event.direction,
                target=event.target,
                intensity=event.intensity,
                purpose=event.purpose,
                priority=event.priority,
                source=event.source,
            )
        )
        anchor = event.at or (event.span.start if event.span else None)
        where = f"@{anchor.segment_key}.w{anchor.word}" if anchor else ""
        label = str(event.type) + (f":{event.direction}" if event.direction else "")
        member = members.get(event.character_key) or CastInput(event.character_key, None, "sha256:" + "0" * 64)
        support = _memory_support(
            member,
            gaze_direction=str(event.direction) if dimension == "gaze" and event.direction else None,
            expression=str(event.type) if dimension in ("facial_expression", "reaction") else None,
            gesture=str(event.type) if dimension == "gesture" else None,
        )
        span = event.span or (WordSpan(start=event.at, end=event.at) if event.at else None)
        _add(
            items,
            item_ref=event_path(scene.key, event.key),
            character=event.character_key,
            dimension=dimension,
            value=f"{label}{where}+{duration}ms",
            precision="word",
            priority=Priority(event.priority),
            kind="event",
            source=str(event.source),
            supported_by=support,
            reliability=reliability_of(vocab, dimension, str(event.type)),
            config=config,
            span=span,
            duration_ms=duration,
        )
    if acting and acting.situation.stimulus and acting.situation.stimulus.kind == "element":
        referenced.add(acting.situation.stimulus.ref)

    # -- annotations and prosody directives
    prosody: list[ProsodyDirective] = []
    pause_defaults = dict(vocab.pause_ms)
    for segment_key in scene.segment_keys:
        segment = spec.script.segment(segment_key)
        character = segment.speaker_key
        profile, _ = profiles[character]
        emphasis: list[int] = []
        pauses: list[Pause] = []
        nonverbal: list[dict[str, Any]] = []
        delivery: list[dict[str, Any]] = []
        for annotation in segment.annotations:
            annotation_dim = vocab.annotation_dimensions.get(str(annotation.type))
            if annotation_dim is None:
                continue
            start, end = annotation.span.start.word, annotation.span.end.word
            tag = str(annotation.tag)
            kind = AnnotationType(annotation.type)
            if kind == AnnotationType.PAUSE:
                ms = int(annotation.pause_ms or pause_defaults.get(tag, 300))
                pauses.append(Pause(after_word=end, ms=ms))
                value = f"{ms}ms after w{end}"
            elif kind == AnnotationType.EMPHASIS:
                emphasis += list(range(start, end + 1))
                value = f"{tag} w{start}" if start == end else f"{tag} w{start}-w{end}"
            elif kind == AnnotationType.NONVERBAL_AUDIO:
                nonverbal.append({"tag": tag, "after_word": end})
                value = f"{tag} after w{end}"
            elif kind == AnnotationType.PRONUNCIATION:
                value = f"respell w{start}-w{end} as {annotation.respelling}"
            else:
                delivery.append({"tag": tag, "start_word": start, "end_word": end})
                value = f"{tag} w{start}-w{end}"
            source = str(annotation.source)
            priority = Priority.MUST if source in (*USER_SOURCES, "intent_policy") else Priority.SHOULD
            _add(
                items,
                item_ref=annotation_path(segment_key, annotation.key),
                character=character,
                dimension=annotation_dim,
                value=value,
                precision="word",
                priority=priority,
                kind="annotation",
                source=source,
                supported_by=_intent_support(scene, source),
                reliability=reliability_of(vocab, annotation_dim, tag),
                config=config,
                span=annotation.span,
            )
        seg_range = words.segment_range(segment_key)
        covering_state = covering(resolved_states(scene, words, character), *seg_range) if seg_range else None
        strategy = (
            str(covering_state.values.strategies.prosody)
            if covering_state and covering_state.values.strategies
            else None
        )
        defaults = config.prosody_strategies.get(strategy or "", None)
        prosody.append(
            ProsodyDirective(
                character_key=character,
                segment_key=segment_key,
                strategy=strategy or "controlled_even",
                rate=defaults.rate if defaults else 1.0,
                energy=_clamp(profile.baseline.energy + (defaults.energy_delta if defaults else 0.0)),
                pitch_variation=defaults.pitch_variation if defaults else 0.5,
                emphasis_words=sorted(set(emphasis)),
                pauses=sorted(pauses, key=lambda p: p.after_word),
                nonverbal=nonverbal,
                delivery=delivery,
            )
        )

    # -- world, continuity, constraints
    cbs_world = None
    if world is not None:
        cbs_world = CBSWorld(
            behavior_digest=world.behavior_digest, affordances=_affordances(scene, world, config, referenced)
        )
    element_state, element_position = _element_bindings(scene, world)
    requirements = [
        ContinuityRequirement(
            kind="attention_target",
            character_key=r.values.character_key,
            ref=r.values.attention_target,
            state=element_state.get(r.values.attention_target),
            position=element_position.get(r.values.attention_target),
        )
        for r in resolved_states(scene, words)
        if r.values.attention_target and r.values.attention_target.startswith("el_")
    ]
    requirements += [
        ContinuityRequirement(
            kind="attention_target",
            character_key=e.character_key,
            ref=e.target,
            state=element_state.get(e.target),
            position=element_position.get(e.target),
        )
        for e in (acting.events if acting else [])
        if e.target and e.target.startswith("el_")
    ]
    previous = set(previous_scene_characters)
    requirements += [
        ContinuityRequirement(kind="posture_carry", character_key=c, ref="previous_scene_end")
        for c in characters
        if c in previous
    ]
    unique = {(r.kind, r.character_key, r.ref): r for r in requirements}
    locks = sorted(
        {
            str(lock.group)
            for lock in spec.locks
            if str(lock.group) in ("voice", "acting")
            and (lock.scope.scene_keys is None or scene.key in lock.scope.scene_keys)
            and (lock.scope.character_keys is None or set(lock.scope.character_keys) & set(characters))
        }
    )
    situation = None
    if acting is not None:
        stimulus = acting.situation.stimulus
        situation = CBSSituation(
            kind=acting.situation.kind,
            audience_stance=acting.situation.audience_stance,
            stimulus=CBSStimulus(kind=stimulus.kind, ref=stimulus.ref) if stimulus else None,
        )
    return CBSContent(
        cast=[
            CBSCastMember(
                character_key=c,
                dna_behavior_digest=members[c].dna_behavior_digest,
                behavior_profile=profiles[c][0],
            )
            for c in characters
        ],
        world=cbs_world,
        intent=SceneIntent.model_validate(scene.intent.model_dump(exclude={"notes"})),
        situation=situation,
        trajectory=trajectory,
        events=events,
        prosody_directives=prosody,
        continuity=CBSContinuity(requirements=sorted(unique.values(), key=lambda r: (r.kind, r.character_key, r.ref))),
        constraints=Constraints(
            locks=locks,
            avoidances=avoidances,
            out_of_character=sorted(out_of_character),
            max_intensity=_clamp(max_intensity),
        ),
        requested_controls=items.controls,
        provenance=list(items.provenance.values()),
    )


def build_envelope(
    content: CBSContent,
    *,
    spec: VideoSpec,
    scene: Scene,
    cast: Mapping[str, CastInput],
    vocab_version: str,
) -> CanonicalBehaviorSpec:
    """Assembles the envelope on read (traceability only; never digested, never cached)."""
    refs = EnvelopeRefs(
        cast=[
            CastRefs(
                character_key=m.character_key,
                creator_version_id=m.creator_version_id,
                appearance_version_id=m.appearance_version_id,
                voice_version_id=m.voice_version_id,
                memory_snapshot_id=m.memory_snapshot_id,
            )
            for m in (cast.get(c.character_key) for c in content.cast)
            if m is not None
            and m.creator_version_id is not None
            and m.appearance_version_id is not None
            and m.voice_version_id is not None
        ],
        world=WorldRefs(
            world_version_id=scene.world.world_version_id,
            camera_position_key=scene.world.camera_position_key,
            time_of_day=scene.world.time_of_day,
            weather=scene.world.weather,
        )
        if scene.world is not None
        else None,
    )
    return CanonicalBehaviorSpec(
        envelope=CBSEnvelope(
            vocab_version=vocab_version,
            scope=Scope(video_id=spec.video_id, version_id=spec.version_id, scene_key=scene.key),
            refs=refs,
            content_digest=content.digest(),
        ),
        content=content,
    )
