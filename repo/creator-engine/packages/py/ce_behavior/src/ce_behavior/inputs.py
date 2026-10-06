"""Inputs of the behavior nodes, gathered from a version's spec and its resolved references
(`ce_build.refs.BuildRefs`): the cast as the resolver sees it (Creator DNA, behavior digest,
pinned memory snapshot), the bound world, the characters of the previous scene, and the editorial
elements the spec already contains (what the build-time compiler may use, §15.7).

Shared by the executors (`ce_exec`) and by the pure planning helpers (`ce_behavior.plan`), so the
invariant tests resolve and compile exactly as production does.
"""

from __future__ import annotations

from ce_build.refs import BuildRefs
from ce_core.enums import DerivedFromKind
from ce_core.identity.creator import CreatorDNA
from ce_core.identity.world import WorldDNA
from ce_core.spec.anchors import SceneSpan, SegmentRef, ShotRef, WordRef
from ce_core.spec.videospec import Scene, Shot, VideoSpec

from ce_behavior.compiler import EditorialContext, Overlay, Punch
from ce_behavior.resolve import CastInput, WorldInput
from ce_behavior.scene import SceneWords

__all__ = ["cast_inputs", "editorial_context", "previous_characters", "scene_characters", "world_input"]


def scene_characters(spec: VideoSpec, scene: Scene) -> list[str]:
    return list(
        dict.fromkeys(
            [c.character_key for c in scene.cast] + [spec.script.segment(k).speaker_key for k in scene.segment_keys]
        )
    )


def cast_inputs(spec: VideoSpec, refs: BuildRefs, scene: Scene) -> dict[str, CastInput]:
    pins = {p.character_key: p.snapshot_id for p in spec.memory.snapshots}
    out: dict[str, CastInput] = {}
    for character in scene_characters(spec, scene):
        member = next((c for c in spec.cast if c.key == character), None)
        if member is None:
            continue
        creator = refs.creator(member.creator_version_id)
        snapshot = refs.snapshots.get(pins[character]) if character in pins else None
        out[character] = CastInput(
            character_key=character,
            dna=CreatorDNA.model_validate(dict(creator.dna)),
            dna_behavior_digest=creator.behavior_digest(),
            snapshot_items=tuple(snapshot.items) if snapshot else (),
            creator_version_id=creator.version_id,
            appearance_version_id=member.overrides.appearance_version_id or creator.appearance_version_id,
            voice_version_id=member.overrides.voice_version_id or creator.voice_version_id,
            memory_snapshot_id=snapshot.snapshot_id if snapshot else None,
        )
    return out


def world_input(refs: BuildRefs, scene: Scene) -> WorldInput | None:
    if scene.world is None:
        return None
    world = refs.worlds.get(scene.world.world_version_id)
    if world is None:
        return None
    return WorldInput(dna=WorldDNA.model_validate(dict(world.dna)), behavior_digest=world.behavior_digest())


def previous_characters(spec: VideoSpec, scene: Scene) -> list[str]:
    ordered = sorted(spec.scenes, key=lambda s: s.order)
    index = next(i for i, s in enumerate(ordered) if s.key == scene.key)
    if index == 0:
        return []
    return sorted(scene_characters(spec, ordered[index - 1]))


def editorial_context(spec: VideoSpec, scene: Scene, shot: Shot | None, words: SceneWords) -> EditorialContext:
    """Overlays (with the items they were planned to approximate), punch moves of the shot (or of
    every shot), SFX and music positions, caption emphasis and the items a split shot was split for."""
    overlays: list[Overlay] = []
    starts: dict[str, int] = {}
    for other in scene.shots:
        rng = words.range(other.span)
        if rng is not None:
            starts[other.key] = rng[0]
        if str(other.layer) != "overlay" or rng is None:
            continue
        refs = tuple(d.ref for d in other.derived_from if d.kind == DerivedFromKind.COMPILER_APPROXIMATION)
        overlays.append(Overlay(other.key, rng[0], rng[1], refs))
    punches: list[Punch] = []
    for target in [shot] if shot is not None else list(scene.shots):
        for move in target.camera.moves:
            if str(move.type) in ("punch_in", "punch_out") and isinstance(move.at, WordRef):
                position = words.position(move.at)
                if position is not None:
                    punches.append(Punch(target.key, move.key, position, str(move.type)))
    sfx: list[tuple[str, int]] = []
    for event in spec.audio.sfx:
        at = event.at
        position = None
        if isinstance(at, WordRef):
            position = words.position(at)
        elif isinstance(at, ShotRef):
            position = starts.get(at.shot_key)
        elif isinstance(at, SegmentRef):
            seg = words.segment_range(at.segment_key)
            position = (seg[0] if at.edge == "start" else seg[1]) if seg else None
        if position is not None:
            sfx.append((event.key, position))
    music: list[tuple[str, int]] = []
    for cue in spec.audio.music.cues:
        if isinstance(cue.span, SceneSpan):
            if cue.span.scene_key == scene.key and words.count:
                music.append((cue.key, 0))
        elif (cue_range := words.range(cue.span)) is not None:
            music.append((cue.key, cue_range[0]))
    splits: frozenset[str] = frozenset()
    if shot is not None:
        splits = frozenset(d.ref for d in shot.derived_from if d.kind == DerivedFromKind.COMPILER_APPROXIMATION)
    return EditorialContext(
        overlays=tuple(overlays),
        punches=tuple(punches),
        sfx_positions=tuple(sfx),
        music_positions=tuple(music),
        captions_emphasis=bool(spec.captions.enabled and str(spec.captions.highlight) != "none"),
        split_refs=splits,
    )
