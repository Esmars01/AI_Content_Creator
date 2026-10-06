"""Building the VideoSpec from the stage outputs (code only; §13 stages 5, 7 and 8).

The model chooses; this module makes the choices structural: keys, word spans that tile each
scene, world bindings that respect World DNA, shot plans that respect the mode's edit grammar and
the intent policies, acting states with spans and triggers, and the tags of the user's text as
annotations, emotion overrides and events (§21, source `user_tag`).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ce_config.loader import ConfigBundle
from ce_config.schemas import Mode
from ce_core.keys import KeyKind, new_key
from ce_voice import parse_tags
from ce_voice.tags import TaggedText

from ce_director.context import ContextPack, WorldOption
from ce_director.draft import SpecDraft, word_span
from ce_director.intent_policy import PolicySet
from ce_director.models import ActingOut, SceneActingOut, ScenesOut, StateOut

__all__ = [
    "TagHints",
    "WorldChoice",
    "add_acting",
    "add_music",
    "add_scenes",
    "add_segments",
    "add_shots",
    "choose_world",
    "plan_shots",
    "render_outputs",
]

SENTENCE_END = (".", "!", "?", "…")


@dataclass
class TagHints:
    """What the canonical tags in one segment asked for (exact mode: the user's tags)."""

    segment_key: str
    source: str  # user_tag | director
    emotions: list[tuple[str, int, int]] = field(default_factory=list)  # label, first word, last word
    events: list[tuple[str, int]] = field(default_factory=list)  # event type, word


@dataclass
class WorldChoice:
    world: WorldOption
    camera_position: str
    camera_profile: str
    time_of_day: str
    weather: str
    zone: str | None
    posture: str
    framing: str
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- script


def add_segments(
    draft: SpecDraft, texts: Sequence[TaggedText | str], *, speaker: str, source: str
) -> tuple[list[str], list[TagHints]]:
    """One segment per text; canonical tags become annotations (and hints for acting)."""
    keys: list[str] = []
    hints: list[TagHints] = []
    for raw in texts:
        tagged = raw if isinstance(raw, TaggedText) else parse_tags(raw)
        key = draft.new_key(KeyKind.SEGMENT)
        draft.data["script"]["segments"].append(
            {
                "key": key,
                "speaker_key": speaker,
                "text": tagged.text,
                "language": None,
                "annotations": [],
                "claim_keys": [],
            }
        )
        for ann in tagged.annotations:
            draft.add_annotation(key, ann.type, ann.tag, ann.start_word, ann.end_word, source=source)
        hint = TagHints(key, source)
        hint.emotions = [(e.label, e.start_word, e.end_word) for e in tagged.emotions]
        hint.events = [(e.type, e.word) for e in tagged.events]
        keys.append(key)
        hints.append(hint)
    return keys, hints


# ---------------------------------------------------------------------- worlds


def choose_world(
    pack: ContextPack,
    bundle: ConfigBundle,
    mode: Mode,
    *,
    world_kind: str | None,
    requested_world: UUID | None,
    camera_position: str | None,
    camera_profile: str | None,
    posture: str | None,
    framing: str | None,
    avoid: tuple[str, str] | None,
) -> WorldChoice:
    """World binding (§13 stage 7, §19.3): the requested world, else a world of the requested kind
    (the creator's defaults first), else the creator's default world. Camera position and profile
    must be permitted by World DNA; consecutive videos vary the position (or time of day)."""
    notes: list[str] = []
    worlds = pack.worlds
    if not worlds:
        raise LookupError("no approved world is available for planning")
    defaults = [w for w in worlds if w.world_id in pack.creator.default_world_ids]
    chosen: WorldOption | None = None
    if requested_world is not None:
        chosen = next((w for w in worlds if w.world_id == requested_world), None)
        if chosen is None:
            notes.append(f"The requested world {requested_world} has no approved version; choosing another.")
    if chosen is None and world_kind:
        of_kind = [w for w in [*defaults, *worlds] if str(w.dna.kind) == world_kind]
        if of_kind:
            chosen = of_kind[0]
        else:
            fallback = (defaults or worlds)[0]
            notes.append(
                f"No approved {world_kind.replace('_', ' ')} world exists; using {fallback.dna.name} "
                f"({fallback.dna.kind}). A world proposal for a new world arrives with World Studio (Phase 10)."
            )
    if chosen is None:
        preferred = [w for w in (defaults or worlds) if str(w.dna.kind) in mode.default_world_kinds]
        chosen = (preferred or defaults or worlds)[0]
    dna = chosen.dna
    permitted = [p for p in dna.camera_positions if str(p.status) == "permitted"] or list(dna.camera_positions)
    profiles_pref = [
        *([camera_profile] if camera_profile else []),
        *pack.creator.dna.camera.preferred_camera_profiles,
        *mode.default_camera_profiles,
    ]

    def profile_for(position: Any) -> str | None:
        allowed = list(position.allowed_camera_profiles)
        return next((p for p in profiles_pref if p in allowed and p in bundle.camera_profiles), None)

    candidates = [p for p in permitted if profile_for(p) is not None]
    if camera_position:
        named = [p for p in candidates if p.key == camera_position]
        if named:
            candidates = named + [p for p in candidates if p.key != camera_position]
        else:
            notes.append(f"Camera position {camera_position} is not usable in {dna.name}; choosing another.")
    if not candidates:
        candidates = permitted
    position = candidates[0]
    time_of_day = str(dna.time_and_weather.default_time_of_day)
    if avoid is not None and (position.key, time_of_day) == avoid:
        others = [p for p in candidates if p.key != position.key]
        times = [str(t) for t in dna.time_and_weather.allowed_times if str(t) != time_of_day]
        if others:
            notes.append(
                f"The previous video used {position.key} at {time_of_day}; using {others[0].key} to avoid "
                "visual repetition (§18.6)."
            )
            position = others[0]
        elif times:
            notes.append(
                f"The previous video used {position.key} at {time_of_day}; using {times[0]} to avoid visual "
                "repetition (§18.6)."
            )
            time_of_day = times[0]
    profile = profile_for(position) or (list(position.allowed_camera_profiles) or mode.default_camera_profiles)[0]
    if camera_profile and profile != camera_profile:
        notes.append(f"Camera profile {camera_profile} is not allowed at {position.key}; using {profile}.")
    wanted_posture = posture or str(pack.creator.dna.gesture.default_posture or "seated_upright")
    zone_key: str | None = None
    final_posture = wanted_posture
    if dna.zones:
        zone = next((z for z in dna.zones if wanted_posture in z.allowed_postures), None)
        if zone is None:
            zone = dna.zones[0]
            final_posture = str(zone.allowed_postures[0])
            notes.append(f"Posture {wanted_posture} is not allowed in {dna.name} ({zone.key}); using {final_posture}.")
        zone_key = zone.key
    final_framing = framing or str(position.default_framing or pack.creator.dna.camera.framing_preference)
    return WorldChoice(
        world=chosen,
        camera_position=position.key,
        camera_profile=profile,
        time_of_day=time_of_day,
        weather=str(dna.time_and_weather.default_weather),
        zone=zone_key,
        posture=final_posture,
        framing=final_framing,
        notes=notes,
    )


# ---------------------------------------------------------------------- scenes and shots


def plan_shots(
    draft: SpecDraft,
    scene_key: str,
    *,
    mode: Mode,
    wpm: float,
    reveal: tuple[str, int] | None,
    policies: PolicySet,
    hold_words: int,
) -> list[tuple[int, int]]:
    """Base-shot ranges (scene word positions) under the edit grammar's `max_shot_s`: cuts prefer
    segment starts, then sentence starts; none within `no_cut_within_words` of the reveal when
    `hold_through_reveal`; a `first_cut_within_s` proposal adds an early cut (pattern interrupt)."""
    words = draft.scene_words(scene_key)
    n = len(words)
    if n == 0:
        return []
    per_word = 60.0 / max(wpm, 1.0)
    max_words = max(2, int(mode.edit_grammar.max_shot_s / per_word))
    hold = policies.get(scene_key, "editing", "hold_through_reveal")
    no_cut = policies.get(scene_key, "editing", "no_cut_within_words")
    radius = int(no_cut.value) if no_cut is not None else hold_words
    reveal_pos = words.index(reveal) if reveal is not None and reveal in words else None
    blocked: set[int] = set()
    if hold is not None and hold.value is True and reveal_pos is not None:
        blocked = {c for c in range(reveal_pos - radius, reveal_pos + radius + 1)}

    def rank(c: int) -> int:
        if words[c][1] == 0:
            return 0
        previous = draft.tokens(words[c - 1][0])[words[c - 1][1]].text
        return 1 if previous.endswith(SENTENCE_END) else 2

    min_words = 2
    cuts: list[int] = []
    first = policies.get(scene_key, "editing", "first_cut_within_s")
    if first is not None and isinstance(first.value, int | float):
        target = max(min_words, round(float(first.value) / per_word))
        options = [c for c in range(min_words, min(n - min_words, target) + 1) if c not in blocked]
        if options:
            cuts.append(min(options, key=lambda c: (rank(c), abs(c - target))))
    start = cuts[-1] if cuts else 0
    remaining = n - start
    pieces = -(-remaining // max_words)  # ceil: the fewest shots that respect max_shot_s
    for k in range(1, pieces):
        ideal = start + round(k * remaining / pieces)
        lo = max((cuts[-1] if cuts else 0) + min_words, ideal - max_words // 3)
        hi = min(n - min_words, ideal + max_words // 3)
        options = [c for c in range(lo, hi + 1) if c not in blocked]
        if options:
            cuts.append(min(options, key=lambda c: (rank(c), abs(c - ideal))))
    bounds = [0, *sorted(set(cuts)), n]
    return [(a, b - 1) for a, b in itertools.pairwise(bounds) if b > a]


def add_scenes(
    draft: SpecDraft,
    scenes: ScenesOut,
    *,
    pack: ContextPack,
    choices: Sequence[WorldChoice],
    wardrobe_version_id: UUID | None,
) -> dict[str, tuple[str, int] | None]:
    """Scenes with intent, world binding and cast; returns each scene's reveal word."""
    reveal: dict[str, tuple[str, int] | None] = {}
    character = pack.character_key
    for order, (scene_out, choice) in enumerate(zip(scenes.scenes, choices, strict=True), start=1):
        key = f"scn_{order}"
        intent = scene_out.intent.model_dump(mode="json")
        draft.data["scenes"].append(
            {
                "key": key,
                "purpose": scene_out.purpose,
                "order": order,
                "segment_keys": list(scene_out.segment_keys),
                "intent": intent,
                "world": {
                    "world_version_id": str(choice.world.world_version_id),
                    "camera_position_key": choice.camera_position,
                    "time_of_day": choice.time_of_day,
                    "weather": choice.weather,
                    "overrides": {
                        "element_states": {},
                        "hide_elements": [],
                        "add_elements": [],
                        "move_elements": [],
                        "lighting": None,
                        "acoustics": None,
                    },
                    "continuity_ref": {"kind": "world_plate", "camera_position_key": choice.camera_position},
                },
                "cast": [
                    {
                        "character_key": character,
                        "wardrobe_version_id": str(wardrobe_version_id) if wardrobe_version_id else None,
                        "placement": choice.zone,
                        "default_posture": choice.posture,
                    }
                ],
                "acting": None,
                "pacing": None,
                "shots": [],
            }
        )
        reveal[key] = (scene_out.reveal_at.segment_key, scene_out.reveal_at.word) if scene_out.reveal_at else None
    return reveal


def add_shots(
    draft: SpecDraft,
    scene_key: str,
    ranges: Sequence[tuple[int, int]],
    scene_out_overlays: Sequence[Any],
    *,
    choice: WorldChoice,
    character: str,
    mode: Mode,
    takes: int,
) -> None:
    scene = draft.scene(scene_key)
    allowed = {str(t) for t in mode.allowed_shot_types}
    for first, last in ranges:
        scene["shots"].append(
            {
                "key": draft.new_key(KeyKind.SHOT),
                "type": "talking_head",
                "layer": "base",
                "span": word_span(draft.ref(scene_key, first), draft.ref(scene_key, last)),
                "character_key": character,
                "camera": {
                    "profile_id": choice.camera_profile,
                    "framing": choice.framing,
                    "angle": "eye_level",
                    "moves": [],
                },
                "visual": {"keyframe": {"strategy": "composite", "asset_id": None}, "prompt_extra": "", "negative": ""},
                "takes": {"count": takes, "selected_take_key": None},
                "derived_from": [],
            }
        )
    for overlay in scene_out_overlays:
        if overlay.kind not in allowed:
            continue
        shot: dict[str, Any] = {
            "key": draft.new_key(KeyKind.SHOT),
            "type": overlay.kind,
            "layer": "overlay",
            "span": word_span(
                (overlay.start.segment_key, overlay.start.word), (overlay.end.segment_key, overlay.end.word)
            ),
            "character_key": None,
            "camera": {"profile_id": choice.camera_profile, "framing": "insert", "angle": "eye_level", "moves": []},
            "takes": {"count": 1, "selected_take_key": None},
            "derived_from": [],
        }
        if overlay.kind == "broll":
            shot["broll"] = {
                "source": "generate",
                "asset_id": None,
                "prompt": overlay.prompt or "a relevant close-up insert",
                "world_bound": False,
                "allow_text_in_frame": False,
            }
        else:
            shot["title"] = {"text": overlay.title or overlay.prompt or " ", "style_id": None}
        scene["shots"].append(shot)


# ---------------------------------------------------------------------- acting


def state_starts(
    draft: SpecDraft, scene_key: str, states: Sequence[StateOut], resolve_seconds: Mapping[int, tuple[str, int]]
) -> list[int | None]:
    """Each state's first word position (start_s states resolved by `resolve_seconds`)."""
    out: list[int | None] = []
    for i, state in enumerate(states):
        if state.start is not None:
            out.append(draft.position(scene_key, (state.start.segment_key, state.start.word)))
        else:
            ref = resolve_seconds.get(i)
            out.append(draft.position(scene_key, ref) if ref is not None else None)
    return out


def add_acting(
    draft: SpecDraft,
    acting: ActingOut,
    *,
    character: str,
    hints: Sequence[TagHints],
    resolved: Mapping[str, Mapping[int, tuple[str, int]]],
) -> dict[str, list[tuple[int, int]]]:
    """Writes `scenes[].acting`; returns each scene's state ranges (positions)."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for scene_out in acting.scenes:
        scene = draft.scene(scene_out.scene_key)
        words = draft.scene_words(scene_out.scene_key)
        found = state_starts(draft, scene_out.scene_key, scene_out.states, resolved.get(scene_out.scene_key, {}))
        starts = [s if s is not None else 0 for s in found]
        order = sorted(range(len(scene_out.states)), key=lambda i: starts[i])
        states_json: list[dict[str, Any]] = []
        keys: dict[int, str] = {}
        taken = draft.all_keys()
        scene_ranges: list[tuple[int, int]] = []
        for rank, i in enumerate(order):
            state = scene_out.states[i]
            first = starts[i]
            nxt = order[rank + 1] if rank + 1 < len(order) else None
            last = (starts[nxt] if nxt is not None else len(words)) - 1
            key = new_key(KeyKind.STATE, taken)
            taken.add(key)
            keys[i] = key
            scene_ranges.append((first, last))
            source = "user_tag" if _from_tag(hints, words, first, state.displayed.label) else "director"
            transition = None
            if state.transition is not None:
                trigger = None
                if state.transition.trigger_kind:
                    at = state.transition.trigger_at
                    ref = (at.segment_key, at.word) if at is not None else words[first]
                    trigger = {
                        "kind": state.transition.trigger_kind,
                        "at": {"segment_key": ref[0], "word": ref[1]},
                        "description": state.transition.description,
                    }
                transition = {
                    "trigger": trigger,
                    "style": state.transition.style,
                    "duration_words": state.transition.duration_words,
                }
            states_json.append(
                {
                    "key": key,
                    "character_key": character,
                    "source": source,
                    "span": word_span(words[first], words[last]),
                    "carry": False,
                    "internal_state": {"label": state.internal_state, "description": state.internal_description},
                    "social_goal": state.social_goal,
                    "audience_goal": state.audience_goal,
                    "performance_intent": state.performance_intent,
                    "emotion": {
                        "felt": state.felt.model_dump(mode="json"),
                        "displayed": state.displayed.model_dump(mode="json"),
                        "masking": state.masking,
                    },
                    "confidence_delta": state.confidence_delta,
                    "attention_target": state.attention_target,
                    "strategies": state.strategies.model_dump(mode="json"),
                    "transition_in": transition,
                    "priority": state.priority,
                    "out_of_character": None,
                }
            )
        events_json = _events(draft, scene_out, character, keys, hints, words)
        for ann in scene_out.annotations:
            draft.add_annotation(ann.segment_key, ann.type, ann.tag, ann.word, ann.end_word, source="director")
        scene["acting"] = {
            "situation": {
                "kind": scene_out.situation.kind,
                "description": scene_out.situation.description,
                "audience_stance": scene_out.situation.audience_stance,
                "stimulus": scene_out.situation.stimulus.model_dump(mode="json")
                if scene_out.situation.stimulus
                else None,
            },
            "states": states_json,
            "events": events_json,
        }
        ranges[scene_out.scene_key] = scene_ranges
    return ranges


def _from_tag(hints: Sequence[TagHints], words: Sequence[tuple[str, int]], first: int, label: str) -> bool:
    seg, w = words[first]
    return any(
        h.source == "user_tag" and h.segment_key == seg and e[1] == w and e[0] == label
        for h in hints
        for e in h.emotions
    )


def _events(
    draft: SpecDraft,
    scene_out: SceneActingOut,
    character: str,
    state_keys: Mapping[int, str],
    hints: Sequence[TagHints],
    words: Sequence[tuple[str, int]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    taken = set(draft.all_keys())

    def key() -> str:
        k = new_key(KeyKind.EVENT, taken)
        taken.add(k)
        return k

    seen: set[tuple[str, str, int]] = set()
    for ev in scene_out.events:
        trigger_ref = None
        if ev.reacts_to_state is not None and ev.reacts_to_state in state_keys:
            trigger_ref = (
                f"/scenes[{scene_out.scene_key}]/acting/states[{state_keys[ev.reacts_to_state]}]/transition_in/trigger"
            )
        events.append(
            {
                "key": key(),
                "character_key": character,
                "type": ev.type,
                "at": {"segment_key": ev.at.segment_key, "word": ev.at.word},
                "span": None,
                "duration_ms": ev.duration_ms,
                "direction": ev.direction,
                "target": ev.target,
                "intensity": ev.intensity,
                "purpose": ev.purpose,
                "trigger_ref": trigger_ref,
                "priority": ev.priority,
                "source": "director",
            }
        )
        seen.add((ev.type, ev.at.segment_key, ev.at.word))
    in_scene = {seg for seg, _ in words}
    for hint in hints:
        if hint.segment_key not in in_scene:
            continue
        for event_type, word in hint.events:
            if (event_type, hint.segment_key, word) in seen:
                for e in events:
                    if (e["type"], e["at"]["segment_key"], e["at"]["word"]) == (event_type, hint.segment_key, word):
                        e["source"] = hint.source
                continue
            events.append(
                {
                    "key": key(),
                    "character_key": character,
                    "type": event_type,
                    "at": {"segment_key": hint.segment_key, "word": word},
                    "span": None,
                    "duration_ms": None,
                    "direction": None,
                    "target": None,
                    "intensity": None,
                    "purpose": None,
                    "trigger_ref": None,
                    "priority": "should",
                    "source": hint.source,
                }
            )
    return events


# ---------------------------------------------------------------------- audio and output


def add_music(draft: SpecDraft, *, mood: str, duck_db: float) -> None:
    """One generated, instrumental cue per scene with speech (music stays under speech, §13 stage 10)."""
    for scene in draft.scenes():
        if not scene["segment_keys"]:
            continue
        draft.data["audio"]["music"]["cues"].append(
            {
                "key": draft.new_key(KeyKind.MUSIC_CUE),
                "span": {"kind": "scene", "scene_key": scene["key"]},
                "mode": "generate",
                "mood": mood,
                "bpm": None,
                "asset_id": None,
                "duck_db": duck_db,
                "derived_from": [],
            }
        )


def render_outputs(bundle: ConfigBundle, platform_targets: Sequence[str], aspect: str) -> list[dict[str, str]]:
    """One output: the first target platform's preset in the primary aspect."""
    for platform_id in [*platform_targets, *sorted(bundle.platforms)]:
        platform = bundle.platforms.get(platform_id)
        if platform is None:
            continue
        preset = next((p for p in platform.render_presets if str(p.aspect) == aspect), None)
        if preset is not None:
            return [{"preset_id": preset.id, "aspect": aspect}]
    return []
