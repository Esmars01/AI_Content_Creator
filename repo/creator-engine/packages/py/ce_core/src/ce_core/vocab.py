"""The closed vocabularies (`config/vocab/`, §15.2, I13): loader, validator and lint.

Every control field in intent, acting, the CBS, memory kinds and edit operations takes its
values from these files. The loader validates each file's shape with Pydantic, flattens the
tokens into named categories (`emotion`, `strategy.gaze`, `intent.narrative_goal`,
`event_type`, `lock_group`, …), and `lint()` enforces the cross-file rules:

- every file declares the same `vocab_version`;
- strategy tokens and event tokens are disjoint;
- every token has a description in `descriptions.yaml`;
- every event type maps to exactly one *requestable* dimension;
- references between files resolve (emotion compatibilities, proxy labels and dimensions,
  dimension method preferences, memory field types, lock-group patterns and node kinds).

Adding a vocabulary item is a config change, never a code change (rule 7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from ce_core.canonical import content_digest
from ce_core.enums import RealizationMethod, ReliabilityClass
from ce_core.errors import Issue
from ce_core.spec.paths import SpecPath, SpecPathError
from ce_core.yamlio import safe_load

__all__ = [
    "VOCAB_FILES",
    "MemoryKindDef",
    "Vocabulary",
    "VocabularyError",
    "load_vocabulary",
]

VOCAB_FILES = (
    "emotions.yaml",
    "acting.yaml",
    "behavior_events.yaml",
    "behavior_dimensions.yaml",
    "observation_proxies.yaml",
    "descriptions.yaml",
    "intent.yaml",
    "memory_kinds.yaml",
    "world_elements.yaml",
    "edit_vocabulary.yaml",
)

# Build-graph node kinds (§12.1). Lock groups and regenerate components may only name these.
NODE_KINDS = frozenset(
    {
        "behavior.resolve",
        "behavior.compile_voice",
        "voice.prepare",
        "tts.segment",
        "asr.verify",
        "align.segment",
        "behavior.compile_visual",
        "world.plate",
        "behavior.keyframe_state",
        "image.keyframe",
        "avatar.render",
        "lipsync.patch",
        "post.expression",
        "video.broll",
        "video.upscale",
        "video.interpolate",
        "screen.prepare",
        "behavior.observe",
        "qc.shot",
        "qc.world",
        "post.camera",
        "post.realism",
        "audio.music",
        "audio.sfx",
        "audio.room",
        "captions.build",
        "mix.audio",
        "render.final",
        "render.proxy",
        "provenance.watermark_video",
        "provenance.watermark_audio",
        "provenance.sign",
        "qc.render",
        "captions.translate",
        "behavior.coverage",
    }
)


class VocabularyError(ValueError):
    """A vocabulary file is missing or malformed."""


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    vocab_version: str
    migrations: list[dict[str, str]] = Field(default_factory=list)


class EmotionDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    family: str
    valence: float = Field(ge=-1, le=1)
    arousal: float = Field(ge=0, le=1)
    dominance: float = Field(ge=0, le=1)
    compatible_strategies: list[str]
    incompatible: list[str]
    proxies: list[str]


class _Emotions(_File):
    families: list[str]
    labels: dict[str, EmotionDef]


class _Strategies(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prosody: list[str]
    gaze: list[str]
    gesture: list[str]
    posture: list[str]
    reaction: list[str]
    camera_awareness: list[str]


class _AnnotationTags(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pause: list[str]
    emphasis: list[str]
    nonverbal_audio: list[str]
    delivery: list[str]
    pronunciation: list[str]
    inserted_disfluency: list[str]


class _ActingValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intensity_jump_needs_transition: float = Field(ge=0, le=1)


class _Acting(_File):
    situation_kinds: list[str]
    audience_stances: list[str]
    stimulus_kinds: list[str]
    internal_states: list[str]
    social_goals: list[str]
    audience_goals: list[str]
    performance_intents: list[str]
    strategies: _Strategies
    attention_targets: list[str]
    attention_target_kinds: list[str]
    transition_styles: list[str]
    trigger_kinds: list[str]
    annotation_tags: _AnnotationTags
    camera: dict[str, list[str]]
    persona: dict[str, list[str]]
    validation: _ActingValidation
    pause_ms: dict[str, int]


class EventDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dimension: str
    default_duration_ms: int = Field(gt=0)
    params: list[Literal["direction", "target", "intensity", "purpose", "trigger_ref"]]


class _Events(_File):
    directions: list[str]
    event_types: dict[str, EventDef]
    purposes: list[str]


class DimensionDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: Literal["visual", "audio"]
    method_preference: list[RealizationMethod]


class _Dimensions(_File):
    requestable: dict[str, DimensionDef]
    engine_properties: list[str]
    strategy_dimensions: dict[str, list[str]]
    annotation_dimensions: dict[str, str]


class ProxyDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dimension: str
    labels: list[str]
    analyzer: str
    measure: str
    thresholds: dict[str, float]
    tolerance_ms: int = Field(ge=0)
    reliability: ReliabilityClass
    min_confidence: float = Field(ge=0, le=1)
    # Set from the measured calibration of the analyzer revision that produced the tracks (§16.2);
    # never in the YAML (the shipped reliability classes are initial hypotheses).
    calibrated_confidence: float | None = Field(default=None, ge=0, le=1)


class _Proxies(_File):
    proxies: dict[str, ProxyDef]


class DescriptionDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=3)
    vocal: str | None = Field(default=None, min_length=3)  # the voice-channel phrasing (emotions)
    aliases: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)


class _Descriptions(_File):
    descriptions: dict[str, dict[str, str | DescriptionDef]]


class _Intent(_File):
    narrative_goal: list[str]
    emotional_goal: list[str]
    audience_effect: list[str]
    persuasion_goal: list[str]
    information_goal: list[str]
    attention_goal: list[str]
    reveal_strategy: list[str]
    performance_strategy: list[str]
    cta_goal: list[str]
    scene_purpose: list[str]


class _MemoryKindSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fields: dict[str, str]
    key: list[str]


class _MemoryKinds(_File):
    categories: dict[str, dict[str, _MemoryKindSpec]]
    rejected_pattern_kinds: list[str]


class _WorldElements(_File):
    world_kinds: list[str]
    element_kinds: list[str]
    standard_states: list[str]
    hand_spaces: list[str]
    ambient_profiles: list[str]
    continuity_ref_kinds: list[str]
    reference_roles: list[str]


class LockGroupDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    patterns: list[str]
    pins_routes: bool
    pinned_node_kinds: list[str] = Field(default_factory=list)
    scope: dict[Literal["scene_keys", "character_keys", "shot_keys"], str]


class ComponentDef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_kinds: list[str]
    may_change: list[str]
    blocked_by: list[str]
    input_lock_groups: list[str]
    requires_director: bool = False


class _EditVocabulary(_File):
    lock_groups: dict[str, LockGroupDef]
    regenerate_components: dict[str, ComponentDef]


@dataclass(frozen=True)
class MemoryKindDef:
    """One memory kind: its id (`category.kind` or `category`), field types and dedup key fields."""

    kind_id: str
    category: str
    fields: Mapping[str, str]
    key_fields: tuple[str, ...]


@dataclass(frozen=True)
class Vocabulary:
    """The loaded vocabulary. Use `has()`/`check()` for membership and `lint()` for cross-file rules."""

    version: str
    root: Path
    categories: Mapping[str, frozenset[str]]
    emotions: Mapping[str, EmotionDef]
    events: Mapping[str, EventDef]
    dimensions: Mapping[str, DimensionDef]
    engine_properties: frozenset[str]
    strategy_dimensions: Mapping[str, tuple[str, ...]]
    annotation_dimensions: Mapping[str, str]
    annotation_tags: Mapping[str, frozenset[str]]
    proxies: Mapping[str, ProxyDef]
    memory_kinds: Mapping[str, MemoryKindDef]
    lock_groups: Mapping[str, LockGroupDef]
    regenerate_components: Mapping[str, ComponentDef]
    descriptions: Mapping[str, Mapping[str, DescriptionDef]]
    acting_validation: _ActingValidation
    pause_ms: Mapping[str, int]
    file_versions: Mapping[str, str]
    digest: str
    _raw: Mapping[str, Any] = field(repr=False)

    # ------------------------------------------------------------------ membership
    def has(self, category: str, token: str) -> bool:
        tokens = self.categories.get(category)
        if tokens is None:
            raise KeyError(f"unknown vocabulary category {category!r}")
        return token in tokens

    def check(self, category: str, token: str | None, *, path: str | None = None) -> list[Issue]:
        """An `unknown_vocab` issue when `token` is not in `category` (None is accepted)."""
        if token is None or self.has(category, token):
            return []
        return [
            Issue(
                code="unknown_vocab",
                message=f"{token!r} is not a {category} in vocab_version {self.version}",
                path=path,
                detail={"category": category, "token": token, "vocab_version": self.version},
            )
        ]

    def tokens(self, category: str) -> frozenset[str]:
        return self.categories[category]

    def describe(self, category: str, token: str) -> DescriptionDef | None:
        return self.descriptions.get(category, {}).get(token)

    # ------------------------------------------------------------------ memory values
    @cached_property
    def memory_value_models(self) -> dict[str, type[BaseModel]]:
        """One Pydantic model per memory kind, built from the field specs in memory_kinds.yaml."""
        return {kind_id: _memory_model(self, spec) for kind_id, spec in self.memory_kinds.items()}

    def validate_memory_value(self, kind_id: str, value: Mapping[str, Any]) -> BaseModel:
        model = self.memory_value_models.get(kind_id)
        if model is None:
            raise ValueError(f"unknown memory kind {kind_id!r}")
        return model.model_validate(dict(value))

    # ------------------------------------------------------------------ lint
    def lint(self) -> list[Issue]:
        issues: list[Issue] = []

        def add(code: str, message: str, **detail: Any) -> None:
            issues.append(Issue(code=code, message=message, detail=detail))

        for name, version in self.file_versions.items():
            if version != self.version:
                add("version_mismatch", f"{name} declares vocab_version {version}, expected {self.version}")

        strategy_tokens = set().union(*(self.categories[c] for c in self.categories if c.startswith("strategy.")))
        overlap = strategy_tokens & set(self.events)
        if overlap:
            add("strategy_event_overlap", f"strategy and event tokens must be disjoint: {sorted(overlap)}")

        for category, tokens in self.categories.items():
            described = self.descriptions.get(category, {})
            missing = sorted(set(tokens) - set(described))
            if missing:
                add("missing_description", f"{category}: no description for {missing}", category=category)
        for category, described in self.descriptions.items():
            if category not in self.categories:
                add("orphan_description", f"descriptions for unknown category {category!r}")
                continue
            extra = sorted(set(described) - self.categories[category])
            if extra:
                add("orphan_description", f"{category}: descriptions for unknown tokens {extra}")

        for name, event in self.events.items():
            if event.dimension not in self.dimensions:
                add("event_dimension", f"event {name} maps to {event.dimension!r}, not a requestable dimension")

        for name, emotion in self.emotions.items():
            if emotion.family not in self.categories["emotion_family"]:
                add("emotion_family", f"emotion {name}: unknown family {emotion.family}")
            for other in emotion.incompatible:
                if other not in self.emotions:
                    add("emotion_ref", f"emotion {name}: incompatible label {other!r} does not exist")
            for strategy in emotion.compatible_strategies:
                if strategy not in strategy_tokens:
                    add("emotion_ref", f"emotion {name}: compatible strategy {strategy!r} does not exist")
            for proxy_key in emotion.proxies:
                if proxy_key not in self.proxies:
                    add("emotion_ref", f"emotion {name}: proxy {proxy_key!r} does not exist")

        all_tokens = set().union(*self.categories.values())
        for key, proxy in self.proxies.items():
            if proxy.dimension not in self.dimensions:
                add("proxy_dimension", f"proxy {key}: {proxy.dimension!r} is not a requestable dimension")
            unknown = sorted(set(proxy.labels) - all_tokens)
            if unknown:
                add("proxy_label", f"proxy {key}: unknown labels {unknown}")

        for name, dim in self.dimensions.items():
            if not dim.method_preference or dim.method_preference[-1] != RealizationMethod.OMIT:
                add("method_preference", f"dimension {name}: method preference must end with 'omit'")
        for channel, dims in self.strategy_dimensions.items():
            if f"strategy.{channel}" not in self.categories:
                add("strategy_dimension", f"strategy_dimensions: unknown channel {channel}")
            for dim_name in dims:
                if dim_name not in self.dimensions:
                    add("strategy_dimension", f"strategy_dimensions.{channel}: unknown dimension {dim_name}")
        missing_pauses = sorted(set(self.annotation_tags["pause"]) - set(self.pause_ms))
        if missing_pauses:
            add("pause_ms", f"acting.yaml pause_ms lacks lengths for {missing_pauses}")
        for ann_type, dim_name in self.annotation_dimensions.items():
            if ann_type not in self.annotation_tags:
                add("annotation_dimension", f"annotation_dimensions: unknown annotation type {ann_type}")
            if dim_name not in self.dimensions:
                add("annotation_dimension", f"annotation_dimensions.{ann_type}: unknown dimension {dim_name}")

        for kind_id, kind in self.memory_kinds.items():
            for field_name, type_spec in kind.fields.items():
                problem = _check_type_spec(self, type_spec)
                if problem:
                    add("memory_field_type", f"memory kind {kind_id}.{field_name}: {problem}")
            for key_field in kind.key_fields:
                if key_field not in kind.fields:
                    add("memory_key", f"memory kind {kind_id}: key field {key_field!r} is not a field")

        for group, lock in self.lock_groups.items():
            pattern_fields: set[str] = set()
            for pattern in lock.patterns:
                try:
                    parsed = SpecPath.parse(pattern, allow_glob=True)
                except SpecPathError as exc:
                    add("lock_pattern", f"lock group {group}: {exc}")
                    continue
                pattern_fields |= {s.field for s in parsed.segments if s.selector == "*"}
            for scope_key, scope_field in lock.scope.items():
                if scope_field not in pattern_fields:
                    add("lock_scope", f"lock group {group}: scope {scope_key} restricts [{scope_field}[*]], absent")
            for node_kind in lock.pinned_node_kinds:
                if node_kind not in NODE_KINDS:
                    add("node_kind", f"lock group {group}: unknown node kind {node_kind}")
            if lock.pins_routes and not lock.pinned_node_kinds:
                add("lock_pins", f"lock group {group} pins routes but names no node kinds")
        for name, comp in self.regenerate_components.items():
            for node_kind in comp.node_kinds:
                if node_kind not in NODE_KINDS:
                    add("node_kind", f"component {name}: unknown node kind {node_kind}")
            for group in (*comp.blocked_by, *comp.input_lock_groups):
                if group not in self.lock_groups:
                    add("component_lock", f"component {name}: unknown lock group {group}")
            for pattern in comp.may_change:
                try:
                    SpecPath.parse(pattern, allow_glob=True)
                except SpecPathError as exc:
                    add("component_path", f"component {name}: {exc}")
        return issues


# ---------------------------------------------------------------------- loading


def _read(root: Path, name: str) -> Any:
    path = root / name
    if not path.is_file():
        raise VocabularyError(f"missing vocabulary file {path}")
    try:
        return safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # yaml.YAMLError and decoding errors
        raise VocabularyError(f"{path}: invalid YAML: {exc}") from exc


def _parse[M: BaseModel](model: type[M], data: Any, name: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise VocabularyError(f"{name}: {exc}") from exc


def load_vocabulary(root: Path | str) -> Vocabulary:
    """Loads and shape-validates `config/vocab/`. Call `lint()` for the cross-file rules."""
    root = Path(root)
    raw = {name: _read(root, name) for name in VOCAB_FILES}
    emotions = _parse(_Emotions, raw["emotions.yaml"], "emotions.yaml")
    acting = _parse(_Acting, raw["acting.yaml"], "acting.yaml")
    events = _parse(_Events, raw["behavior_events.yaml"], "behavior_events.yaml")
    dims = _parse(_Dimensions, raw["behavior_dimensions.yaml"], "behavior_dimensions.yaml")
    proxies = _parse(_Proxies, raw["observation_proxies.yaml"], "observation_proxies.yaml")
    descriptions = _parse(_Descriptions, raw["descriptions.yaml"], "descriptions.yaml")
    intent = _parse(_Intent, raw["intent.yaml"], "intent.yaml")
    memory = _parse(_MemoryKinds, raw["memory_kinds.yaml"], "memory_kinds.yaml")
    world = _parse(_WorldElements, raw["world_elements.yaml"], "world_elements.yaml")
    edit = _parse(_EditVocabulary, raw["edit_vocabulary.yaml"], "edit_vocabulary.yaml")

    categories: dict[str, frozenset[str]] = {
        "emotion": frozenset(emotions.labels),
        "emotion_family": frozenset(emotions.families),
        "situation_kind": frozenset(acting.situation_kinds),
        "audience_stance": frozenset(acting.audience_stances),
        "stimulus_kind": frozenset(acting.stimulus_kinds),
        "internal_state": frozenset(acting.internal_states),
        "social_goal": frozenset(acting.social_goals),
        "audience_goal": frozenset(acting.audience_goals),
        "performance_intent": frozenset(acting.performance_intents),
        "attention_target": frozenset(acting.attention_targets),
        "attention_target_kind": frozenset(acting.attention_target_kinds),
        "transition_style": frozenset(acting.transition_styles),
        "trigger_kind": frozenset(acting.trigger_kinds),
        "event_type": frozenset(events.event_types),
        "direction": frozenset(events.directions),
        "event_purpose": frozenset(events.purposes),
        "dimension": frozenset(dims.requestable),
        "engine_property": frozenset(dims.engine_properties),
        "rejected_pattern_kind": frozenset(memory.rejected_pattern_kinds),
        "memory_category": frozenset(memory.categories),
        "world_kind": frozenset(world.world_kinds),
        "element_kind": frozenset(world.element_kinds),
        "element_state": frozenset(world.standard_states),
        "hand_space": frozenset(world.hand_spaces),
        "ambient_profile": frozenset(world.ambient_profiles),
        "continuity_ref_kind": frozenset(world.continuity_ref_kinds),
        "reference_role": frozenset(world.reference_roles),
        "lock_group": frozenset(edit.lock_groups),
        "regenerate_component": frozenset(edit.regenerate_components),
    }
    for channel, tokens in acting.strategies.model_dump().items():
        categories[f"strategy.{channel}"] = frozenset(tokens)
    annotation_tags = {k: frozenset(v) for k, v in acting.annotation_tags.model_dump().items()}
    for ann_type, tokens in annotation_tags.items():
        categories[f"annotation_tag.{ann_type}"] = tokens
    for name, tokens in acting.camera.items():
        categories[f"camera.{name}"] = frozenset(tokens)
    for name, tokens in acting.persona.items():
        categories[f"persona.{name}"] = frozenset(tokens)
    for name, tokens in intent.model_dump(exclude={"vocab_version", "migrations"}).items():
        categories[f"intent.{name}"] = frozenset(tokens)

    memory_kinds: dict[str, MemoryKindDef] = {}
    for category, kinds in memory.categories.items():
        for kind, spec in kinds.items():
            kind_id = category if kind == category else f"{category}.{kind}"
            memory_kinds[kind_id] = MemoryKindDef(kind_id, category, dict(spec.fields), tuple(spec.key))
    categories["memory_kind"] = frozenset(memory_kinds)

    desc: dict[str, dict[str, DescriptionDef]] = {}
    for category, entries in descriptions.descriptions.items():
        desc[category] = {
            token: (DescriptionDef(text=value) if isinstance(value, str) else value) for token, value in entries.items()
        }

    files: dict[str, _File] = {
        "emotions.yaml": emotions,
        "acting.yaml": acting,
        "behavior_events.yaml": events,
        "behavior_dimensions.yaml": dims,
        "observation_proxies.yaml": proxies,
        "descriptions.yaml": descriptions,
        "intent.yaml": intent,
        "memory_kinds.yaml": memory,
        "world_elements.yaml": world,
        "edit_vocabulary.yaml": edit,
    }
    return Vocabulary(
        version=emotions.vocab_version,
        root=root,
        categories=categories,
        emotions=emotions.labels,
        events=events.event_types,
        dimensions=dims.requestable,
        engine_properties=frozenset(dims.engine_properties),
        strategy_dimensions={k: tuple(v) for k, v in dims.strategy_dimensions.items()},
        annotation_dimensions=dims.annotation_dimensions,
        annotation_tags=annotation_tags,
        proxies=proxies.proxies,
        memory_kinds=memory_kinds,
        lock_groups=edit.lock_groups,
        regenerate_components=edit.regenerate_components,
        descriptions=desc,
        acting_validation=acting.validation,
        pause_ms=acting.pause_ms,
        file_versions={name: f.vocab_version for name, f in files.items()},
        digest=content_digest(raw),
        _raw=raw,
    )


# ---------------------------------------------------------------------- memory value models

_SCALARS: dict[str, tuple[Any, Any]] = {
    "str": (str, Field(min_length=1)),
    "unit": (float, Field(ge=0, le=1)),
    "signed_unit": (float, Field(ge=-1, le=1)),
    "float": (float, ...),
    "int": (int, ...),
    "ms": (int, Field(ge=0)),
    "bool": (bool, ...),
}


def _check_type_spec(vocab: Vocabulary, spec: str) -> str | None:
    base = spec[:-1] if spec.endswith("?") else spec
    if base.startswith("list[") and base.endswith("]"):
        base = base[5:-1]
    if base in _SCALARS or base == "element_key":
        return None
    if base.startswith("vocab:"):
        category = base[6:]
        return None if category in vocab.categories else f"unknown vocabulary category {category!r}"
    return f"unknown field type {spec!r}"


def _vocab_validator(vocab: Vocabulary, category: str) -> Any:
    from pydantic import AfterValidator

    def check(value: str) -> str:
        if not vocab.has(category, value):
            raise ValueError(f"{value!r} is not a {category} in vocab_version {vocab.version}")
        return value

    return AfterValidator(check)


def _field_type(vocab: Vocabulary, spec: str) -> tuple[Any, Any]:
    from typing import Annotated

    from ce_core.keys import ElementKey

    optional = spec.endswith("?")
    base = spec[:-1] if optional else spec
    is_list = base.startswith("list[") and base.endswith("]")
    inner = base[5:-1] if is_list else base
    if inner.startswith("vocab:"):
        annotated: Any = Annotated[str, _vocab_validator(vocab, inner[6:])]
        default: Any = ...
    elif inner == "element_key":
        annotated, default = ElementKey, ...
    else:
        py_type, field_info = _SCALARS[inner]
        annotated = Annotated[py_type, field_info] if field_info is not ... else py_type
        default = ...
    if is_list:
        annotated = list[annotated]
        default = Field(default_factory=list)
    if optional:
        annotated = annotated | None
        default = None
    return annotated, default


def _memory_model(vocab: Vocabulary, kind: MemoryKindDef) -> type[BaseModel]:
    fields: dict[str, Any] = {name: _field_type(vocab, spec) for name, spec in kind.fields.items()}
    model_name = "Memory_" + kind.kind_id.replace(".", "_")
    model: type[BaseModel] = create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", frozen=True),
        **fields,
    )
    return model
