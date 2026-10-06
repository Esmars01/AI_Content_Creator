"""EditOperation → SpecPatch (§28 step 3). Code, not the LLM, decides what each operation changes.

Operations are applied in order to a working copy of the spec document (so a later operation sees
an earlier one: split the acting states of "the first 3 seconds", then change them); the patch is
the key-addressed difference between the original and the result. Reference slots must already be
resolved (`resolve_operation`). Vocabulary values are checked strictly here (I13): the Director
maps LLM labels onto the vocabulary before translation.

Some operations change nothing in the spec and instead steer the build or record a side effect:
`reroute` (forced re-routes; an `adapter_id` also pins the engine in `generation.engine_hints`),
`regenerate` with `strategy: lipsync_patch`, and `memory_feedback`.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ce_core.edit.locks import regenerate_refusals
from ce_core.edit.ops import (
    ActingChanges,
    AddAnnotation,
    AddBehaviorEvent,
    AddScene,
    AddShot,
    EditOperation,
    EditScope,
    EditScript,
    EmotionValueChange,
    MemoryFeedback,
    MoveScene,
    RefreshMemory,
    Regenerate,
    RemoveAnnotation,
    RemoveBehaviorEvent,
    RemoveScene,
    RemoveShot,
    ReplaceBroll,
    Reroute,
    SelectTake,
    SetActing,
    SetAnnotation,
    SetBehaviorEvent,
    SetBrand,
    SetCamera,
    SetCaptions,
    SetCast,
    SetEffects,
    SetIntent,
    SetLock,
    SetMeta,
    SetMusic,
    SetPacing,
    SetProduct,
    SetProvenanceLabel,
    SetRenderOutputs,
    SetSfx,
    SetShot,
    SetWardrobe,
    SetWorldBinding,
    SetWorldOverride,
    SplitShot,
)
from ce_core.edit.patch import SpecPatch, patch_from_diff
from ce_core.edit.rebase import rebase_segment
from ce_core.errors import Issue
from ce_core.keys import KeyKind, new_key
from ce_core.text import tokenize
from ce_core.vocab import Vocabulary

__all__ = ["EditError", "TranslateContext", "Translation", "resolve_operation", "translate"]

Doc = dict[str, Any]
VIDEO_INTENT_FIELDS = frozenset(
    {
        "narrative_goal",
        "audience_effect",
        "persuasion_goal",
        "information_goal",
        "emotional_arc",
        "attention_goal",
        "cta_goal",
    }
)
SCENE_INTENT_FIELDS = frozenset(
    {
        "narrative_goal",
        "emotional_goal",
        "audience_effect",
        "persuasion_goal",
        "information_goal",
        "attention_goal",
        "reveal_strategy",
        "tension_level",
        "curiosity_level",
        "performance_strategy",
    }
)
UNIT_INTENT_FIELDS = frozenset({"tension_level", "curiosity_level"})


class EditError(ValueError):
    """An operation cannot be applied; `issues` say why (with paths and codes)."""

    def __init__(self, message: str, issues: Sequence[Issue] = ()) -> None:
        super().__init__(message)
        self.issues = list(issues) or [Issue(code="edit_invalid", message=message)]


@dataclass(frozen=True)
class TranslateContext:
    vocab: Vocabulary
    worlds: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # world_version_id → WorldDNA JSON
    camera_profiles: frozenset[str] | None = None
    render_presets: frozenset[str] | None = None
    node_keys: Sequence[str] = ()  # the parent's graph (regenerate, reroute)
    emotion_range: Callable[[str, str], tuple[float, float] | None] | None = None  # DNA bounds per character
    actor: str = "user"  # user | director | system
    allow_lock_removal: bool = False  # explicit user actions only (the lock endpoint, structured ops)


@dataclass
class Translation:
    patch: SpecPatch
    document: Doc
    notes: list[str] = field(default_factory=list)
    rebase: list[dict[str, Any]] = field(default_factory=list)
    force_reroute: list[str] = field(default_factory=list)
    lipsync_patch_shots: list[str] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    removed_locks: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------- reference slots


def resolve_operation(
    op: EditOperation, slots: Mapping[str, EditScope], anchors: Mapping[str, Mapping[str, Any]]
) -> EditOperation:
    """Replaces reference slots (`scope.ref`, `at_ref`) with the resolver's concrete scopes and
    word anchors; unknown slots raise `EditError`."""
    data = op.model_dump(mode="json")

    def fill_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
        if not scope or not scope.get("ref"):
            return scope
        slot = slots.get(scope["ref"])
        if slot is None:
            raise EditError(f"unknown reference {scope['ref']}", [Issue("unknown_reference", f"no {scope['ref']}")])
        merged = {k: v for k, v in slot.model_dump(mode="json").items() if v is not None}
        merged.update({k: v for k, v in scope.items() if v is not None and k != "ref"})
        merged["ref"] = None
        return merged

    def fill_at(holder: dict[str, Any]) -> None:
        ref = holder.get("at_ref")
        if not ref:
            return
        anchor = anchors.get(ref)
        if anchor is None:
            raise EditError(f"unknown anchor {ref}", [Issue("unknown_reference", f"no anchor {ref}")])
        holder["at"] = dict(anchor)
        holder["at_ref"] = None

    if "scope" in data:
        data["scope"] = fill_scope(data["scope"])
    for name in ("event", "annotation"):
        if isinstance(data.get(name), dict):
            if name == "annotation" and data[name].get("at_ref"):
                anchor = anchors.get(data[name]["at_ref"])
                if anchor is None:
                    raise EditError("unknown anchor", [Issue("unknown_reference", f"no anchor {data[name]['at_ref']}")])
                data[name]["span"] = {"kind": "words", "start": dict(anchor), "end": dict(anchor)}
                data[name]["at_ref"] = None
            else:
                fill_at(data[name])
    for move in data.get("add_moves") or []:
        fill_at(move)
    return type(op).model_validate(data)


# ---------------------------------------------------------------------- helpers


def _ref(segment_key: str, word: int) -> dict[str, Any]:
    return {"segment_key": segment_key, "word": word}


def _span(a: tuple[str, int], b: tuple[str, int]) -> dict[str, Any]:
    return {"kind": "words", "start": _ref(*a), "end": _ref(*b)}


class _T:
    def __init__(self, doc: Doc, ctx: TranslateContext) -> None:
        self.doc = doc
        self.ctx = ctx
        self.out = Translation(patch=SpecPatch(), document=doc)
        self.original = copy.deepcopy(doc)

    # ------------------------------------------------------------------ lookups
    def fail(self, code: str, message: str, path: str | None = None) -> EditError:
        return EditError(message, [Issue(code=code, message=message, path=path)])

    def keys(self) -> set[str]:
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                key = node.get("key")
                if isinstance(key, str):
                    found.add(key)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.doc)
        return found

    def new_key(self, kind: KeyKind) -> str:
        return new_key(kind, self.keys())

    def scenes(self) -> list[Doc]:
        return sorted(self.doc["scenes"], key=lambda s: s["order"])

    def scene(self, key: str) -> Doc:
        scenes: list[Doc] = self.doc["scenes"]
        for scene in scenes:
            if scene["key"] == key:
                return scene
        raise self.fail("unknown_key", f"no scene {key}", "/scenes")

    def segment(self, key: str) -> Doc:
        segments: list[Doc] = self.doc["script"]["segments"]
        for segment in segments:
            if segment["key"] == key:
                return segment
        raise self.fail("unknown_key", f"no segment {key}", "/script/segments")

    def scene_of_segment(self, key: str) -> Doc:
        scenes: list[Doc] = self.doc["scenes"]
        for scene in scenes:
            if key in scene["segment_keys"]:
                return scene
        raise self.fail("unknown_key", f"segment {key} is in no scene", "/scenes")

    def shot(self, key: str, scene_key: str | None = None) -> tuple[Doc, Doc]:
        for scene in self.doc["scenes"]:
            if scene_key is not None and scene["key"] != scene_key:
                continue
            for shot in scene["shots"]:
                if shot["key"] == key:
                    return scene, shot
        raise self.fail("unknown_key", f"no shot {key}", "/scenes")

    def words(self, scene: Doc) -> list[tuple[str, int]]:
        return [(key, i) for key in scene["segment_keys"] for i in range(len(tokenize(self.segment(key)["text"])))]

    def position(self, scene: Doc, ref: Mapping[str, Any]) -> int:
        words = self.words(scene)
        try:
            return words.index((ref["segment_key"], int(ref["word"])))
        except ValueError:
            raise self.fail(
                "anchor_out_of_range", f"{ref['segment_key']}:{ref['word']} is not a word of scene {scene['key']}"
            ) from None

    def span_range(self, scene: Doc, span: Mapping[str, Any] | None) -> tuple[int, int] | None:
        if not span or span.get("kind") != "words":
            return None
        return self.position(scene, span["start"]), self.position(scene, span["end"])

    def scope_scenes(self, scope: EditScope | None) -> list[Doc]:
        if scope is None:
            return self.scenes()
        if scope.ref is not None:
            raise self.fail("unresolved_reference", f"reference {scope.ref} was not resolved")
        if scope.scene_keys is not None:
            return [self.scene(k) for k in scope.scene_keys]
        if scope.shot_keys is not None:
            found = [self.shot(k)[0] for k in scope.shot_keys]
            return list({s["key"]: s for s in found}.values())
        if scope.span is not None:
            return [self.scene_of_segment(scope.span.start.segment_key)]
        if scope.state_keys is not None:
            return [
                s
                for s in self.scenes()
                if s.get("acting") and any(st["key"] in scope.state_keys for st in s["acting"]["states"])
            ]
        return self.scenes()

    def only_character(self, scope: EditScope | None = None) -> str:
        if scope is not None and scope.character_keys:
            return scope.character_keys[0]
        cast = self.doc["cast"]
        if len(cast) == 1:
            return str(cast[0]["key"])
        raise self.fail("ambiguous_character", "name the character: the video has several")

    def check_vocab(self, category: str, token: str | None, path: str) -> None:
        if token is not None and not self.ctx.vocab.has(category, token):
            raise self.fail("unknown_vocab", f"{token!r} is not a {category} (vocab {self.ctx.vocab.version})", path)

    def note(self, text: str) -> None:
        if text not in self.out.notes:
            self.out.notes.append(text)

    def world(self, version_id: str | None) -> Mapping[str, Any] | None:
        return self.ctx.worlds.get(str(version_id)) if version_id else None

    # ------------------------------------------------------------------ dispatch
    def apply(self, op: EditOperation) -> None:
        handler = getattr(self, f"op_{op.op}")
        handler(op)

    # ------------------------------------------------------------------ intent
    def op_set_intent(self, op: SetIntent) -> None:
        targets = (
            [("/intent/video", self.doc["intent"]["video"], VIDEO_INTENT_FIELDS)]
            if op.scope is None
            else [
                (f"/scenes[{s['key']}]/intent", s["intent"], SCENE_INTENT_FIELDS) for s in self.scope_scenes(op.scope)
            ]
        )
        for path, intent, allowed in targets:
            for name, value in op.fields.items():
                if name not in allowed:
                    raise self.fail("unknown_field", f"{name} is not an intent field here", f"{path}/{name}")
                if name in UNIT_INTENT_FIELDS:
                    if not isinstance(value, int | float) or not 0 <= float(value) <= 1:
                        raise self.fail("invalid_value", f"{name} must be 0..1", f"{path}/{name}")
                    intent[name] = float(value)
                elif name == "emotional_arc":
                    labels = value if isinstance(value, list) else [value]
                    for label in labels:
                        if label not in self.ctx.vocab.emotions:
                            raise self.fail("unknown_vocab", f"{label!r} is not an emotion", f"{path}/{name}")
                    intent[name] = list(labels)
                else:
                    if value is not None and not isinstance(value, str):
                        raise self.fail("invalid_value", f"{name} takes a vocabulary token", f"{path}/{name}")
                    self.check_vocab(f"intent.{name}", value, f"{path}/{name}")
                    intent[name] = value

    # ------------------------------------------------------------------ acting
    def _split_states(self, scene: Doc, rng: tuple[int, int], characters: set[str] | None) -> None:
        words = self.words(scene)
        p0, p1 = rng
        states = scene["acting"]["states"]
        changed = True
        while changed:
            changed = False
            for index, state in enumerate(list(states)):
                if characters is not None and state["character_key"] not in characters:
                    continue
                srange = self.span_range(scene, state["span"])
                if srange is None:
                    continue
                a, b = srange
                cut = p0 if a < p0 <= b else (p1 + 1 if a <= p1 < b else None)
                if cut is None:
                    continue
                first = copy.deepcopy(state)
                second = copy.deepcopy(state)
                first["span"] = _span(words[a], words[cut - 1])
                second["key"] = self.new_key(KeyKind.STATE)
                second["span"] = _span(words[cut], words[b])
                second["transition_in"] = None
                states[index] = first
                states.insert(index + 1, second)
                self.note(
                    f"split acting state {state['key']} at word {cut} of scene {scene['key']} (new {second['key']})"
                )
                changed = True
                break

    def _resolved_state(self, scene: Doc, state: Doc) -> Doc:
        """A carried state with its inherited values filled in (from the previous state)."""
        if not state.get("carry"):
            return state
        previous: Doc | None = None
        for other in scene["acting"]["states"]:
            if other is state:
                break
            if other["character_key"] == state["character_key"]:
                previous = other
        if previous is None:
            return state
        base = self._resolved_state(scene, previous)
        filled = copy.deepcopy(state)
        for name in (
            "internal_state",
            "social_goal",
            "audience_goal",
            "performance_intent",
            "emotion",
            "attention_target",
            "strategies",
        ):
            if filled.get(name) is None:
                filled[name] = copy.deepcopy(base.get(name))
        filled["carry"] = False
        return filled

    def _emotion_value(self, character: str, value: Doc, change: EmotionValueChange, path: str) -> Doc:
        out = dict(value)
        if change.label is not None:
            if change.label not in self.ctx.vocab.emotions:
                raise self.fail("unknown_vocab", f"{change.label!r} is not an emotion", path)
            out["label"] = change.label
        if change.intensity is not None:
            out["intensity"] = float(change.intensity)
        elif change.intensity_delta is not None:
            out["intensity"] = round(min(1.0, max(0.0, float(out["intensity"]) + change.intensity_delta)), 4)
        bounds = self.ctx.emotion_range(character, out["label"]) if self.ctx.emotion_range else None
        if bounds is not None:
            low, high = bounds
            clamped = round(min(high, max(low, float(out["intensity"]))), 4)
            if clamped != out["intensity"]:
                self.note(f"{out['label']} intensity for {character} clamped to {clamped} (Creator DNA range)")
                out["intensity"] = clamped
        return out

    def _change_state(self, scene: Doc, index: int, changes: ActingChanges) -> None:
        states = scene["acting"]["states"]
        state = self._resolved_state(scene, states[index])
        base = f"/scenes[{scene['key']}]/acting/states[{state['key']}]"
        character = state["character_key"]
        for name, category in (
            ("internal_state", "internal_state"),
            ("social_goal", "social_goal"),
            ("audience_goal", "audience_goal"),
            ("performance_intent", "performance_intent"),
        ):
            value = getattr(changes, name)
            if value is not None:
                self.check_vocab(category, value, f"{base}/{name}")
                if name == "internal_state":
                    state["internal_state"] = {**(state.get("internal_state") or {}), "label": value}
                else:
                    state[name] = value
        if changes.emotion is not None:
            emotion = copy.deepcopy(state["emotion"])
            ch = changes.emotion
            if ch.displayed is not None:
                emotion["displayed"] = self._emotion_value(
                    character, emotion["displayed"], ch.displayed, f"{base}/emotion"
                )
            if ch.felt is not None:
                emotion["felt"] = self._emotion_value(character, emotion["felt"], ch.felt, f"{base}/emotion")
            elif ch.displayed is not None and not emotion.get("masking"):
                emotion["felt"] = dict(emotion["displayed"])  # not masking: felt follows what is shown
            if ch.masking is not None:
                emotion["masking"] = ch.masking
            if emotion.get("masking") and emotion["felt"] == emotion["displayed"]:
                emotion["masking"] = False
                self.note(f"{state['key']}: masking cleared (felt equals displayed)")
            state["emotion"] = emotion
        if changes.confidence_delta is not None:
            state["confidence_delta"] = changes.confidence_delta
        if changes.attention_target is not None:
            target = changes.attention_target
            if not target.startswith("el_"):
                self.check_vocab("attention_target", target, f"{base}/attention_target")
            state["attention_target"] = target
        if changes.strategies:
            strategies = dict(state.get("strategies") or {})
            for channel, token in changes.strategies.items():
                self.check_vocab(f"strategy.{channel}", token, f"{base}/strategies/{channel}")
                strategies[channel] = token
            state["strategies"] = strategies
        if changes.transition_in is not None:
            tr = changes.transition_in
            current = copy.deepcopy(state.get("transition_in")) or {
                "trigger": None,
                "style": "gradual",
                "duration_words": 0,
            }
            if tr.style is not None:
                self.check_vocab("transition_style", tr.style, f"{base}/transition_in/style")
                current["style"] = tr.style
            if tr.trigger_kind is not None:
                self.check_vocab("trigger_kind", tr.trigger_kind, f"{base}/transition_in/trigger")
                at = tr.trigger_at.model_dump(mode="json") if tr.trigger_at else state["span"]["start"]
                current["trigger"] = {"kind": tr.trigger_kind, "at": at, "description": ""}
            if tr.duration_words is not None:
                current["duration_words"] = tr.duration_words
            state["transition_in"] = current
        if changes.priority is not None:
            state["priority"] = changes.priority
        state["source"] = "user_edit"
        states[index] = state

    def op_set_acting(self, op: SetActing) -> None:
        touched = 0
        characters = set(op.scope.character_keys) if op.scope.character_keys is not None else None
        for scene in self.scope_scenes(op.scope):
            if scene.get("acting") is None:
                raise self.fail(
                    "no_acting", f"scene {scene['key']} has no acting plan", f"/scenes[{scene['key']}]/acting"
                )
            if op.situation is not None:
                situation = scene["acting"]["situation"]
                if op.situation.kind is not None:
                    self.check_vocab("situation_kind", op.situation.kind, f"/scenes[{scene['key']}]/acting/situation")
                    situation["kind"] = op.situation.kind
                if op.situation.audience_stance is not None:
                    self.check_vocab("audience_stance", op.situation.audience_stance, f"/scenes[{scene['key']}]/acting")
                    situation["audience_stance"] = op.situation.audience_stance
                if op.situation.description is not None:
                    situation["description"] = op.situation.description
            if op.changes.is_empty:
                continue
            rng = self.span_range(scene, op.scope.span.model_dump(mode="json")) if op.scope.span else None
            if rng is not None:
                self._split_states(scene, rng, characters)
            for index, state in enumerate(scene["acting"]["states"]):
                if characters is not None and state["character_key"] not in characters:
                    continue
                if op.scope.state_keys is not None and state["key"] not in op.scope.state_keys:
                    continue
                if rng is not None:
                    srange = self.span_range(scene, state["span"])
                    if srange is None or srange[0] < rng[0] or srange[1] > rng[1]:
                        continue
                self._change_state(scene, index, op.changes)
                touched += 1
        if not op.changes.is_empty and touched == 0:
            raise self.fail("empty_scope", "no acting state is in the edit's scope")

    def _event_scene(self, scope: EditScope, at: Mapping[str, Any] | None) -> Doc:
        if at is not None:
            return self.scene_of_segment(at["segment_key"])
        scenes = self.scope_scenes(scope)
        if not scenes:
            raise self.fail("empty_scope", "the event's scope has no scene")
        return scenes[0]

    def op_add_behavior_event(self, op: AddBehaviorEvent) -> None:
        event = op.event
        if event.at_ref is not None:
            raise self.fail("unresolved_reference", f"anchor {event.at_ref} was not resolved")
        at = event.at.model_dump(mode="json") if event.at else None
        span = event.span.model_dump(mode="json") if event.span else None
        if at is None and span is None and op.scope.span is not None:
            at = op.scope.span.start.model_dump(mode="json")
        scene = self._event_scene(op.scope, at or (span["start"] if span else None))
        if at is None and span is None:
            words = self.words(scene)
            if not words:
                raise self.fail("no_words", f"scene {scene['key']} has no words to anchor an event")
            at = _ref(*words[0])
        if scene.get("acting") is None:
            raise self.fail("no_acting", f"scene {scene['key']} has no acting plan")
        anchor = at or span["start"]  # type: ignore[index]
        self.position(scene, anchor)
        if event.type not in self.ctx.vocab.events:
            raise self.fail("unknown_vocab", f"{event.type!r} is not a behavior event", "/event/type")
        self.check_vocab("direction", event.direction, "/event/direction")
        self.check_vocab("event_purpose", event.purpose, "/event/purpose")
        character = event.character_key or (
            op.scope.character_keys[0]
            if op.scope.character_keys
            else self.segment(anchor["segment_key"])["speaker_key"]
        )
        key = event.key or self.new_key(KeyKind.EVENT)
        if key in self.keys():
            raise self.fail("duplicate_key", f"key {key} exists")
        scene["acting"]["events"].append(
            {
                "key": key,
                "character_key": character,
                "type": event.type,
                "at": at,
                "span": span if at is None else None,
                "duration_ms": event.duration_ms,
                "direction": event.direction,
                "target": event.target,
                "intensity": event.intensity,
                "purpose": event.purpose,
                "trigger_ref": None,
                "priority": event.priority,
                "source": "user_edit",
            }
        )

    def op_remove_behavior_event(self, op: RemoveBehaviorEvent) -> None:
        scene = self.scene(op.scene_key)
        events = (scene.get("acting") or {}).get("events", [])
        kept = [e for e in events if e["key"] != op.event_key]
        if len(kept) == len(events):
            raise self.fail("unknown_key", f"no event {op.event_key} in {op.scene_key}")
        scene["acting"]["events"] = kept

    def op_set_behavior_event(self, op: SetBehaviorEvent) -> None:
        targets: list[Doc] = []
        if op.event_key is not None:
            scene = self.scene(op.scene_key or "")
            targets = [e for e in (scene.get("acting") or {}).get("events", []) if e["key"] == op.event_key]
            if not targets:
                raise self.fail("unknown_key", f"no event {op.event_key} in {op.scene_key}")
        else:
            characters = set(op.scope.character_keys) if op.scope and op.scope.character_keys else None
            for scene in self.scope_scenes(op.scope):
                for event in (scene.get("acting") or {}).get("events", []):
                    if event["type"] == op.event_type and (characters is None or event["character_key"] in characters):
                        targets.append(event)
            if not targets:
                raise self.fail("empty_scope", f"no {op.event_type} event in the edit's scope")
        ch = op.changes
        for event in targets:
            if ch.type is not None:
                if ch.type not in self.ctx.vocab.events:
                    raise self.fail("unknown_vocab", f"{ch.type!r} is not a behavior event")
                event["type"] = ch.type
            if ch.at is not None:
                event["at"], event["span"] = ch.at.model_dump(mode="json"), None
            if ch.span is not None:
                event["span"], event["at"] = ch.span.model_dump(mode="json"), None
            for name in ("duration_ms", "target", "priority"):
                value = getattr(ch, name)
                if value is not None:
                    event[name] = value
            if ch.direction is not None:
                self.check_vocab("direction", ch.direction, "/changes/direction")
                event["direction"] = ch.direction
            if ch.purpose is not None:
                self.check_vocab("event_purpose", ch.purpose, "/changes/purpose")
                event["purpose"] = ch.purpose
            if ch.intensity is not None:
                event["intensity"] = ch.intensity
            elif ch.intensity_delta is not None:
                current = float(event["intensity"] if event.get("intensity") is not None else 0.5)
                event["intensity"] = round(min(1.0, max(0.0, current + ch.intensity_delta)), 4)
            if event.get("source") != "compiler_approximation":
                event["source"] = "user_edit"

    # ------------------------------------------------------------------ annotations
    def _annotation_list(self, segment_key: str) -> list[Doc]:
        return list(self.segment(segment_key)["annotations"])

    def op_add_annotation(self, op: AddAnnotation) -> None:
        ann = op.annotation
        if ann.at_ref is not None:
            raise self.fail("unresolved_reference", f"anchor {ann.at_ref} was not resolved")
        span = ann.span.model_dump(mode="json") if ann.span else None
        if span is None and op.scope is not None and op.scope.span is not None:
            span = op.scope.span.model_dump(mode="json")
        if span is None:
            raise self.fail("missing_span", "an annotation needs a word span")
        segment_key = op.segment_key or span["start"]["segment_key"]
        if span["start"]["segment_key"] != segment_key or span["end"]["segment_key"] != segment_key:
            raise self.fail("cross_segment", "an annotation spans words of its own segment only")
        count = len(tokenize(self.segment(segment_key)["text"]))
        if not 0 <= span["start"]["word"] <= span["end"]["word"] < count:
            raise self.fail(
                "anchor_out_of_range", f"words {span['start']['word']}..{span['end']['word']} of {segment_key}"
            )
        if ann.type not in self.ctx.vocab.annotation_tags:
            raise self.fail("unknown_vocab", f"{ann.type!r} is not an annotation type")
        self.check_vocab(f"annotation_tag.{ann.type}", ann.tag, "/annotation/tag")
        if ann.type == "inserted_disfluency" and any(lock["group"] == "script" for lock in self.doc["locks"]):
            raise self.fail("wording_locked", "inserted disfluencies need unlocked wording")
        key = ann.key or self.new_key(KeyKind.ANNOTATION)
        self.segment(segment_key)["annotations"].append(
            {
                "key": key,
                "type": ann.type,
                "tag": ann.tag,
                "span": span,
                "source": "user_edit",
                "pause_ms": ann.pause_ms,
                "respelling": ann.respelling,
            }
        )

    def op_remove_annotation(self, op: RemoveAnnotation) -> None:
        segment = self.segment(op.segment_key)
        kept = [a for a in segment["annotations"] if a["key"] != op.annotation_key]
        if len(kept) == len(segment["annotations"]):
            raise self.fail("unknown_key", f"no annotation {op.annotation_key} on {op.segment_key}")
        segment["annotations"] = kept

    def op_set_annotation(self, op: SetAnnotation) -> None:
        segment = self.segment(op.segment_key)
        found = [a for a in segment["annotations"] if a["key"] == op.annotation_key]
        if not found:
            raise self.fail("unknown_key", f"no annotation {op.annotation_key} on {op.segment_key}")
        ann = found[0]
        if op.changes.tag is not None:
            self.check_vocab(f"annotation_tag.{ann['type']}", op.changes.tag, "/changes/tag")
            ann["tag"] = op.changes.tag
        if op.changes.span is not None:
            ann["span"] = op.changes.span.model_dump(mode="json")
        if op.changes.pause_ms is not None:
            ann["pause_ms"] = op.changes.pause_ms
        if op.changes.respelling is not None:
            ann["respelling"] = op.changes.respelling
        ann["source"] = "user_edit"

    # ------------------------------------------------------------------ world (I6: never World DNA)
    def _map_binding(self, scene: Doc, binding: Doc, world: Mapping[str, Any]) -> Doc:
        """A binding moved to another world version: camera position, time of day, weather,
        overrides and the placement made valid for it (each change noted)."""
        positions = [c for c in world.get("camera_positions", []) if c.get("status", "permitted") == "permitted"]
        keys = [c["key"] for c in positions]
        profiles = {sh["camera"]["profile_id"] for sh in scene["shots"] if sh["type"] == "talking_head"}
        if binding["camera_position_key"] not in keys and positions:
            fitting = [c for c in positions if profiles & set(c.get("allowed_camera_profiles", []))]
            chosen = (fitting or positions)[0]["key"]
            self.note(
                f"{scene['key']}: camera position {binding['camera_position_key']} → {chosen} (not in the new world)"
            )
            binding["camera_position_key"] = chosen
        tw = world.get("time_and_weather", {})
        if tw.get("allowed_times") and binding["time_of_day"] not in tw["allowed_times"]:
            binding["time_of_day"] = tw.get("default_time_of_day", tw["allowed_times"][0])
            self.note(f"{scene['key']}: time of day → {binding['time_of_day']} (allowed in the new world)")
        if tw.get("allowed_weather") and binding["weather"] not in tw["allowed_weather"]:
            binding["weather"] = tw.get("default_weather", tw["allowed_weather"][0])
            self.note(f"{scene['key']}: weather → {binding['weather']}")
        elements = {e["key"] for e in world.get("elements", [])}
        overrides = binding["overrides"]
        dropped = sorted(
            {k for k in overrides["element_states"] if k not in elements}
            | {k for k in overrides["hide_elements"] if k not in elements}
            | {m["key"] for m in overrides["move_elements"] if m["key"] not in elements}
        )
        if dropped:
            overrides["element_states"] = {k: v for k, v in overrides["element_states"].items() if k in elements}
            overrides["hide_elements"] = [k for k in overrides["hide_elements"] if k in elements]
            overrides["move_elements"] = [m for m in overrides["move_elements"] if m["key"] in elements]
            self.note(f"{scene['key']}: overrides of elements absent from the new world dropped: {', '.join(dropped)}")
        ref = binding.get("continuity_ref")
        if ref and ref.get("kind") == "world_plate":
            binding["continuity_ref"] = {"kind": "world_plate", "camera_position_key": binding["camera_position_key"]}
        zones = world.get("zones", [])
        for member in scene["cast"]:
            if member.get("placement") and member["placement"] not in {z["key"] for z in zones} and zones:
                posture = member.get("default_posture")
                fitting_zones = [z for z in zones if posture in z.get("allowed_postures", [])]
                chosen = (fitting_zones or zones)[0]["key"]
                self.note(f"{scene['key']}: {member['character_key']} placed at {chosen} (zone of the new world)")
                member["placement"] = chosen
        acting = scene.get("acting")
        if acting:
            for state in acting["states"]:
                target = state.get("attention_target")
                if isinstance(target, str) and target.startswith("el_") and target not in elements:
                    state["attention_target"] = "camera"
                    self.note(f"{state['key']}: attention target {target} is not in the new world → camera")
            for event in acting["events"]:
                if (
                    isinstance(event.get("target"), str)
                    and event["target"].startswith("el_")
                    and event["target"] not in elements
                ):
                    self.note(f"{event['key']}: target {event['target']} is not in the new world (cleared)")
                    event["target"] = None
        return binding

    def op_set_world_binding(self, op: SetWorldBinding) -> None:
        if op.world_query is not None and op.world_version_id is None:
            raise self.fail("unresolved_reference", f"world {op.world_query!r} was not resolved to a world version")
        for scene in self.scope_scenes(op.scope):
            binding = copy.deepcopy(scene.get("world"))
            if binding is None:
                if op.world_version_id is None:
                    raise self.fail("no_world", f"scene {scene['key']} has no world; name one")
                binding = {
                    "world_version_id": str(op.world_version_id),
                    "camera_position_key": op.camera_position_key or "cam_default",
                    "time_of_day": op.time_of_day or "midday",
                    "weather": op.weather or "clear",
                    "overrides": {
                        "element_states": {},
                        "hide_elements": [],
                        "add_elements": [],
                        "move_elements": [],
                        "lighting": None,
                        "acoustics": None,
                    },
                    "continuity_ref": None,
                }
            if op.world_version_id is not None and str(op.world_version_id) != binding["world_version_id"]:
                binding["world_version_id"] = str(op.world_version_id)
                world = self.world(str(op.world_version_id))
                if world is not None:
                    binding = self._map_binding(scene, binding, world)
            if op.camera_position_key is not None:
                binding["camera_position_key"] = op.camera_position_key
                ref = binding.get("continuity_ref")
                if ref and ref.get("kind") == "world_plate":
                    binding["continuity_ref"] = {"kind": "world_plate", "camera_position_key": op.camera_position_key}
            if op.time_of_day is not None:
                binding["time_of_day"] = op.time_of_day
            if op.weather is not None:
                binding["weather"] = op.weather
            if op.continuity_ref is not None:
                binding["continuity_ref"] = op.continuity_ref.model_dump(mode="json")
            world = self.world(binding["world_version_id"])
            if world is not None:
                self._check_binding(scene, binding, world)
            scene["world"] = binding

    def _check_binding(self, scene: Doc, binding: Doc, world: Mapping[str, Any]) -> None:
        base = f"/scenes[{scene['key']}]/world"
        positions = {c["key"]: c for c in world.get("camera_positions", [])}
        position = positions.get(binding["camera_position_key"])
        if position is None or position.get("status", "permitted") != "permitted":
            raise self.fail("camera_position", f"{binding['camera_position_key']} is not a permitted position", base)
        tw = world.get("time_and_weather", {})
        if tw.get("allowed_times") and binding["time_of_day"] not in tw["allowed_times"]:
            raise self.fail("time_of_day", f"{binding['time_of_day']} is not allowed in this world", base)
        if tw.get("allowed_weather") and binding["weather"] not in tw["allowed_weather"]:
            raise self.fail("weather", f"{binding['weather']} is not allowed in this world", base)

    def op_set_world_override(self, op: SetWorldOverride) -> None:
        for scene in self.scope_scenes(op.scope):
            if scene.get("world") is None:
                raise self.fail("no_world", f"scene {scene['key']} has no world binding")
            overrides = scene["world"]["overrides"]
            world = self.world(scene["world"]["world_version_id"])
            elements = {e["key"]: e for e in world.get("elements", [])} if world else {}
            base = f"/scenes[{scene['key']}]/world/overrides"
            for key, state in op.element_states.items():
                if elements and key not in elements:
                    raise self.fail("unknown_element", f"{key} is not an element of this world", base)
                if state is None:
                    overrides["element_states"].pop(key, None)
                    continue
                definition = elements.get(key)
                if definition is not None and state not in (definition.get("states") or []):
                    raise self.fail("element_state", f"{key} has no state {state!r}", base)
                overrides["element_states"][key] = state
            for key in op.hide:
                if elements and key not in elements:
                    raise self.fail("unknown_element", f"{key} is not an element of this world", base)
                if key not in overrides["hide_elements"]:
                    overrides["hide_elements"].append(key)
            overrides["hide_elements"] = [k for k in overrides["hide_elements"] if k not in op.unhide]
            existing_added = {a["key"] for a in overrides["add_elements"]}
            for added in op.add_elements:
                if added.key in existing_added:
                    raise self.fail("duplicate_key", f"{added.key} is already added", base)
                overrides["add_elements"].append(added.model_dump(mode="json"))
            overrides["add_elements"] = [a for a in overrides["add_elements"] if a["key"] not in op.remove_added]
            for moved in op.move_elements:
                definition = elements.get(moved.key)
                if elements and (definition is None or definition.get("mutability") != "movable"):
                    raise self.fail("not_movable", f"{moved.key} is not a movable element", base)
                overrides["move_elements"] = [m for m in overrides["move_elements"] if m["key"] != moved.key]
                overrides["move_elements"].append(moved.model_dump(mode="json"))
            if op.clear_lighting:
                overrides["lighting"] = None
            if op.lighting is not None:
                overrides["lighting"] = op.lighting.model_dump(mode="json")
            if op.clear_acoustics:
                overrides["acoustics"] = None
            if op.acoustics is not None:
                for token in op.acoustics.ambient_additions:
                    self.check_vocab("ambient_profile", token, f"{base}/acoustics")
                overrides["acoustics"] = op.acoustics.model_dump(mode="json")

    def op_set_wardrobe(self, op: SetWardrobe) -> None:
        if op.wardrobe_version_id is None:
            raise self.fail("unresolved_reference", f"wardrobe {op.wardrobe_query!r} was not resolved")
        character = op.character_key or self.only_character(op.scope)
        for scene in self.scope_scenes(op.scope):
            speakers = {self.segment(k)["speaker_key"] for k in scene["segment_keys"]}
            members = [m for m in scene["cast"] if m["character_key"] == character]
            if not members and character not in speakers:
                continue
            if members:
                members[0]["wardrobe_version_id"] = str(op.wardrobe_version_id)
            else:
                scene["cast"].append(
                    {
                        "character_key": character,
                        "wardrobe_version_id": str(op.wardrobe_version_id),
                        "placement": None,
                        "default_posture": None,
                    }
                )

    def op_set_cast(self, op: SetCast) -> None:
        character = op.character_key or self.only_character()
        found = [m for m in self.doc["cast"] if m["key"] == character]
        if not found:
            raise self.fail("unknown_key", f"no cast member {character}", "/cast")
        member = found[0]
        if op.voice_query is not None and op.voice_version_id is None:
            raise self.fail("unresolved_reference", f"voice {op.voice_query!r} was not resolved")
        if op.clear_appearance:
            member["overrides"]["appearance_version_id"] = None
        if op.appearance_version_id is not None:
            member["overrides"]["appearance_version_id"] = str(op.appearance_version_id)
        if op.clear_voice:
            member["overrides"]["voice_version_id"] = None
        if op.voice_version_id is not None:
            member["overrides"]["voice_version_id"] = str(op.voice_version_id)
        if op.clear_voice_prosody:
            member["voice_prosody"] = None
        if op.voice_prosody is not None:
            member["voice_prosody"] = op.voice_prosody.model_dump(mode="json")
        if op.role is not None:
            member["role"] = op.role

    def op_set_camera(self, op: SetCamera) -> None:
        if op.scope.shot_keys is not None:
            shots = [self.shot(k) for k in op.scope.shot_keys]
        else:
            shots = [
                (scene, shot)
                for scene in self.scope_scenes(op.scope)
                for shot in scene["shots"]
                if shot["type"] == "talking_head"
            ]
        if not shots:
            raise self.fail("empty_scope", "no shot is in the edit's scope")
        if (
            op.profile_id is not None
            and self.ctx.camera_profiles is not None
            and op.profile_id not in self.ctx.camera_profiles
        ):
            raise self.fail("unknown_camera_profile", f"no camera profile {op.profile_id}")
        self.check_vocab("camera.framing", op.framing, "/framing")
        self.check_vocab("camera.angle", op.angle, "/angle")
        for scene, shot in shots:
            camera = shot["camera"]
            if op.profile_id is not None:
                camera["profile_id"] = op.profile_id
            if op.framing is not None:
                camera["framing"] = op.framing
            if op.angle is not None:
                camera["angle"] = op.angle
            camera["moves"] = [
                m for m in camera["moves"] if m["key"] not in op.remove_moves and m["type"] not in op.remove_move_types
            ]
            for move in op.add_moves:
                if move.at_ref is not None:
                    raise self.fail("unresolved_reference", f"anchor {move.at_ref} was not resolved")
                self.check_vocab("camera.move_type", move.type, "/add_moves/type")
                self.check_vocab("camera.transition", move.transition, "/add_moves/transition")
                at = move.at.model_dump(mode="json") if move.at else None
                if at is None:
                    if shot["span"].get("kind") != "words":
                        raise self.fail("missing_anchor", f"shot {shot['key']} has no word span; anchor the move")
                    at = dict(shot["span"]["start"])
                self.position(scene, at)
                camera["moves"].append(
                    {
                        "key": move.key or self.new_key(KeyKind.CAMERA_MOVE),
                        "type": move.type,
                        "at": at,
                        "scale": move.scale,
                        "transition": move.transition,
                        "derived_from": [],
                    }
                )
            if op.camera_position_key is not None:
                if scene.get("world") is None:
                    raise self.fail("no_world", f"scene {scene['key']} has no world binding")
                scene["world"]["camera_position_key"] = op.camera_position_key
                ref = scene["world"].get("continuity_ref")
                if ref and ref.get("kind") == "world_plate":
                    scene["world"]["continuity_ref"] = {
                        "kind": "world_plate",
                        "camera_position_key": op.camera_position_key,
                    }

    def op_set_pacing(self, op: SetPacing) -> None:
        for scene in self.scope_scenes(op.scope):
            pacing = dict(scene.get("pacing") or {"target_wpm_delta": 0.0, "cut_cadence": None})
            if op.target_wpm_delta is not None:
                pacing["target_wpm_delta"] = op.target_wpm_delta
            if op.clear_cut_cadence:
                pacing["cut_cadence"] = None
            if op.cut_cadence is not None:
                pacing["cut_cadence"] = op.cut_cadence
            scene["pacing"] = pacing if pacing != {"target_wpm_delta": 0.0, "cut_cadence": None} else None
            if op.pause_scale is not None:
                for key in scene["segment_keys"]:
                    for ann in self.segment(key)["annotations"]:
                        if ann["type"] != "pause":
                            continue
                        base = ann.get("pause_ms") or self.ctx.vocab.pause_ms.get(ann["tag"], 300)
                        ann["pause_ms"] = max(1, round(base * op.pause_scale))
                        ann["source"] = "user_edit"

    def op_edit_script(self, op: EditScript) -> None:
        if any(lock["group"] == "script" for lock in self.doc["locks"]):
            raise self.fail(
                "wording_locked",
                "The script wording is locked (the `script` lock): unlock it to edit the text.",
                f"/script/segments[{op.segment_key}]/text",
            )
        segment = self.segment(op.segment_key)
        old = segment["text"]
        if old == op.text:
            return
        rebased, report = rebase_segment(self.doc, op.segment_key, old, op.text)
        self.doc.clear()
        self.doc.update(rebased)
        self.segment(op.segment_key)["text"] = op.text
        self.out.rebase.append({"segment_key": op.segment_key, **report.as_dict()})
        for dropped in report.dropped:
            self.note(f"{dropped['path']} dropped: its words were deleted")

    # ------------------------------------------------------------------ structure
    def _renumber(self) -> None:
        for order, scene in enumerate(sorted(self.doc["scenes"], key=lambda s: s["order"]), start=1):
            scene["order"] = order
        self._sync_script_order()

    def _sync_script_order(self) -> None:
        order = [k for scene in self.scenes() for k in scene["segment_keys"]]
        by_key = {s["key"]: s for s in self.doc["script"]["segments"]}
        rest = [s for s in self.doc["script"]["segments"] if s["key"] not in order]
        self.doc["script"]["segments"] = [by_key[k] for k in order if k in by_key] + rest

    def op_add_scene(self, op: AddScene) -> None:
        scene = op.scene.model_dump(mode="json")
        existing = self.keys()
        new_keys = [scene["key"], *(s.key for s in op.segments)]
        clash = [k for k in new_keys if k in existing]
        if clash:
            raise self.fail("duplicate_key", f"keys already in use: {clash}")
        for segment in op.segments:
            self.doc["script"]["segments"].append(segment.model_dump(mode="json"))
        after = self.scene(op.after_scene_key)["order"] if op.after_scene_key else 0
        for other in self.doc["scenes"]:
            if other["order"] > after:
                other["order"] += 1
        scene["order"] = after + 1
        self.doc["scenes"].append(scene)
        self._renumber()

    def op_remove_scene(self, op: RemoveScene) -> None:
        scene = self.scene(op.scene_key)
        if len(self.doc["scenes"]) == 1:
            raise self.fail("last_scene", "a video keeps at least one scene")
        segments = set(scene["segment_keys"])
        shots = {s["key"] for s in scene["shots"]}
        self.doc["scenes"] = [s for s in self.doc["scenes"] if s["key"] != op.scene_key]
        self.doc["script"]["segments"] = [s for s in self.doc["script"]["segments"] if s["key"] not in segments]
        cues = self.doc["audio"]["music"]["cues"]
        kept_cues = [
            c
            for c in cues
            if not (c["span"].get("kind") == "scene" and c["span"].get("scene_key") == op.scene_key)
            and not (c["span"].get("kind") == "words" and c["span"]["start"]["segment_key"] in segments)
        ]
        if len(kept_cues) != len(cues):
            self.note(f"music cues of {op.scene_key} removed")
        self.doc["audio"]["music"]["cues"] = kept_cues
        sfx = self.doc["audio"]["sfx"]
        kept_sfx = [
            e for e in sfx if e["at"].get("shot_key") not in shots and e["at"].get("segment_key") not in segments
        ]
        if len(kept_sfx) != len(sfx):
            self.note(f"sound effects anchored in {op.scene_key} removed")
        self.doc["audio"]["sfx"] = kept_sfx
        self._renumber()

    def op_move_scene(self, op: MoveScene) -> None:
        scene = self.scene(op.scene_key)
        others = [s for s in self.scenes() if s["key"] != op.scene_key]
        position = (
            0
            if op.after_scene_key is None
            else next((i + 1 for i, s in enumerate(others) if s["key"] == op.after_scene_key), None)
        )
        if position is None:
            raise self.fail("unknown_key", f"no scene {op.after_scene_key}")
        ordered = [*others[:position], scene, *others[position:]]
        for order, item in enumerate(ordered, start=1):
            item["order"] = order
        self._sync_script_order()

    def op_add_shot(self, op: AddShot) -> None:
        scene = self.scene(op.scene_key)
        shot = op.shot.model_dump(mode="json")
        if shot["key"] in self.keys():
            raise self.fail("duplicate_key", f"key {shot['key']} exists")
        scene["shots"].append(shot)

    def op_remove_shot(self, op: RemoveShot) -> None:
        scene, _ = self.shot(op.shot_key, op.scene_key)
        scene["shots"] = [s for s in scene["shots"] if s["key"] != op.shot_key]
        sfx = self.doc["audio"]["sfx"]
        self.doc["audio"]["sfx"] = [e for e in sfx if e["at"].get("shot_key") != op.shot_key]
        if len(self.doc["audio"]["sfx"]) != len(sfx):
            self.note(f"sound effects anchored at {op.shot_key} removed")

    def op_split_shot(self, op: SplitShot) -> None:
        scene, shot = self.shot(op.shot_key, op.scene_key)
        rng = self.span_range(scene, shot["span"])
        if rng is None:
            raise self.fail("not_splittable", f"shot {op.shot_key} has no word span")
        words = self.words(scene)
        cut = self.position(scene, op.at.model_dump(mode="json"))
        if not rng[0] < cut <= rng[1]:
            raise self.fail("not_splittable", f"word {op.at.segment_key}:{op.at.word} is not inside {op.shot_key}")
        second = copy.deepcopy(shot)
        second["key"] = op.new_shot_key or self.new_key(KeyKind.SHOT)
        shot["span"] = _span(words[rng[0]], words[cut - 1])
        second["span"] = _span(words[cut], words[rng[1]])
        moves_first: list[Doc] = []
        moves_second: list[Doc] = []
        for move in shot["camera"]["moves"]:
            (moves_second if self.position(scene, move["at"]) >= cut else moves_first).append(move)
        shot["camera"]["moves"] = moves_first
        second["camera"]["moves"] = moves_second
        second["takes"] = {"count": shot["takes"]["count"], "selected_take_key": None}
        index = next(i for i, s in enumerate(scene["shots"]) if s["key"] == shot["key"])
        scene["shots"].insert(index + 1, second)

    def op_set_shot(self, op: SetShot) -> None:
        scene, shot = self.shot(op.shot_key, op.scene_key)
        ch = op.changes
        if ch.type is not None:
            shot["type"] = ch.type
        if ch.layer is not None:
            shot["layer"] = ch.layer
        if ch.span is not None:
            span = ch.span.model_dump(mode="json")
            self.span_range(scene, span)
            shot["span"] = span
        if ch.broll_prompt is not None:
            if shot.get("broll") is None:
                raise self.fail("not_broll", f"shot {op.shot_key} is not a B-roll shot")
            shot["broll"]["prompt"] = ch.broll_prompt
        if ch.reaction_source is not None:
            shot["reaction_source"] = ch.reaction_source.model_dump(mode="json")
        if ch.zooms is not None or ch.speed_segments is not None or ch.webcam_bubble is not None:
            if shot.get("screen") is None:
                raise self.fail("not_screen", f"shot {op.shot_key} is not a screen shot")
            if ch.zooms is not None:
                shot["screen"]["zooms"] = [z.model_dump(mode="json") for z in ch.zooms]
            if ch.speed_segments is not None:
                shot["screen"]["speed_segments"] = [s.model_dump(mode="json") for s in ch.speed_segments]
            if ch.webcam_bubble is not None:
                shot["screen"]["webcam_bubble"] = ch.webcam_bubble.model_dump(mode="json")
        if ch.takes_count is not None:
            shot["takes"]["count"] = ch.takes_count
            selected = shot["takes"].get("selected_take_key")
            if selected and int(selected.removeprefix("tk_")) > ch.takes_count:
                shot["takes"]["selected_take_key"] = None

    # ------------------------------------------------------------------ video level
    def op_set_meta(self, op: SetMeta) -> None:
        meta = self.doc["meta"]
        for name in ("quality_tier", "platform_targets", "primary_aspect", "target_duration_s", "title"):
            value = getattr(op, name)
            if value is not None:
                meta[name] = list(value) if isinstance(value, list) else value
        for template_id in op.add_template_ids:
            if str(template_id) not in meta.setdefault("template_ids", []):
                meta["template_ids"].append(str(template_id))

    def op_set_render_outputs(self, op: SetRenderOutputs) -> None:
        if op.outputs is not None:
            if self.ctx.render_presets is not None:
                unknown = [o.preset_id for o in op.outputs if o.preset_id not in self.ctx.render_presets]
                if unknown:
                    raise self.fail("unknown_preset", f"unknown render presets {unknown}")
            self.doc["render"]["outputs"] = [o.model_dump(mode="json") for o in op.outputs]
        if op.reframe is not None:
            self.doc["render"]["reframe"] = op.reframe.model_dump(mode="json")

    def op_replace_broll(self, op: ReplaceBroll) -> None:
        _, shot = self.shot(op.shot_key, op.scene_key)
        if shot["type"] not in ("broll", "insert", "product"):
            raise self.fail("not_broll", f"shot {op.shot_key} is not a B-roll, insert or product shot")
        shot["broll"] = op.broll.model_dump(mode="json")

    def _list_edit(
        self, items: list[Doc], add: Iterable[Any], remove: Iterable[str], update: Mapping[str, Any], what: str
    ) -> list[Doc]:
        remove = set(remove)
        missing = sorted(remove - {i["key"] for i in items})
        if missing:
            raise self.fail("unknown_key", f"no {what} {missing}")
        out = [i for i in items if i["key"] not in remove]
        for key, changes in update.items():
            found = [i for i in out if i["key"] == key]
            if not found:
                raise self.fail("unknown_key", f"no {what} {key}")
            found[0].update({k: v for k, v in changes.model_dump(mode="json").items() if v is not None})
        existing = self.keys()
        for item in add:
            data = item.model_dump(mode="json")
            if data["key"] in existing:
                raise self.fail("duplicate_key", f"key {data['key']} exists")
            out.append(data)
        return out

    def op_set_music(self, op: SetMusic) -> None:
        music = self.doc["audio"]["music"]
        music["cues"] = self._list_edit(music["cues"], op.add, op.remove, op.update, "music cue")

    def op_set_sfx(self, op: SetSfx) -> None:
        self.doc["audio"]["sfx"] = self._list_edit(
            self.doc["audio"]["sfx"], op.add, op.remove, op.update, "sound effect"
        )

    def op_set_captions(self, op: SetCaptions) -> None:
        for name, value in op.changes.model_dump(mode="json").items():
            if value is not None:
                self.doc["captions"][name] = value

    def op_set_effects(self, op: SetEffects) -> None:
        self.doc["effects"] = self._list_edit(self.doc["effects"], op.add, op.remove, {}, "effect")

    def op_set_brand(self, op: SetBrand) -> None:
        if op.clear_brand_kit:
            self.doc["brand"]["brand_kit_id"] = None
        if op.brand_kit_id is not None:
            self.doc["brand"]["brand_kit_id"] = str(op.brand_kit_id)
        if op.logo_overlay is not None:
            self.doc["brand"]["logo_overlay"] = op.logo_overlay

    def op_set_product(self, op: SetProduct) -> None:
        self.doc["products"] = self._list_edit(self.doc["products"], op.add, op.remove, {}, "product")

    def op_set_provenance_label(self, op: SetProvenanceLabel) -> None:
        self.doc["provenance"]["visible_label"] = op.visible_label

    # ------------------------------------------------------------------ generation control
    def _scope_shot_keys(self, scope: EditScope) -> list[str]:
        if scope.shot_keys is not None:
            return list(scope.shot_keys)
        return [shot["key"] for scene in self.scope_scenes(scope) for shot in scene["shots"]]

    def _node_scene(self, node_key: str) -> tuple[str | None, str | None]:
        """The scene and shot a node key belongs to (from its element key)."""
        parts = node_key.split(":")
        element = parts[1] if len(parts) > 1 else ""
        for scene in self.doc["scenes"]:
            if element == scene["key"]:
                return scene["key"], None
            if element in scene["segment_keys"]:
                return scene["key"], None
            for shot in scene["shots"]:
                if element == shot["key"]:
                    return scene["key"], shot["key"]
        return None, None

    def _new_seed(self, node_key: str, previous: int | None) -> int:
        basis = f"{self.doc['generation']['seed_namespace']}|{node_key}|{previous}|regenerate"
        return int.from_bytes(hashlib.sha256(basis.encode()).digest()[:4], "big") & 0x7FFFFFFF

    def op_regenerate(self, op: Regenerate) -> None:
        scenes = [s["key"] for s in self.scope_scenes(op.scope)]
        shots = self._scope_shot_keys(op.scope)
        refused = regenerate_refusals(
            op.components,
            self.doc["locks"],
            self.ctx.vocab,
            scenes=scenes or [None],
            shots=(op.scope.shot_keys or [None]),
            characters=(op.scope.character_keys or [None]),
            seed_policy=op.seed_policy,
        )
        if refused:
            raise EditError(
                refused[0].message,
                [Issue(code="regenerate_refused", message=r.message, detail=r.as_dict()) for r in refused],
            )
        for name in op.components:
            component = self.ctx.vocab.regenerate_components[name]
            if component.requires_director:
                raise self.fail(
                    "needs_director",
                    f"regenerating {name} needs the Director: it runs as an edit proposal in replan mode",
                )
            if op.seed_policy == "new":
                kinds = set(component.node_kinds)
                matched = 0
                for node_key in self.ctx.node_keys:
                    if node_key.split(":", 1)[0] not in kinds:
                        continue
                    scene, shot = self._node_scene(node_key)
                    if scene not in scenes:
                        continue
                    if op.scope.shot_keys is not None and shot is not None and shot not in op.scope.shot_keys:
                        continue
                    overrides = self.doc["generation"]["seed_overrides"]
                    overrides[node_key] = self._new_seed(node_key, overrides.get(node_key))
                    matched += 1
                if not matched:
                    raise self.fail("empty_scope", f"no {name} node is in the regenerate scope")
        if op.takes is not None:
            for key in shots:
                _, target = self.shot(key)
                if target["type"] in ("talking_head", "broll", "insert", "product"):
                    target["takes"]["count"] = op.takes
        if op.strategy == "lipsync_patch":
            talking = [k for k in shots if self.shot(k)[1]["type"] == "talking_head"]
            self.out.lipsync_patch_shots += [k for k in talking if k not in self.out.lipsync_patch_shots]

    def op_reroute(self, op: Reroute) -> None:
        if self.ctx.node_keys:
            unknown = [k for k in op.node_keys if k not in self.ctx.node_keys]
            if unknown:
                raise self.fail("unknown_node", f"no nodes {unknown} in the version's graph")
        self.out.force_reroute += [k for k in op.node_keys if k not in self.out.force_reroute]
        if op.adapter_id is not None:
            for key in op.node_keys:
                self.doc["generation"]["engine_hints"][key] = op.adapter_id

    def op_select_take(self, op: SelectTake) -> None:
        _, shot = self.shot(op.shot_key)
        if op.take_key is not None and int(op.take_key.removeprefix("tk_")) > shot["takes"]["count"]:
            raise self.fail("unknown_take", f"{op.take_key} exceeds the shot's {shot['takes']['count']} takes")
        shot["takes"]["selected_take_key"] = op.take_key

    def op_set_lock(self, op: SetLock) -> None:
        locks = self.doc["locks"]
        for removal in op.remove:
            if not self.ctx.allow_lock_removal:
                raise self.fail(
                    "lock_removal_forbidden",
                    f"An edit proposed from an instruction cannot remove the `{removal.group}` lock; "
                    "unlock it yourself.",
                    "/locks",
                )
            scope = removal.scope.model_dump(mode="json") if removal.scope is not None else None
            kept = []
            for lock in locks:
                if lock["group"] == removal.group and (scope is None or lock["scope"] == scope):
                    self.out.removed_locks.append(lock)
                else:
                    kept.append(lock)
            if len(kept) == len(locks):
                raise self.fail("unknown_lock", f"no {removal.group} lock with that scope")
            locks[:] = kept
        for lock in op.add:
            if lock.group not in self.ctx.vocab.lock_groups:
                raise self.fail("unknown_vocab", f"{lock.group!r} is not a lock group", "/locks")
            data = lock.model_dump(mode="json")
            data["set_by"] = "system" if self.ctx.actor == "system" else "user"
            if not any(lk["group"] == data["group"] and lk["scope"] == data["scope"] for lk in locks):
                locks.append(data)

    def op_refresh_memory(self, op: RefreshMemory) -> None:
        if not op.snapshot_ids:
            raise self.fail("snapshots_missing", "refresh_memory needs the new snapshots (pinned at proposal time)")
        pins = self.doc["memory"]["snapshots"]
        for character, snapshot in op.snapshot_ids.items():
            found = [p for p in pins if p["character_key"] == character]
            if found:
                found[0]["snapshot_id"] = str(snapshot)
            else:
                pins.append({"character_key": character, "snapshot_id": str(snapshot)})

    def op_memory_feedback(self, op: MemoryFeedback) -> None:
        self.out.side_effects.append(
            {
                "kind": "memory_feedback",
                "character_key": op.character_key or self.only_character(),
                "memory_kind": op.kind,
                "value": op.value,
                "text": op.text,
            }
        )


def _trajectory(doc: Doc) -> list[str]:
    displayed: list[str] = []
    for scene in sorted(doc["scenes"], key=lambda s: s["order"]):
        for state in (scene.get("acting") or {}).get("states", []):
            label = ((state.get("emotion") or {}).get("displayed") or {}).get("label")
            if label and (not displayed or displayed[-1] != label):
                displayed.append(label)
    return displayed


def _is_summary(arc: Sequence[str], trajectory: Sequence[str]) -> bool:
    it = iter(trajectory)
    return all(label in it for label in arc)


def _follow_arc(t: _T, before: Doc) -> None:
    """An acting edit that changes displayed emotions keeps the video's `emotional_arc` a summary of
    the trajectory (§14): an arc that summarized the old trajectory is re-derived from the new one."""
    video = t.doc["intent"]["video"]
    arc = list(video.get("emotional_arc") or [])
    new = _trajectory(t.doc)
    if not arc or not new or _is_summary(arc, new) or not _is_summary(arc, _trajectory(before)):
        return
    video["emotional_arc"] = new
    t.note(f"the video's emotional arc now follows the edited trajectory: {' → '.join(new)}")


def translate(ops: Sequence[EditOperation], spec: Mapping[str, Any], ctx: TranslateContext) -> Translation:
    """Applies resolved operations to a copy of `spec` (JSON form) and returns the patch, the
    patched document and what the edit does outside the spec. Raises `EditError`."""
    doc = copy.deepcopy(dict(spec))
    t = _T(doc, ctx)
    for op in ops:
        t.apply(op)
    _follow_arc(t, dict(spec))
    t.out.document = t.doc
    t.out.patch = patch_from_diff(spec, t.doc)
    return t.out
