"""Context-aware VideoSpec validation (§11 validators, §15.3 acting validation).

The Pydantic models enforce structure. This module enforces everything that needs context:
the vocabulary (I13), the tokenizer (anchors in bounds, tiling), referenced records (exist,
approved, same creator), World DNA (bindings, zones, postures), Creator DNA (ranges and
avoidances), feature flags and configured tolerances. It returns every issue at once,
each addressed by a SpecPath, instead of failing on the first one.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ce_core.enums import AnnotationType, RecordStatus, ShotLayer, ShotType
from ce_core.errors import Issue
from ce_core.identity.creator import CreatorDNA, VoiceDNA
from ce_core.identity.world import WorldDNA
from ce_core.spec.acting import ActingState, BehaviorEvent
from ce_core.spec.anchors import DurationSpan, SceneSpan, SegmentRef, ShotRef, WordRef, WordSpan
from ce_core.spec.paths import SpecPath, SpecPathError
from ce_core.spec.videospec import Scene, VideoSpec
from ce_core.text import TOKENIZER_VERSION, tokenize
from ce_core.vocab import Vocabulary

__all__ = [
    "CreatorVersionInfo",
    "InMemoryReferences",
    "OwnedVersionInfo",
    "RecordInfo",
    "ReferenceLookup",
    "SnapshotInfo",
    "ValidationContext",
    "VoiceVersionInfo",
    "WorldVersionInfo",
    "errors_only",
    "validate_spec",
]


# ---------------------------------------------------------------------- reference lookups


@dataclass(frozen=True)
class CreatorVersionInfo:
    id: UUID
    creator_id: UUID
    status: RecordStatus
    dna: CreatorDNA | None = None


@dataclass(frozen=True)
class OwnedVersionInfo:
    """An appearance or wardrobe version. `creator_id` is the owning creator."""

    id: UUID
    status: RecordStatus
    creator_id: UUID | None


@dataclass(frozen=True)
class VoiceVersionInfo:
    """`creator_id` None = an org voice preset, usable by any creator of the org."""

    id: UUID
    status: RecordStatus
    creator_id: UUID | None
    dna: VoiceDNA | None = None


@dataclass(frozen=True)
class WorldVersionInfo:
    id: UUID
    status: RecordStatus
    dna: WorldDNA


@dataclass(frozen=True)
class RecordInfo:
    id: UUID
    status: RecordStatus | None = None


@dataclass(frozen=True)
class SnapshotInfo:
    id: UUID
    creator_version_id: UUID


class ReferenceLookup(Protocol):
    """Read access to the records a spec references (implemented over the DB in ce_db)."""

    def creator_version(self, version_id: UUID) -> CreatorVersionInfo | None: ...
    def appearance_version(self, version_id: UUID) -> OwnedVersionInfo | None: ...
    def voice_version(self, version_id: UUID) -> VoiceVersionInfo | None: ...
    def wardrobe_version(self, version_id: UUID) -> OwnedVersionInfo | None: ...
    def world_version(self, version_id: UUID) -> WorldVersionInfo | None: ...
    def product_version(self, version_id: UUID) -> RecordInfo | None: ...
    def asset(self, asset_id: UUID) -> RecordInfo | None: ...
    def memory_snapshot(self, snapshot_id: UUID) -> SnapshotInfo | None: ...
    def creator_defaults(self, creator_version_id: UUID) -> tuple[UUID, UUID] | None:
        """(default appearance_version_id, default voice_version_id) of a creator version."""
        ...


@dataclass
class InMemoryReferences:
    """A dict-backed ReferenceLookup for tests, fixtures and seeds."""

    creator_versions: dict[UUID, CreatorVersionInfo] = field(default_factory=dict)
    defaults: dict[UUID, tuple[UUID, UUID]] = field(default_factory=dict)
    appearance_versions: dict[UUID, OwnedVersionInfo] = field(default_factory=dict)
    voice_versions: dict[UUID, VoiceVersionInfo] = field(default_factory=dict)
    wardrobe_versions: dict[UUID, OwnedVersionInfo] = field(default_factory=dict)
    world_versions: dict[UUID, WorldVersionInfo] = field(default_factory=dict)
    product_versions: dict[UUID, RecordInfo] = field(default_factory=dict)
    assets: dict[UUID, RecordInfo] = field(default_factory=dict)
    snapshots: dict[UUID, SnapshotInfo] = field(default_factory=dict)

    def creator_version(self, version_id: UUID) -> CreatorVersionInfo | None:
        return self.creator_versions.get(version_id)

    def appearance_version(self, version_id: UUID) -> OwnedVersionInfo | None:
        return self.appearance_versions.get(version_id)

    def voice_version(self, version_id: UUID) -> VoiceVersionInfo | None:
        return self.voice_versions.get(version_id)

    def wardrobe_version(self, version_id: UUID) -> OwnedVersionInfo | None:
        return self.wardrobe_versions.get(version_id)

    def world_version(self, version_id: UUID) -> WorldVersionInfo | None:
        return self.world_versions.get(version_id)

    def product_version(self, version_id: UUID) -> RecordInfo | None:
        return self.product_versions.get(version_id)

    def asset(self, asset_id: UUID) -> RecordInfo | None:
        return self.assets.get(asset_id)

    def memory_snapshot(self, snapshot_id: UUID) -> SnapshotInfo | None:
        return self.snapshots.get(snapshot_id)

    def creator_defaults(self, creator_version_id: UUID) -> tuple[UUID, UUID] | None:
        return self.defaults.get(creator_version_id)


@dataclass
class ValidationContext:
    vocab: Vocabulary
    refs: ReferenceLookup | None = None
    multi_character_enabled: bool = False
    duration_tolerance: float = 0.25
    default_wpm: float = 150.0
    # World proposals: draft world versions a version may bind while `needs_world_approval` (§19.2).
    allow_draft_world_ids: frozenset[UUID] = frozenset()
    require_memory_snapshots: bool = True


def errors_only(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


# ---------------------------------------------------------------------- word index


@dataclass
class _SceneWords:
    """Word positions of one scene: (segment_key, word) → index in the scene's speech span."""

    index: dict[tuple[str, int], int]
    count: int
    segment_bounds: dict[str, tuple[int, int]]


class _Validator:
    def __init__(self, spec: VideoSpec, ctx: ValidationContext) -> None:
        self.spec = spec
        self.ctx = ctx
        self.vocab = ctx.vocab
        self.refs = ctx.refs
        self.issues: list[Issue] = []
        self.data = spec.model_dump(mode="json")
        self.cast_keys = {c.key for c in spec.cast}
        self.segments = {s.key: s for s in spec.script.segments}
        self.words = {s.key: tokenize(s.text) for s in spec.script.segments}
        self.segment_scene: dict[str, str] = {}
        self.scene_words: dict[str, _SceneWords] = {}
        self.shot_keys = {shot.key for _, shot in spec.shots()}
        self.world_dna: dict[str, WorldDNA] = {}
        self.creator_dna: dict[str, CreatorDNA] = {}
        self.creator_of_character: dict[str, UUID] = {}
        self.voice_wpm: dict[str, VoiceDNA] = {}

    # ------------------------------------------------------------------ helpers
    def add(self, code: str, message: str, path: str | None = None, *, warning: bool = False, **detail: object) -> None:
        self.issues.append(Issue(code, message, path, dict(detail), "warning" if warning else "error"))

    def vocab_check(self, category: str, token: str | None, path: str) -> None:
        self.issues.extend(self.vocab.check(category, token, path=path))

    def run(self) -> list[Issue]:
        self.check_versions()
        self.check_script_and_scenes()
        self.check_characters()
        self.load_references()
        for scene in sorted(self.spec.scenes, key=lambda s: s.order):
            self.check_scene(scene)
        self.check_intent_and_meta()
        self.check_audio_effects()
        self.check_locks()
        self.check_derived_from()
        self.check_memory_pins()
        self.check_duration()
        self.check_provenance()
        return self.issues

    # ------------------------------------------------------------------ versions and structure
    def check_versions(self) -> None:
        if self.spec.vocab_version != self.vocab.version:
            self.add(
                "vocab_version",
                f"spec declares vocab_version {self.spec.vocab_version}, current is {self.vocab.version}",
                "/vocab_version",
            )
        if self.spec.tokenizer_version != TOKENIZER_VERSION:
            self.add(
                "tokenizer_version",
                f"spec declares tokenizer_version {self.spec.tokenizer_version}, current is {TOKENIZER_VERSION}",
                "/tokenizer_version",
            )
        if self.spec.generation.seed_namespace is None:
            self.add("seed_namespace", "generation.seed_namespace is required", "/generation/seed_namespace")

    def check_script_and_scenes(self) -> None:
        scenes = sorted(self.spec.scenes, key=lambda s: s.order)
        orders = [s.order for s in scenes]
        if orders != list(range(1, len(scenes) + 1)):
            self.add("scene_order", f"scene orders must be 1..{len(scenes)} without gaps, got {orders}", "/scenes")
        timeline: list[str] = []
        for scene in scenes:
            for seg_key in scene.segment_keys:
                path = f"/scenes[{scene.key}]/segment_keys"
                if seg_key not in self.segments:
                    self.add("unknown_segment", f"scene {scene.key} lists unknown segment {seg_key}", path)
                elif seg_key in self.segment_scene:
                    self.add(
                        "segment_in_two_scenes",
                        f"segment {seg_key} belongs to {self.segment_scene[seg_key]} and {scene.key}",
                        path,
                    )
                else:
                    self.segment_scene[seg_key] = scene.key
                    timeline.append(seg_key)
            index: dict[tuple[str, int], int] = {}
            bounds: dict[str, tuple[int, int]] = {}
            n = 0
            for seg_key in scene.segment_keys:
                if seg_key not in self.segments:
                    continue
                count = len(self.words[seg_key])
                bounds[seg_key] = (n, n + count - 1)
                for w in range(count):
                    index[(seg_key, w)] = n + w
                n += count
            self.scene_words[scene.key] = _SceneWords(index, n, bounds)
        unassigned = [s.key for s in self.spec.script.segments if s.key not in self.segment_scene]
        if unassigned:
            self.add("segment_without_scene", f"segments not in any scene: {unassigned}", "/script/segments")
        script_order = [s.key for s in self.spec.script.segments if s.key in self.segment_scene]
        if timeline != script_order:
            self.add("segment_order", "scenes (by order) must list segments in script order", "/scenes")
        claims = {c.key for c in self.spec.research.claims} if self.spec.research else set()
        for seg in self.spec.script.segments:
            if not self.words[seg.key]:
                self.add("empty_segment", f"segment {seg.key} has no words", f"/script/segments[{seg.key}]/text")
            for claim in seg.claim_keys:
                if claim not in claims:
                    self.add(
                        "unknown_claim",
                        f"segment {seg.key} references unknown claim {claim}",
                        f"/script/segments[{seg.key}]/claim_keys",
                    )
            for ann in seg.annotations:
                path = f"/script/segments[{seg.key}]/annotations[{ann.key}]"
                self.vocab_check(f"annotation_tag.{ann.type}", ann.tag, f"{path}/tag")
                if ann.span.start.segment_key != seg.key or ann.span.end.segment_key != seg.key:
                    self.add(
                        "annotation_span", f"annotation {ann.key} must stay inside segment {seg.key}", f"{path}/span"
                    )
                else:
                    self.word_span_range(ann.span, path + "/span", scene_key=None)
                if ann.type == AnnotationType.INSERTED_DISFLUENCY and self.spec.wording_locked:
                    self.add(
                        "disfluency_locked", "inserted disfluencies are only allowed while wording is unlocked", path
                    )

    def check_characters(self) -> None:
        for seg in self.spec.script.segments:
            if seg.speaker_key not in self.cast_keys:
                self.add(
                    "unknown_character",
                    f"segment {seg.key} speaker {seg.speaker_key} is not in the cast",
                    f"/script/segments[{seg.key}]/speaker_key",
                )
        for scene in self.spec.scenes:
            scene_cast = [c.character_key for c in scene.cast]
            for key, n in Counter(scene_cast).items():
                if n > 1:
                    self.add(
                        "duplicate_scene_cast",
                        f"{key} appears twice in scene {scene.key}",
                        f"/scenes[{scene.key}]/cast",
                    )
            for key in scene_cast:
                if key not in self.cast_keys:
                    self.add(
                        "unknown_character",
                        f"scene {scene.key} cast {key} is not in the video cast",
                        f"/scenes[{scene.key}]/cast[{key}]",
                    )
            speakers = {self.segments[s].speaker_key for s in scene.segment_keys if s in self.segments}
            missing = sorted(speakers - set(scene_cast))
            if missing:
                self.add(
                    "speaker_not_in_scene",
                    f"speakers {missing} are missing from scene {scene.key} cast",
                    f"/scenes[{scene.key}]/cast",
                )

    # ------------------------------------------------------------------ references
    def load_references(self) -> None:
        refs = self.refs
        if refs is None:
            return
        for member in self.spec.cast:
            path = f"/cast[{member.key}]"
            creator = refs.creator_version(member.creator_version_id)
            if creator is None:
                self.add(
                    "unknown_reference",
                    f"creator version {member.creator_version_id} does not exist",
                    f"{path}/creator_version_id",
                )
                continue
            if creator.status != RecordStatus.APPROVED:
                self.add(
                    "not_approved",
                    f"creator version {creator.id} is {creator.status}, specs may reference only approved versions",
                    f"{path}/creator_version_id",
                )
            self.creator_of_character[member.key] = creator.creator_id
            if creator.dna is not None:
                self.creator_dna[member.key] = creator.dna
            if member.overrides.appearance_version_id is not None:
                appearance = refs.appearance_version(member.overrides.appearance_version_id)
                self.check_owned(
                    appearance, creator.creator_id, f"{path}/overrides/appearance_version_id", "appearance"
                )
            voice_id = member.overrides.voice_version_id
            if voice_id is None:
                defaults = refs.creator_defaults(member.creator_version_id)
                voice_id = defaults[1] if defaults else None
            if voice_id is not None:
                voice = refs.voice_version(voice_id)
                if voice is None:
                    self.add(
                        "unknown_reference",
                        f"voice version {voice_id} does not exist",
                        f"{path}/overrides/voice_version_id",
                    )
                else:
                    if voice.status != RecordStatus.APPROVED:
                        self.add(
                            "not_approved",
                            f"voice version {voice.id} is {voice.status}",
                            f"{path}/overrides/voice_version_id",
                        )
                    if voice.creator_id not in (None, creator.creator_id):
                        self.add(
                            "foreign_override",
                            f"voice version {voice.id} belongs to another creator",
                            f"{path}/overrides/voice_version_id",
                        )
                    if voice.dna is not None:
                        self.voice_wpm[member.key] = voice.dna
        for product in self.spec.products:
            info = refs.product_version(product.product_version_id)
            path = f"/products[{product.key}]/product_version_id"
            if info is None:
                self.add("unknown_reference", f"product version {product.product_version_id} does not exist", path)
            elif info.status != RecordStatus.APPROVED:
                self.add("not_approved", f"product version {info.id} is {info.status}", path)
        for asset_id, path in self.asset_references():
            if refs.asset(asset_id) is None:
                self.add("unknown_reference", f"asset {asset_id} does not exist", path)
        for scene in self.spec.scenes:
            for cast in scene.cast:
                if cast.wardrobe_version_id is None:
                    continue
                wardrobe = refs.wardrobe_version(cast.wardrobe_version_id)
                owner = self.creator_of_character.get(cast.character_key)
                self.check_owned(
                    wardrobe, owner, f"/scenes[{scene.key}]/cast[{cast.character_key}]/wardrobe_version_id", "wardrobe"
                )
            if scene.world is not None:
                world = refs.world_version(scene.world.world_version_id)
                path = f"/scenes[{scene.key}]/world/world_version_id"
                if world is None:
                    self.add("unknown_reference", f"world version {scene.world.world_version_id} does not exist", path)
                    continue
                if world.status != RecordStatus.APPROVED and world.id not in self.ctx.allow_draft_world_ids:
                    self.add(
                        "not_approved",
                        f"world version {world.id} is {world.status} (only a world proposal may be a draft)",
                        path,
                    )
                self.world_dna[scene.key] = world.dna

    def check_owned(self, info: OwnedVersionInfo | None, owner: UUID | None, path: str, label: str) -> None:
        if info is None:
            self.add("unknown_reference", f"{label} version does not exist", path)
            return
        if info.status != RecordStatus.APPROVED:
            self.add("not_approved", f"{label} version {info.id} is {info.status}", path)
        if owner is not None and info.creator_id != owner:
            self.add("foreign_override", f"{label} version {info.id} belongs to another creator", path)

    def asset_references(self) -> Iterable[tuple[UUID, str]]:
        for asset in self.spec.assets:
            yield asset.asset_id, f"/assets[{asset.key}]/asset_id"
        for seg in self.spec.script.segments:
            if seg.quote_source:
                yield seg.quote_source.asset_id, f"/script/segments[{seg.key}]/quote_source/asset_id"
        for scene, shot in self.spec.shots():
            base = f"/scenes[{scene.key}]/shots[{shot.key}]"
            if shot.visual and shot.visual.keyframe.asset_id:
                yield shot.visual.keyframe.asset_id, f"{base}/visual/keyframe/asset_id"
            if shot.broll and shot.broll.asset_id:
                yield shot.broll.asset_id, f"{base}/broll/asset_id"
            if shot.screen:
                yield shot.screen.asset_id, f"{base}/screen/asset_id"
            if shot.reaction_source:
                yield shot.reaction_source.asset_id, f"{base}/reaction_source/asset_id"
        for cue in self.spec.audio.music.cues:
            if cue.asset_id:
                yield cue.asset_id, f"/audio/music/cues[{cue.key}]/asset_id"
        for sfx in self.spec.audio.sfx:
            if sfx.asset_id:
                yield sfx.asset_id, f"/audio/sfx[{sfx.key}]/asset_id"

    # ------------------------------------------------------------------ anchors
    def word_span_range(self, span: WordSpan, path: str, scene_key: str | None) -> tuple[int, int] | None:
        """(start, end) indices of a word span within its scene's speech, or None with an issue."""
        refs = (span.start, span.end)
        for ref in refs:
            if not self.word_ok(ref, path):
                return None
        start_scene = self.segment_scene.get(span.start.segment_key)
        end_scene = self.segment_scene.get(span.end.segment_key)
        if start_scene is None or start_scene != end_scene:
            self.add("span_crosses_scenes", "a word span must stay within one scene", path)
            return None
        if scene_key is not None and start_scene != scene_key:
            self.add("span_outside_scene", f"span belongs to scene {start_scene}, not {scene_key}", path)
            return None
        words = self.scene_words[start_scene]
        a = words.index[(span.start.segment_key, span.start.word)]
        b = words.index[(span.end.segment_key, span.end.word)]
        if a > b:
            self.add("span_backwards", "word span runs backwards", path)
            return None
        return a, b

    def word_ok(self, ref: WordRef, path: str) -> bool:
        words = self.words.get(ref.segment_key)
        if words is None:
            self.add("unknown_segment", f"unknown segment {ref.segment_key}", path)
            return False
        if ref.word >= len(words):
            self.add(
                "word_out_of_range", f"word {ref.word} is out of range for {ref.segment_key} ({len(words)} words)", path
            )
            return False
        if ref.segment_key not in self.segment_scene:
            self.add("segment_without_scene", f"segment {ref.segment_key} is in no scene", path)
            return False
        return True

    def point_ok(self, ref: WordRef | ShotRef | SegmentRef, path: str, scene_key: str | None = None) -> None:
        if isinstance(ref, WordRef):
            if self.word_ok(ref, path) and scene_key and self.segment_scene.get(ref.segment_key) != scene_key:
                self.add("point_outside_scene", f"{ref.segment_key}:{ref.word} is not in scene {scene_key}", path)
        elif isinstance(ref, SegmentRef):
            if ref.segment_key not in self.segments:
                self.add("unknown_segment", f"unknown segment {ref.segment_key}", path)
        elif ref.shot_key not in self.shot_keys:
            self.add("unknown_shot", f"unknown shot {ref.shot_key}", path)

    def span_ok(
        self, span: WordSpan | DurationSpan | SceneSpan, path: str, scene_key: str | None
    ) -> tuple[int, int] | None:
        if isinstance(span, WordSpan):
            return self.word_span_range(span, path, scene_key)
        if isinstance(span, DurationSpan):
            self.point_ok(span.after, f"{path}/after")
            return None
        if span.scene_key not in self.scene_words:
            self.add("unknown_scene", f"unknown scene {span.scene_key}", path)
        return None

    # ------------------------------------------------------------------ scenes
    def check_scene(self, scene: Scene) -> None:
        base = f"/scenes[{scene.key}]"
        self.vocab_check("intent.scene_purpose", scene.purpose, f"{base}/purpose")
        intent = scene.intent
        for name, category in (
            ("narrative_goal", "intent.narrative_goal"),
            ("emotional_goal", "intent.emotional_goal"),
            ("audience_effect", "intent.audience_effect"),
            ("persuasion_goal", "intent.persuasion_goal"),
            ("information_goal", "intent.information_goal"),
            ("attention_goal", "intent.attention_goal"),
            ("reveal_strategy", "intent.reveal_strategy"),
            ("performance_strategy", "intent.performance_strategy"),
        ):
            self.vocab_check(category, getattr(intent, name), f"{base}/intent/{name}")
        words = self.scene_words[scene.key]
        world = self.world_dna.get(scene.key)
        if scene.world is not None and world is not None:
            self.check_world_binding(scene, world)
        for cast in scene.cast:
            cpath = f"{base}/cast[{cast.character_key}]"
            self.vocab_check("strategy.posture", cast.default_posture, f"{cpath}/default_posture")
            if world is not None and cast.placement is not None:
                zone = world.zone(cast.placement)
                if zone is None:
                    self.add("unknown_zone", f"zone {cast.placement} is not in the bound world", f"{cpath}/placement")
                elif cast.default_posture and cast.default_posture not in zone.allowed_postures:
                    self.add(
                        "posture_not_allowed",
                        f"posture {cast.default_posture} is not allowed in zone {zone.key}",
                        f"{cpath}/default_posture",
                    )
        self.check_shots(scene, words, world)
        if scene.acting is not None:
            self.check_acting(scene, words, world)
        else:
            talking = {s.character_key for s in scene.shots if s.type in {ShotType.TALKING_HEAD, ShotType.SILENT_HOLD}}
            if talking:
                self.add("missing_acting", f"scene {scene.key} has talking shots but no acting plan", f"{base}/acting")

    def check_world_binding(self, scene: Scene, world: WorldDNA) -> None:
        binding = scene.world
        assert binding is not None
        base = f"/scenes[{scene.key}]/world"
        position = world.camera_position(binding.camera_position_key)
        if position is None:
            self.add(
                "unknown_camera_position",
                f"camera position {binding.camera_position_key} is not in the world",
                f"{base}/camera_position_key",
            )
        elif position.status != "permitted":
            self.add(
                "forbidden_camera_position",
                f"camera position {position.key} is forbidden",
                f"{base}/camera_position_key",
            )
        if binding.time_of_day not in world.time_and_weather.allowed_times:
            self.add(
                "time_not_allowed",
                f"time of day {binding.time_of_day} is not allowed in this world",
                f"{base}/time_of_day",
            )
        if binding.weather not in world.time_and_weather.allowed_weather:
            self.add(
                "weather_not_allowed", f"weather {binding.weather} is not allowed in this world", f"{base}/weather"
            )
        overrides = binding.overrides
        for el_key, state in overrides.element_states.items():
            element = world.element(el_key)
            if element is None:
                self.add(
                    "unknown_element",
                    f"element {el_key} is not in the world",
                    f"{base}/overrides/element_states[{el_key}]",
                )
            elif state not in element.states:
                self.add(
                    "unknown_element_state",
                    f"{el_key} has no state {state!r} (states: {element.states})",
                    f"{base}/overrides/element_states[{el_key}]",
                )
        for el_key in overrides.hide_elements:
            if world.element(el_key) is None:
                self.add("unknown_element", f"element {el_key} is not in the world", f"{base}/overrides/hide_elements")
        for moved in overrides.move_elements:
            element = world.element(moved.key)
            if element is None:
                self.add(
                    "unknown_element", f"element {moved.key} is not in the world", f"{base}/overrides/move_elements"
                )
            elif element.mutability != "movable":
                self.add(
                    "element_not_movable",
                    f"element {moved.key} is {element.mutability}, only movable elements move",
                    f"{base}/overrides/move_elements",
                )
        for added in overrides.add_elements:
            self.vocab_check("element_kind", added.kind, f"{base}/overrides/add_elements")
        if overrides.lighting is not None:
            tol = world.lighting.tolerance
            outside = (
                abs(overrides.lighting.color_temp_k_delta) > tol.color_temp_k
                or abs(overrides.lighting.luminance_delta) > tol.luminance
            )
            if outside and not overrides.lighting.deviation_declared:
                self.add(
                    "lighting_out_of_tolerance",
                    "lighting deltas exceed the world tolerance; set deviation_declared to allow",
                    f"{base}/overrides/lighting",
                )
            elif outside:
                self.add(
                    "lighting_deviation",
                    "declared lighting deviation (flagged in previz)",
                    f"{base}/overrides/lighting",
                    warning=True,
                )
        if overrides.acoustics is not None:
            for ambient in overrides.acoustics.ambient_additions:
                self.vocab_check("ambient_profile", ambient, f"{base}/overrides/acoustics")
        ref = binding.continuity_ref
        if ref is not None and ref.kind == "world_plate" and world.camera_position(ref.camera_position_key) is None:
            self.add(
                "unknown_camera_position",
                f"continuity_ref position {ref.camera_position_key} is not in the world",
                f"{base}/continuity_ref",
            )
        if ref is not None and ref.kind == "asset" and self.refs is not None and self.refs.asset(ref.asset_id) is None:
            self.add("unknown_reference", f"asset {ref.asset_id} does not exist", f"{base}/continuity_ref")

    def check_shots(self, scene: Scene, words: _SceneWords, world: WorldDNA | None) -> None:
        base_ranges: list[tuple[int, int, str]] = []
        scene_cast = {c.character_key for c in scene.cast}
        for shot in scene.shots:
            path = f"/scenes[{scene.key}]/shots[{shot.key}]"
            if shot.type == ShotType.TWO_SHOT and not self.ctx.multi_character_enabled:
                self.add(
                    "two_shot_disabled", "two_shot is rejected while multi_character_enabled=false", f"{path}/type"
                )
            if shot.type == ShotType.TALKING_HEAD and shot.layer != ShotLayer.BASE:
                self.add("talking_head_overlay", "talking-head shots are base shots", f"{path}/layer")
            if shot.character_key is not None and shot.character_key not in scene_cast:
                self.add(
                    "unknown_character",
                    f"shot character {shot.character_key} is not in scene {scene.key} cast",
                    f"{path}/character_key",
                )
            self.vocab_check("camera.framing", shot.camera.framing, f"{path}/camera/framing")
            self.vocab_check("camera.angle", shot.camera.angle, f"{path}/camera/angle")
            rng = self.span_ok(shot.span, f"{path}/span", scene.key)
            if shot.layer == ShotLayer.BASE and rng is not None:
                base_ranges.append((rng[0], rng[1], shot.key))
            for move in shot.camera.moves:
                mpath = f"{path}/camera/moves[{move.key}]"
                self.vocab_check("camera.move_type", move.type, f"{mpath}/type")
                self.vocab_check("camera.transition", move.transition, f"{mpath}/transition")
                self.point_ok(move.at, f"{mpath}/at", scene.key)
                if rng is not None and self.word_ok(move.at, f"{mpath}/at"):
                    idx = words.index.get((move.at.segment_key, move.at.word))
                    if idx is not None and not rng[0] <= idx <= rng[1]:
                        self.add("move_outside_shot", f"camera move {move.key} is outside its shot", f"{mpath}/at")
            if shot.screen is not None:
                for zoom in shot.screen.zooms:
                    self.span_ok(zoom.span, f"{path}/screen/zooms[{zoom.key}]/span", scene.key)
                for seg in shot.screen.speed_segments:
                    self.span_ok(seg.span, f"{path}/screen/speed_segments", scene.key)
            if shot.product_key is not None and shot.product_key not in {p.key for p in self.spec.products}:
                self.add(
                    "unknown_product", f"shot product {shot.product_key} is not in products", f"{path}/product_key"
                )
            if world is not None and scene.world is not None and shot.character_key is not None:
                position = world.camera_position(scene.world.camera_position_key)
                if position is not None and shot.camera.profile_id not in position.allowed_camera_profiles:
                    self.add(
                        "camera_profile_not_allowed",
                        f"camera profile {shot.camera.profile_id} is not allowed at {position.key}",
                        f"{path}/camera/profile_id",
                    )
        if words.count:
            self.check_tiling(base_ranges, words.count, f"/scenes[{scene.key}]/shots", "base shots")

    def check_tiling(self, ranges: list[tuple[int, int, str]], count: int, path: str, what: str) -> None:
        ranges = sorted(ranges)
        expected = 0
        for start, end, key in ranges:
            if start > expected:
                self.add("tiling_gap", f"{what} leave words {expected}..{start - 1} uncovered before {key}", path)
            elif start < expected:
                self.add("tiling_overlap", f"{what} overlap at {key}", path)
            expected = max(expected, end + 1)
        if expected < count:
            self.add("tiling_gap", f"{what} leave words {expected}..{count - 1} uncovered at the end", path)
        if not ranges:
            self.add("tiling_gap", f"no {what} cover the scene's speech", path)

    # ------------------------------------------------------------------ acting
    def check_acting(self, scene: Scene, words: _SceneWords, world: WorldDNA | None) -> None:
        acting = scene.acting
        assert acting is not None
        base = f"/scenes[{scene.key}]/acting"
        self.vocab_check("situation_kind", acting.situation.kind, f"{base}/situation/kind")
        self.vocab_check("audience_stance", acting.situation.audience_stance, f"{base}/situation/audience_stance")
        if acting.situation.stimulus:
            self.vocab_check("stimulus_kind", acting.situation.stimulus.kind, f"{base}/situation/stimulus/kind")
        scene_cast = {c.character_key: c for c in scene.cast}
        element_keys = {e.key for e in world.elements} if world is not None else set()
        if scene.world is not None:
            element_keys |= {a.key for a in scene.world.overrides.add_elements}
        by_character: dict[str, list[tuple[int, int, ActingState]]] = defaultdict(list)
        for state in acting.states:
            spath = f"{base}/states[{state.key}]"
            if state.character_key not in scene_cast:
                self.add(
                    "unknown_character",
                    f"state character {state.character_key} is not in scene {scene.key} cast",
                    f"{spath}/character_key",
                )
            rng = self.span_ok(state.span, f"{spath}/span", scene.key)
            if rng is not None:
                by_character[state.character_key].append((rng[0], rng[1], state))
            self.check_state_vocab(state, spath, element_keys, world is not None)
            self.check_state_dna_and_world(scene, state, spath, world)
        talking = {
            s.character_key
            for s in scene.shots
            if s.type in {ShotType.TALKING_HEAD, ShotType.SILENT_HOLD} and s.character_key
        }
        for character in sorted(talking - set(by_character)):
            if words.count:
                self.add(
                    "missing_states",
                    f"{character} has talking shots in {scene.key} but no acting states",
                    f"{base}/states",
                )
        threshold = self.vocab.acting_validation.intensity_jump_needs_transition
        for character, items in by_character.items():
            if words.count:
                self.check_tiling(
                    [(a, b, s.key) for a, b, s in items], words.count, f"{base}/states", f"{character} acting states"
                )
            ordered = sorted(items, key=lambda x: (x[0], x[1]))
            previous: ActingState | None = None
            for state_first, state_last, state in ordered:
                if state.carry and previous is None:
                    self.add(
                        "carry_without_previous",
                        f"state {state.key} carries but has no previous state",
                        f"{base}/states[{state.key}]/carry",
                    )
                if previous is not None and state.emotion and previous.emotion:
                    jump = abs(state.emotion.displayed.intensity - previous.emotion.displayed.intensity)
                    transition = state.transition_in
                    covered = transition is not None and (
                        transition.style == "sudden" or transition.trigger is not None
                    )
                    if jump > threshold and not covered:
                        self.add(
                            "intensity_jump",
                            f"displayed intensity jumps by {jump:.2f} (> {threshold}) into {state.key} "
                            "without a sudden transition or trigger",
                            f"{base}/states[{state.key}]/transition_in",
                        )
                trigger = state.transition_in.trigger if state.transition_in else None
                if trigger is not None:
                    tpath = f"{base}/states[{state.key}]/transition_in/trigger"
                    self.vocab_check("trigger_kind", trigger.kind, f"{tpath}/kind")
                    self.point_ok(trigger.at, f"{tpath}/at", scene.key)
                    at = words.index.get((trigger.at.segment_key, trigger.at.word))
                    if at is not None and not state_first <= at <= state_last:
                        self.add(
                            "trigger_outside_state",
                            f"the trigger of {state.key} is not inside the state it starts",
                            f"{tpath}/at",
                        )
                if previous is not None and state.emotion and previous.emotion and not state.carry:
                    self.check_turn(previous, state, f"{base}/states[{state.key}]")
                if state.emotion and state.strategies and not state.carry:
                    self.check_strategy_fit(state, f"{base}/states[{state.key}]/strategies")
                if not state.carry:
                    previous = state
        annotation_spans: dict[tuple[str, int, int], list[str]] = defaultdict(list)
        for seg_key in scene.segment_keys:
            seg = self.segments.get(seg_key)
            if seg is None:
                continue
            for ann in seg.annotations:
                dim = self.vocab.annotation_dimensions.get(ann.type)
                first = words.index.get((ann.span.start.segment_key, ann.span.start.word))
                last = words.index.get((ann.span.end.segment_key, ann.span.end.word))
                if dim and first is not None and last is not None:
                    annotation_spans[(dim, first, last)].append(ann.key)
        for event in acting.events:
            self.check_event(scene, event, words, element_keys, world is not None, annotation_spans)

    def check_turn(self, previous: ActingState, state: ActingState, path: str) -> None:
        """A change into an emotion the previous state lists as incompatible is a turn the viewer
        must be able to read: it needs a transition trigger (§15.3, §15.4)."""
        assert previous.emotion is not None and state.emotion is not None
        before, after = previous.emotion.displayed.label, state.emotion.displayed.label
        definition = self.vocab.emotions.get(before)
        trigger = state.transition_in.trigger if state.transition_in else None
        if definition is not None and after in definition.incompatible and trigger is None:
            self.add(
                "turn_without_trigger",
                f"{before} → {after} turns between incompatible emotions; {state.key} needs a transition trigger",
                f"{path}/transition_in",
            )

    def check_strategy_fit(self, state: ActingState, path: str) -> None:
        """Warn when no strategy of the state is one that expresses its displayed emotion."""
        assert state.emotion is not None and state.strategies is not None
        label = state.emotion.displayed.label
        definition = self.vocab.emotions.get(label)
        if definition is None or not definition.compatible_strategies:
            return
        channels = ("prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness")
        chosen = {str(getattr(state.strategies, c)) for c in channels}
        if not chosen & set(definition.compatible_strategies):
            self.add(
                "strategies_do_not_express_emotion",
                f"none of the strategies {sorted(chosen)} is listed as expressing {label}",
                path,
                warning=True,
            )

    def check_state_vocab(self, state: ActingState, path: str, element_keys: set[str], world_known: bool) -> None:
        if state.internal_state:
            self.vocab_check("internal_state", state.internal_state.label, f"{path}/internal_state/label")
        self.vocab_check("social_goal", state.social_goal, f"{path}/social_goal")
        self.vocab_check("audience_goal", state.audience_goal, f"{path}/audience_goal")
        self.vocab_check("performance_intent", state.performance_intent, f"{path}/performance_intent")
        if state.emotion:
            self.vocab_check("emotion", state.emotion.felt.label, f"{path}/emotion/felt/label")
            self.vocab_check("emotion", state.emotion.displayed.label, f"{path}/emotion/displayed/label")
        if state.strategies:
            for channel in ("prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness"):
                self.vocab_check(
                    f"strategy.{channel}", getattr(state.strategies, channel), f"{path}/strategies/{channel}"
                )
        if state.transition_in:
            self.vocab_check("transition_style", state.transition_in.style, f"{path}/transition_in/style")
        self.attention_target_ok(state.attention_target, f"{path}/attention_target", element_keys, world_known)

    def attention_target_ok(self, target: str | None, path: str, element_keys: set[str], world_known: bool) -> None:
        if target is None or self.vocab.has("attention_target", target):
            return
        if target.startswith("el_"):
            if world_known and target not in element_keys:
                self.add("unknown_element", f"attention target {target} is not an element of the bound world", path)
            return
        self.add(
            "unknown_vocab", f"attention target {target!r} is neither an attention target nor an element key", path
        )

    def check_state_dna_and_world(self, scene: Scene, state: ActingState, path: str, world: WorldDNA | None) -> None:
        dna = self.creator_dna.get(state.character_key)
        allowed_out = state.out_of_character is not None and state.out_of_character.allowed
        if dna is not None and state.emotion is not None:
            label = state.emotion.displayed.label
            intensity = state.emotion.displayed.intensity
            bounds = dna.behavior.emotion_ranges.get(label)
            if bounds is not None and not bounds[0] <= intensity <= bounds[1]:
                if allowed_out:
                    self.add(
                        "out_of_character",
                        f"{label}@{intensity} is outside DNA range {bounds} (declared out of character)",
                        f"{path}/emotion/displayed",
                        warning=True,
                    )
                else:
                    self.add(
                        "outside_dna_range",
                        f"{label}@{intensity} is outside the creator's DNA range {list(bounds)}",
                        f"{path}/emotion/displayed",
                    )
            for cap in dna.avoidances.emotions:
                if cap.label == label and intensity > cap.max_intensity and not allowed_out:
                    self.add(
                        "avoidance",
                        f"{label} above {cap.max_intensity} is on the creator's avoid list",
                        f"{path}/emotion/displayed",
                    )
        if world is not None and state.strategies is not None:
            placement = next((c.placement for c in scene.cast if c.character_key == state.character_key), None)
            zone = world.zone(placement) if placement else None
            if zone is not None and state.strategies.posture not in zone.allowed_postures:
                self.add(
                    "posture_not_allowed",
                    f"posture {state.strategies.posture} is not allowed in zone {zone.key}",
                    f"{path}/strategies/posture",
                )

    def check_event(
        self,
        scene: Scene,
        event: BehaviorEvent,
        words: _SceneWords,
        element_keys: set[str],
        world_known: bool,
        annotation_spans: Mapping[tuple[str, int, int], list[str]],
    ) -> None:
        path = f"/scenes[{scene.key}]/acting/events[{event.key}]"
        if event.character_key not in {c.character_key for c in scene.cast}:
            self.add(
                "unknown_character",
                f"event character {event.character_key} is not in scene {scene.key} cast",
                f"{path}/character_key",
            )
        definition = self.vocab.events.get(event.type)
        if definition is None:
            self.vocab_check("event_type", event.type, f"{path}/type")
        else:
            for name in ("direction", "target", "intensity", "purpose", "trigger_ref"):
                if getattr(event, name) is not None and name not in definition.params:
                    self.add("event_param", f"event type {event.type} does not take `{name}`", f"{path}/{name}")
        self.vocab_check("direction", event.direction, f"{path}/direction")
        self.vocab_check("event_purpose", event.purpose, f"{path}/purpose")
        if event.target is not None:
            self.attention_target_ok(event.target, f"{path}/target", element_keys, world_known)
        rng: tuple[int, int] | None = None
        if event.at is not None:
            self.point_ok(event.at, f"{path}/at", scene.key)
            idx = words.index.get((event.at.segment_key, event.at.word))
            rng = (idx, idx) if idx is not None else None
        elif event.span is not None:
            rng = self.word_span_range(event.span, f"{path}/span", scene.key)
        if definition is not None and rng is not None:
            duplicates = annotation_spans.get((definition.dimension, rng[0], rng[1]))
            if duplicates:
                self.add(
                    "same_dimension_duplicate",
                    f"event {event.key} and annotation {duplicates[0]} address the same dimension "
                    f"({definition.dimension}) on the same span",
                    path,
                )
        dna = self.creator_dna.get(event.character_key)
        if dna is not None and event.type in dna.avoidances.gestures:
            self.add("avoidance", f"{event.type} is on the creator's avoid list", f"{path}/type")
        if event.trigger_ref is not None:
            self.ref_exists(event.trigger_ref, f"{path}/trigger_ref")

    # ------------------------------------------------------------------ video-level
    def check_intent_and_meta(self) -> None:
        video = self.spec.intent.video
        for name, category in (
            ("narrative_goal", "intent.narrative_goal"),
            ("audience_effect", "intent.audience_effect"),
            ("persuasion_goal", "intent.persuasion_goal"),
            ("information_goal", "intent.information_goal"),
            ("attention_goal", "intent.attention_goal"),
            ("cta_goal", "intent.cta_goal"),
        ):
            self.vocab_check(category, getattr(video, name), f"/intent/video/{name}")
        for label in video.emotional_arc:
            self.vocab_check("emotion", label, "/intent/video/emotional_arc")
        if video.emotional_arc:
            displayed: list[str] = []
            for scene in sorted(self.spec.scenes, key=lambda s: s.order):
                if scene.acting:
                    for state in scene.acting.states:
                        if state.emotion and (not displayed or displayed[-1] != state.emotion.displayed.label):
                            displayed.append(state.emotion.displayed.label)
            it = iter(displayed)
            if displayed and not all(label in it for label in video.emotional_arc):
                self.add(
                    "emotional_arc",
                    f"emotional_arc {video.emotional_arc} does not summarize the trajectory {displayed}",
                    "/intent/video/emotional_arc",
                )

    def check_audio_effects(self) -> None:
        for cue in self.spec.audio.music.cues:
            self.span_ok(cue.span, f"/audio/music/cues[{cue.key}]/span", None)
        for sfx in self.spec.audio.sfx:
            self.point_ok(sfx.at, f"/audio/sfx[{sfx.key}]/at")
        for effect in self.spec.effects:
            self.span_ok(effect.span, f"/effects[{effect.key}]/span", None)
        if self.spec.audio.acoustics.source == "world":
            unbound = [s.key for s in self.spec.scenes if s.world is None and s.segment_keys]
            if unbound:
                self.add(
                    "acoustics_without_world",
                    f"acoustics come from worlds, but scenes {unbound} bind none",
                    "/audio/acoustics",
                    warning=True,
                )

    def check_locks(self) -> None:
        scene_keys = {s.key for s in self.spec.scenes}
        for i, lock in enumerate(self.spec.locks):
            path = f"/locks/{i}"
            group = self.vocab.lock_groups.get(lock.group)
            if group is None:
                self.vocab_check("lock_group", lock.group, f"{path}/group")
                continue
            scope = lock.scope
            for field_name, keys, known in (
                ("scene_keys", scope.scene_keys, scene_keys),
                ("character_keys", scope.character_keys, self.cast_keys),
                ("shot_keys", scope.shot_keys, self.shot_keys),
            ):
                if keys is None:
                    continue
                if field_name not in group.scope:
                    self.add(
                        "lock_scope",
                        f"lock group {lock.group} cannot be scoped by {field_name}",
                        f"{path}/scope/{field_name}",
                    )
                unknown = sorted(set(keys) - known)
                if unknown:
                    self.add("lock_scope", f"lock scope names unknown keys {unknown}", f"{path}/scope/{field_name}")
        seen = Counter((lock.group, lock.scope.model_dump_json()) for lock in self.spec.locks)
        for (group_name, _), n in seen.items():
            if n > 1:
                self.add("duplicate_lock", f"lock {group_name} is listed twice with the same scope", "/locks")

    def ref_exists(self, ref: str, path: str) -> None:
        try:
            exists = SpecPath.parse(ref).exists(self.data)
        except SpecPathError as exc:
            self.add("bad_ref", str(exc), path)
            return
        if not exists:
            self.add("dangling_ref", f"{ref} does not exist in this spec", path)

    def check_derived_from(self) -> None:
        for scene, shot in self.spec.shots():
            for i, entry in enumerate(shot.derived_from):
                self.ref_exists(entry.ref, f"/scenes[{scene.key}]/shots[{shot.key}]/derived_from/{i}")
            for move in shot.camera.moves:
                for i, entry in enumerate(move.derived_from):
                    self.ref_exists(
                        entry.ref, f"/scenes[{scene.key}]/shots[{shot.key}]/camera/moves[{move.key}]/derived_from/{i}"
                    )
        for cue in self.spec.audio.music.cues:
            for i, entry in enumerate(cue.derived_from):
                self.ref_exists(entry.ref, f"/audio/music/cues[{cue.key}]/derived_from/{i}")
        for sfx in self.spec.audio.sfx:
            for i, entry in enumerate(sfx.derived_from):
                self.ref_exists(entry.ref, f"/audio/sfx[{sfx.key}]/derived_from/{i}")
        for effect in self.spec.effects:
            for i, entry in enumerate(effect.derived_from):
                self.ref_exists(entry.ref, f"/effects[{effect.key}]/derived_from/{i}")

    def check_memory_pins(self) -> None:
        pins = Counter(p.character_key for p in self.spec.memory.snapshots)
        for character, n in pins.items():
            if n > 1:
                self.add(
                    "memory_pin",
                    f"{character} has {n} pinned snapshots; exactly one is allowed (I7)",
                    "/memory/snapshots",
                )
            if character not in self.cast_keys:
                self.add("memory_pin", f"snapshot pinned for {character}, who is not in the cast", "/memory/snapshots")
        if self.ctx.require_memory_snapshots:
            missing = sorted(self.cast_keys - set(pins))
            if missing:
                self.add("memory_pin", f"no MemorySnapshot pinned for {missing} (I7)", "/memory/snapshots")
        if self.refs is not None:
            creator_versions = {c.key: c.creator_version_id for c in self.spec.cast}
            for pin in self.spec.memory.snapshots:
                info = self.refs.memory_snapshot(pin.snapshot_id)
                path = f"/memory/snapshots[{pin.character_key}]/snapshot_id"
                if info is None:
                    self.add("unknown_reference", f"memory snapshot {pin.snapshot_id} does not exist", path)
                elif creator_versions.get(pin.character_key) not in (None, info.creator_version_id):
                    self.add("snapshot_mismatch", "the snapshot was taken for another creator version", path)

    def check_duration(self) -> None:
        total_s = 0.0
        for scene in self.spec.scenes:
            delta = scene.pacing.target_wpm_delta if scene.pacing else 0.0
            for seg_key in scene.segment_keys:
                seg = self.segments.get(seg_key)
                if seg is None:
                    continue
                voice = self.voice_wpm.get(seg.speaker_key)
                language = seg.language or self.spec.meta.language
                wpm = voice.wpm_for(language, self.ctx.default_wpm) if voice else self.ctx.default_wpm
                total_s += len(self.words[seg_key]) * 60.0 / (wpm * (1.0 + delta))
                for ann in seg.annotations:
                    if ann.type == AnnotationType.PAUSE:
                        total_s += (ann.pause_ms or self.vocab.pause_ms.get(ann.tag, 0)) / 1000.0
            for shot in scene.shots:
                if shot.layer == ShotLayer.BASE and isinstance(shot.span, DurationSpan):
                    total_s += shot.span.duration_ms / 1000.0
        target = self.spec.meta.target_duration_s
        if target and abs(total_s - target) / target > self.ctx.duration_tolerance:
            self.add(
                "duration",
                f"estimated duration {total_s:.1f} s is outside ±{self.ctx.duration_tolerance:.0%} "
                f"of the {target:g} s target",
                "/meta/target_duration_s",
                estimated_s=round(total_s, 2),
            )

    def check_provenance(self) -> None:
        consents = set(self.spec.provenance.consent_ids)
        for seg in self.spec.script.segments:
            if seg.quote_source and seg.quote_source.consent_id not in consents:
                self.add(
                    "quote_consent",
                    f"segment {seg.key} quotes a real customer without listing the consent in provenance.consent_ids",
                    f"/script/segments[{seg.key}]/quote_source",
                )


def validate_spec(spec: VideoSpec, ctx: ValidationContext) -> list[Issue]:
    """All issues of a spec in context. Errors block; warnings are reported in previz."""
    return _Validator(spec, ctx).run()
