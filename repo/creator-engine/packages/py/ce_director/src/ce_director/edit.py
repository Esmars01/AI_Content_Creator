"""Natural-language edits (§28 steps 1–2): reference resolution in code, then one LLM stage (`edit`)
that emits `EditOperation`s against the resolved references, then record queries resolved to
approved records. Translation into a SpecPatch, validation, impact and the coverage delta happen
in `ce_exec.editing`.

**References** ("the first 3 seconds", "the second scene", "him", "when he looks away", the
editor's selection) become slots (`@selection`, `@first_3s`, `@scene_2`, `@him`,
`@look_away`) holding concrete scopes, and anchors (`@span_start`, `@emphasis`, `@look_away`)
holding words. The LLM names slots, never guesses keys; fixtures written against slots therefore
work for any spec (§37: fixtures keyed by scenario, not prompt).

Without a matching fixture (dev and test) a labeled **template planner** handles common phrasings
so the editor keeps working; with `allow_template` off a miss is an error.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from ce_build.refs import BuildRefs
from ce_core.edit.ops import (
    ActingChanges,
    EditOperation,
    EditScope,
    EmotionChange,
    EmotionValueChange,
    EventChanges,
    NewMove,
    Regenerate,
    SetActing,
    SetBehaviorEvent,
    SetCamera,
    SetCast,
    SetLock,
    SetPacing,
    SetWardrobe,
    SetWorldBinding,
)
from ce_core.edit.translate import EditError, resolve_operation
from ce_core.errors import Issue
from ce_core.spec.anchors import WordRef, WordSpan
from ce_core.spec.videospec import Lock, LockScope, VideoSpec
from ce_core.text import tokenize
from ce_llm import FixtureMiss, StructuredOutputError, scenario_key, structured
from pydantic import BaseModel, Field

from ce_director.director import DirectorDeps
from ce_director.runs import RunLog, StageRun
from ce_director.vocabmap import ControlField, VocabMapper

__all__ = [
    "EditContext",
    "EditDirector",
    "EditPlan",
    "EditPlanOut",
    "EditRequest",
    "RecordOption",
    "References",
    "Selection",
    "edit_scenario",
    "resolve_references",
]

_FEMALE = re.compile(r"\b(female|woman|she|her|girl|feminine)\b", re.IGNORECASE)
_MALE = re.compile(r"\b(male|man|he|his|guy|boy|masculine)\b", re.IGNORECASE)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "1st": 1, "2nd": 2, "3rd": 3}
_EVENT_WORDS = {
    "looks away": "look_away",
    "look away": "look_away",
    "smiles": "small_smile",
    "smile": "small_smile",
    "laughs": "laugh",
    "nods": "nod",
    "raises his eyebrow": "eyebrow_raise",
    "raises her eyebrow": "eyebrow_raise",
}
EDIT_CONTROLS = [
    ControlField("operations[].changes.emotion.displayed.label", "emotion"),
    ControlField("operations[].changes.emotion.felt.label", "emotion"),
    *(ControlField(f"operations[].changes.strategies.{c}", f"strategy.{c}") for c in (
        "prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness"
    )),
    ControlField("operations[].changes.internal_state", "internal_state"),
    ControlField("operations[].changes.social_goal", "social_goal"),
    ControlField("operations[].changes.audience_goal", "audience_goal"),
    ControlField("operations[].changes.performance_intent", "performance_intent"),
    ControlField("operations[].event.type", "event_type"),
    ControlField("operations[].event.direction", "direction"),
    ControlField("operations[].event.purpose", "event_purpose"),
    ControlField("operations[].event_type", "event_type"),
    ControlField("operations[].add_moves[].type", "camera.move_type"),
    ControlField("operations[].framing", "camera.framing"),
    ControlField("operations[].angle", "camera.angle"),
]  # fmt: skip


class Selection(BaseModel):
    """The editor's selection (§30 `POST /v1/versions/{id}/edits`)."""

    time_range_s: tuple[float, float] | None = None
    scene_keys: list[str] | None = None
    shot_keys: list[str] | None = None
    character_keys: list[str] | None = None


class EditRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)
    selection: Selection | None = None


@dataclass(frozen=True)
class RecordOption:
    """An approved record an edit may switch to (world, wardrobe or voice version)."""

    kind: Literal["world", "wardrobe", "voice"]
    version_id: UUID
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    owner: str | None = None  # the creator it belongs to (wardrobes and voices)
    accent: str | None = None


@dataclass
class EditContext:
    spec: VideoSpec
    refs: BuildRefs
    word_times: Mapping[str, Sequence[tuple[float, float]]] | None = None  # segment → (start, end) on the timeline
    timing_source: Literal["measured", "estimated"] = "estimated"
    options: list[RecordOption] = field(default_factory=list)


@dataclass
class References:
    slots: dict[str, list[EditScope]] = field(default_factory=dict)
    anchors: dict[str, dict[str, Any]] = field(default_factory=dict)
    table: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, slot: str, scopes: list[EditScope], description: str) -> None:
        self.slots[slot] = scopes
        self.table.append(
            {
                "slot": slot,
                "describes": description,
                "scopes": [s.model_dump(mode="json", exclude_none=True) for s in scopes],
            }
        )

    def anchor(self, slot: str, ref: WordRef, description: str) -> None:
        self.anchors[slot] = ref.model_dump(mode="json")
        self.table.append({"slot": slot, "describes": description, "word": self.anchors[slot]})


class EditPlanOut(BaseModel):
    operations: list[EditOperation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


@dataclass
class EditPlan:
    operations: list[EditOperation]
    assumptions: list[str]
    planner: Literal["llm", "fixture", "template", "structured"]
    references: References
    runs: list[StageRun] = field(default_factory=list)


def edit_scenario(instruction: str) -> str:
    """The fixture scenario of an instruction (case, spacing and final punctuation do not matter)."""
    normalized = re.sub(r"\s+", " ", instruction).strip().rstrip(".!?").lower()
    return scenario_key(f"edit: {normalized}")


# ---------------------------------------------------------------------- references (§28 step 1)


def _words_in_order(spec: VideoSpec) -> list[tuple[str, str, int]]:
    """(scene key, segment key, word) in timeline order."""
    out: list[tuple[str, str, int]] = []
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        for key in scene.segment_keys:
            out += [(scene.key, key, t.index) for t in tokenize(spec.script.segment(key).text)]
    return out


def _time_scopes(
    spec: VideoSpec, word_times: Mapping[str, Sequence[tuple[float, float]]], start_s: float, end_s: float
) -> list[EditScope]:
    """One scope per scene: the words that start inside [start_s, end_s) (at least the first one)."""
    by_scene: dict[str, list[tuple[str, int]]] = {}
    for scene_key, segment, word in _words_in_order(spec):
        times = word_times.get(segment)
        if times is None or word >= len(times):
            continue
        if start_s <= times[word][0] < end_s:
            by_scene.setdefault(scene_key, []).append((segment, word))
    if not by_scene:
        first = _words_in_order(spec)[:1]
        by_scene = {first[0][0]: [(first[0][1], first[0][2])]} if first else {}
    return [
        EditScope(
            scene_keys=[scene],
            span=WordSpan(
                start=WordRef(segment_key=words[0][0], word=words[0][1]),
                end=WordRef(segment_key=words[-1][0], word=words[-1][1]),
            ),
        )
        for scene, words in by_scene.items()
    ]


def _gender(spec: VideoSpec, refs: BuildRefs, character: str) -> str:
    member = next((c for c in spec.cast if c.key == character), None)
    if member is None:
        return "unspecified"
    texts: list[str] = []
    creator = refs.creators.get(member.creator_version_id)
    if creator is not None:
        voice_id = member.overrides.voice_version_id or creator.voice_version_id
        voice = refs.voices.get(voice_id) if voice_id else None
        if voice is not None:
            texts.append(voice.description)
        appearance_id = member.overrides.appearance_version_id or creator.appearance_version_id
        appearance = refs.appearances.get(appearance_id) if appearance_id else None
        if appearance is not None:
            texts.append(" ".join(str(v) for v in appearance.dna.values() if isinstance(v, str)))
    text = " ".join(texts)
    if _FEMALE.search(text):
        return "female"
    if _MALE.search(text):
        return "male"
    return "unspecified"


def resolve_references(
    instruction: str,
    selection: Selection | None,
    spec: VideoSpec,
    refs: BuildRefs,
    word_times: Mapping[str, Sequence[tuple[float, float]]] | None,
) -> References:
    out = References()
    text = instruction.lower()
    scenes = sorted(spec.scenes, key=lambda s: s.order)

    # the editor's selection
    if selection is not None:
        scopes: list[EditScope] = []
        if selection.time_range_s is not None and word_times:
            narrow = {"shot_keys": selection.shot_keys, "character_keys": selection.character_keys}
            scopes = [
                scope.model_copy(update={k: v for k, v in narrow.items() if v})
                for scope in _time_scopes(spec, word_times, *selection.time_range_s)
            ]
        elif selection.scene_keys or selection.shot_keys or selection.character_keys:
            scopes = [
                EditScope(
                    scene_keys=selection.scene_keys,
                    shot_keys=selection.shot_keys,
                    character_keys=selection.character_keys,
                )
            ]
        if scopes:
            out.add("@selection", scopes, "what the editor selected")

    # the first / last N seconds
    match = re.search(r"\b(first|last)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", text)
    if match and word_times:
        seconds = float(match.group(2))
        total = max((t[-1][1] for t in word_times.values() if t), default=0.0)
        start, end = (0.0, seconds) if match.group(1) == "first" else (max(0.0, total - seconds), total + 1.0)
        slot = f"@{match.group(1)}_{match.group(2).replace('.', '_')}s"
        out.add(slot, _time_scopes(spec, word_times, start, end), f"the {match.group(1)} {match.group(2)} seconds")
        out.notes.append(f"“{match.group(0)}” resolved on {'measured' if word_times else 'estimated'} word timings")

    # ordinal scenes
    for m in re.finditer(r"\b(?:(first|second|third|fourth|fifth|1st|2nd|3rd|last)\s+scene|scene\s+(\d+))\b", text):
        index = len(scenes) if m.group(1) == "last" else _ORDINALS.get(m.group(1) or "", int(m.group(2) or 0))
        if 1 <= index <= len(scenes):
            out.add(f"@scene_{index}", [EditScope(scene_keys=[scenes[index - 1].key])], m.group(0))

    # pronouns → characters
    characters = [c.key for c in spec.cast]
    for pronoun, wanted in (("him", "male"), ("he", "male"), ("his", "male"), ("her", "female"), ("she", "female")):
        if not re.search(rf"\b{pronoun}\b", text):
            continue
        matching = [c for c in characters if _gender(spec, refs, c) == wanted]
        chosen = matching or (characters if len(characters) == 1 else [])
        if not chosen:
            continue
        slot = "@him" if wanted == "male" else "@her"
        if slot not in out.slots:
            out.add(slot, [EditScope(character_keys=chosen)], f"“{pronoun}”")
            if not matching:
                out.notes.append(f"“{pronoun}” taken as {chosen[0]}, the only cast member")
    if len(characters) == 1 and "@cast" not in out.slots:
        out.add("@cast", [EditScope(character_keys=characters)], "the only cast member")

    # events ("when he looks away")
    for phrase, event_type in _EVENT_WORDS.items():
        if phrase not in text:
            continue
        for scene in scenes:
            for event in scene.acting.events if scene.acting else []:
                if event.type == event_type and event.at is not None:
                    slot = f"@{event_type}"
                    if slot not in out.anchors:
                        out.anchor(slot, event.at, f"when the {event_type.replace('_', ' ')} happens ({event.key})")
                        out.add(
                            slot,
                            [EditScope(scene_keys=[scene.key], span=WordSpan(start=event.at, end=event.at))],
                            phrase,
                        )

    # anchors inside the selection (or the whole video): its first word and its emphasized word
    primary = out.slots.get("@selection") or next(
        (v for k, v in out.slots.items() if k.startswith(("@first_", "@last_", "@scene_"))), None
    )
    scope_scenes = ([spec.scene(k) for s in primary for k in (s.scene_keys or [])] if primary else scenes) or scenes
    first_scene = scope_scenes[0]
    span = primary[0].span if primary and primary[0].span else None
    if span is not None:
        out.anchor("@span_start", span.start, "the first word of the referenced range")
    elif first_scene.segment_keys:
        out.anchor("@span_start", WordRef(segment_key=first_scene.segment_keys[0], word=0), "the scene's first word")
    emphasis = None
    for scene in scope_scenes:
        for key in scene.segment_keys:
            for ann in spec.script.segment(key).annotations:
                if str(ann.type) == "emphasis" and emphasis is None:
                    emphasis = ann.span.start
    if emphasis is None and first_scene.segment_keys:
        last_key = first_scene.segment_keys[-1]
        count = len(tokenize(spec.script.segment(last_key).text))
        emphasis = WordRef(segment_key=last_key, word=max(0, count - 1))
    if emphasis is not None:
        out.anchor("@emphasis", emphasis, "the key (emphasized) word of the referenced scene — the claim word")
    return out


# ---------------------------------------------------------------------- record queries


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in {"the", "and", "with"}}


def _pick(
    options: Sequence[RecordOption], query: str, *, exclude: set[UUID], prefer_other: bool
) -> RecordOption | None:
    wanted = _tokens(query)
    scored: list[tuple[int, int, str, RecordOption]] = []
    for option in options:
        if option.version_id in exclude:
            continue
        haystack = _tokens(" ".join([option.name, option.description, *option.tags, option.accent or ""]))
        score = len(wanted & haystack)
        scored.append((score, 0, str(option.version_id), option))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    best = scored[0]
    if best[0] == 0 and not prefer_other:
        return None
    return best[3]


def resolve_queries(ops: list[EditOperation], ctx: EditContext, note: Callable[[str], None]) -> list[EditOperation]:
    """`world_query`, `wardrobe_query` and `voice_query` → approved version ids (code, not the LLM)."""
    spec = ctx.spec
    out: list[EditOperation] = []
    for op in ops:
        if isinstance(op, SetWorldBinding) and op.world_query and op.world_version_id is None:
            current = {s.world.world_version_id for s in spec.scenes if s.world is not None}
            worlds = [o for o in ctx.options if o.kind == "world"]
            chosen = _pick(worlds, op.world_query, exclude=current, prefer_other=False)
            if chosen is None:
                raise EditError(
                    f"No approved world matches “{op.world_query}”.",
                    [
                        Issue(
                            code="no_matching_world",
                            message=(
                                f"No approved world matches “{op.world_query}”. Proposing a new world (a drafted "
                                "world with its plates) arrives with World Studio in Phase 10; create and approve "
                                "the world, then ask again."
                            ),
                        )
                    ],
                )
            note(f"“{op.world_query}” → the approved world “{chosen.name}”")
            op = op.model_copy(update={"world_version_id": chosen.version_id, "world_query": None})
        elif isinstance(op, SetWardrobe) and op.wardrobe_query and op.wardrobe_version_id is None:
            character = op.character_key or (spec.cast[0].key if len(spec.cast) == 1 else None)
            worn: set[UUID] = {
                c.wardrobe_version_id
                for s in spec.scenes
                for c in s.cast
                if c.character_key == character and c.wardrobe_version_id is not None
            }
            owner = _owner(spec, ctx.refs, character)
            wardrobes = [o for o in ctx.options if o.kind == "wardrobe" and (owner is None or o.owner in (owner, None))]
            chosen = _pick(wardrobes, op.wardrobe_query, exclude=worn, prefer_other=True)
            if chosen is None:
                raise EditError(
                    "No other approved outfit exists for this creator.",
                    [Issue("no_matching_wardrobe", "No other approved outfit exists for this creator; add one first.")],
                )
            note(f"“{op.wardrobe_query}” → the approved outfit “{chosen.name}”")
            op = op.model_copy(update={"wardrobe_version_id": chosen.version_id, "wardrobe_query": None})
        elif isinstance(op, SetCast) and op.voice_query and op.voice_version_id is None:
            character = op.character_key or (spec.cast[0].key if len(spec.cast) == 1 else None)
            member = next((c for c in spec.cast if c.key == character), None)
            creator = ctx.refs.creators.get(member.creator_version_id) if member else None
            voice_now: UUID | None = (member.overrides.voice_version_id if member else None) or (
                creator.voice_version_id if creator else None
            )
            voices = [o for o in ctx.options if o.kind == "voice"]
            current_accent = next((o.accent for o in voices if o.version_id == voice_now), None)
            if "accent" in op.voice_query.lower() and current_accent is not None:
                voices = [o for o in voices if o.accent != current_accent] or voices
            chosen = _pick(voices, op.voice_query, exclude={voice_now} if voice_now else set(), prefer_other=True)
            if chosen is None:
                raise EditError(
                    "No other approved voice version exists for this creator.",
                    [
                        Issue(
                            "no_matching_voice",
                            "No other approved voice version exists for this creator; voice design arrives with "
                            "Phase 10 (fixture voice versions stand in until then).",
                        )
                    ],
                )
            note(f"“{op.voice_query}” → the approved voice “{chosen.name}”")
            op = op.model_copy(update={"voice_version_id": chosen.version_id, "voice_query": None})
        out.append(op)
    return out


def _owner(spec: VideoSpec, refs: BuildRefs, character: str | None) -> str | None:
    member = next((c for c in spec.cast if c.key == character), None)
    creator = refs.creators.get(member.creator_version_id) if member else None
    return str(creator.creator_id) if creator else None


def _unknown_reference(ref: str) -> str:
    """The proposal card's words for a slot the plan used but this edit does not have."""
    if ref == "@selection":
        return "The edit is about a selection, but nothing is selected: tick the scenes or set a time range."
    return f"The instruction refers to {ref.removeprefix('@').replace('_', ' ')}, which this video does not have."


def expand_slots(ops: list[EditOperation], references: References) -> list[EditOperation]:
    """Resolves every slot; an operation whose slot holds several scopes (a range crossing scenes)
    becomes one operation per scope."""
    out: list[EditOperation] = []
    for op in ops:
        scope = getattr(op, "scope", None)
        ref = scope.ref if isinstance(scope, EditScope) else None
        scopes = references.slots.get(ref, []) if ref else []
        if ref and not scopes:
            raise EditError(f"unknown reference {ref}", [Issue("unknown_reference", _unknown_reference(ref))])
        if ref and len(scopes) > 1:
            out += [resolve_operation(op, {ref: one}, references.anchors) for one in scopes]
            continue
        out.append(resolve_operation(op, {k: v[0] for k, v in references.slots.items() if v}, references.anchors))
    return out


# ---------------------------------------------------------------------- template planner (no LLM)


def _template_plan(instruction: str, references: References, spec: VideoSpec, vocab: Any) -> EditPlanOut:
    """Common phrasings without an LLM (labeled; dev and fixture misses)."""
    text = instruction.lower()
    scope_ref = (
        "@selection"
        if "@selection" in references.slots
        else next((k for k in references.slots if k.startswith(("@first_", "@last_", "@scene_"))), None)
    )
    who = "@him" if "@him" in references.slots else "@her" if "@her" in references.slots else None
    scope = EditScope(ref=scope_ref) if scope_ref else EditScope()
    if who and not scope_ref:
        scope = EditScope(ref=who)
    ops: list[EditOperation] = []
    assumptions = ["Planned by the labeled template edit planner (no recorded LLM response matches)."]
    m = re.search(r"\b(more|less)\s+([a-z]+)", text)
    if "regenerate" in text and "acting" in text:
        # Without an LLM there is no new acting plan to write: the plan stays and the performance
        # is re-rendered with new seeds (labeled, so the proposal says what happened).
        ops.append(Regenerate(scope=scope, components=["avatar_video"], seed_policy="new"))
        assumptions.append("Without an LLM the acting plan is kept; the performance is re-rendered with new seeds.")
    elif "handheld" in text:
        ops.append(
            SetCamera(
                scope=EditScope(),
                add_moves=[NewMove(type="handheld_drift", scale=0.6 if "slight" in text else 1.0, transition="smooth")],
            )
        )
    elif "outfit" in text or "wardrobe" in text or "clothes" in text:
        ops.append(SetWardrobe(scope=EditScope(), wardrobe_query=instruction))
        if "face" in text:
            ops.append(SetLock(add=[Lock(group="appearance", scope=LockScope())]))
    elif "accent" in text or "voice" in text:
        ops.append(SetCast(voice_query=instruction))
    elif "room" in text or "background" in text or "office" in text or "set " in text:
        target = re.sub(r"^.*\b(?:to|into)\s+(?:a|an|the)?\s*", "", text) or instruction
        ops.append(SetWorldBinding(scope=EditScope(), world_query=target))
    elif "faster" in text or "slower" in text:
        ops.append(SetPacing(scope=scope, target_wpm_delta=0.1 if "faster" in text else -0.1))
    elif "smile" in text and m is not None:
        delta = -0.2 if m.group(1) == "less" else 0.2
        if any(e.type == "small_smile" for s in spec.scenes for e in (s.acting.events if s.acting else [])):
            ops.append(
                SetBehaviorEvent(scope=scope, event_type="small_smile", changes=EventChanges(intensity_delta=delta))
            )
        ops.append(
            SetActing(
                scope=scope, changes=ActingChanges(strategies={"reaction": "suppressed" if delta < 0 else "open"})
            )
        )
    elif m is not None and m.group(2) in vocab.emotions:
        delta = 0.2 if m.group(1) == "more" else -0.2
        label = m.group(2) if delta > 0 else None
        ops.append(
            SetActing(
                scope=scope,
                changes=ActingChanges(
                    emotion=EmotionChange(displayed=EmotionValueChange(label=label, intensity_delta=delta))
                ),
            )
        )
    else:
        raise EditError(
            "The template edit planner does not understand this instruction.",
            [Issue("not_understood", "Without an LLM only common edits are understood; rephrase or use operations.")],
        )
    return EditPlanOut(operations=ops, assumptions=assumptions)


# ---------------------------------------------------------------------- the stage


class EditDirector:
    def __init__(self, deps: DirectorDeps) -> None:
        self.deps = deps
        self.bundle = deps.bundle
        self.vocab = deps.bundle.vocab
        self.mapper = VocabMapper(self.vocab)

    def _video_summary(self, spec: VideoSpec) -> dict[str, Any]:
        scenes = []
        for scene in sorted(spec.scenes, key=lambda s: s.order):
            scenes.append(
                {
                    "key": scene.key,
                    "order": scene.order,
                    "purpose": scene.purpose,
                    "segments": {k: spec.script.segment(k).text for k in scene.segment_keys},
                    "states": [
                        {
                            "key": s.key,
                            "character": s.character_key,
                            "displayed": s.emotion.displayed.model_dump(mode="json") if s.emotion else None,
                            "strategies": s.strategies.model_dump(mode="json") if s.strategies else None,
                        }
                        for s in (scene.acting.states if scene.acting else [])
                    ],
                    "events": [
                        {"key": e.key, "type": e.type, "at": e.at.model_dump(mode="json") if e.at else None}
                        for e in (scene.acting.events if scene.acting else [])
                    ],
                    "shots": [{"key": s.key, "type": str(s.type), "camera": s.camera.profile_id} for s in scene.shots],
                    "world": {
                        "camera_position": scene.world.camera_position_key,
                        "time_of_day": str(scene.world.time_of_day),
                    }
                    if scene.world
                    else None,
                }
            )
        return {"title": spec.meta.title, "cast": [c.key for c in spec.cast], "scenes": scenes}

    async def plan(self, request: EditRequest, ctx: EditContext) -> EditPlan:
        references = resolve_references(request.instruction, request.selection, ctx.spec, ctx.refs, ctx.word_times)
        log = RunLog()
        notes = list(references.notes)
        planner: Literal["llm", "fixture", "template"]
        out: EditPlanOut
        provider = self.deps.provider
        summary = {"instruction_chars": len(request.instruction), "references": [r["slot"] for r in references.table]}
        try:
            if provider is None:
                raise FixtureMiss("no LLM provider")
            prompt = self.deps.prompts.render(
                "edit",
                instruction=request.instruction,
                references=references.table,
                video=self._video_summary(ctx.spec),
                options=[
                    {"kind": o.kind, "name": o.name, "description": o.description, "tags": list(o.tags)}
                    for o in ctx.options
                ],
                locks=[lock.model_dump(mode="json") for lock in ctx.spec.locks],
                vocab={
                    "emotion": sorted(self.vocab.tokens("emotion")),
                    "events": sorted(self.vocab.events),
                    **{f"strategy.{c}": sorted(self.vocab.tokens(f"strategy.{c}")) for c in (
                        "prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness"
                    )},
                    "camera.move_type": sorted(self.vocab.tokens("camera.move_type")),
                    "lock_groups": sorted(self.vocab.lock_groups),
                },
            )  # fmt: skip
            config = self.bundle.director
            try:
                result = await structured(
                    provider,
                    stage="edit",
                    output_type=EditPlanOut,
                    messages=prompt.messages(),
                    scenario_id=edit_scenario(request.instruction),
                    check=lambda value: self.mapper.problems(value.model_dump(mode="json"), EDIT_CONTROLS),
                    max_repairs=config.max_repairs if config else 2,
                    temperature=config.temperature if config else 0.2,
                    max_tokens=config.max_tokens if config else 4096,
                )
            except StructuredOutputError as exc:
                log.failed("edit", prompt.template_version, summary, exc)
                if exc.value is None:
                    raise EditError(
                        "The edit could not be planned.",
                        [Issue("llm_output_invalid", f"the model's output failed validation: {exc.problems[:3]}")],
                    ) from exc
                data = self.mapper.coerce(exc.value.model_dump(mode="json"), EDIT_CONTROLS, notes.append, stage="edit")
                out = EditPlanOut.model_validate(data)
            else:
                log.llm("edit", prompt.template_version, summary, result)
                out = result.value
            planner = "fixture" if getattr(provider, "key", "") == "fixture" else "llm"
        except FixtureMiss:
            if not self.deps.allow_template:
                raise
            out = _template_plan(request.instruction, references, ctx.spec, self.vocab)
            log.template("edit", summary, out.model_dump(mode="json"))
            planner = "template"
        try:
            ops = expand_slots(list(out.operations), references)
        except EditError as exc:
            # A recorded answer written for another selection (its slots are not in this edit, e.g. the
            # "make him more skeptical" fixture scoped to `@selection`, typed with nothing selected) is
            # a fixture miss, not the user's error (audit NL-02).
            if planner != "fixture" or not self.deps.allow_template or exc.issues[0].code != "unknown_reference":
                raise
            out = _template_plan(request.instruction, references, ctx.spec, self.vocab)
            log.template("edit", summary, out.model_dump(mode="json"))
            planner = "template"
            ops = expand_slots(list(out.operations), references)
        notes += [a for a in out.assumptions if a not in notes]
        ops = resolve_queries(ops, ctx, notes.append)
        return EditPlan(operations=ops, assumptions=notes, planner=planner, references=references, runs=log.runs)
