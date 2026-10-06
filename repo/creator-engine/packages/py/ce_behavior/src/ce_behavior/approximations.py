"""Compiler approximations after a re-route (§15.7, I1).

Spec elements the plan-time compiler added (`derived_from: compiler_approximation`) record the
`route_digest` of the route they were planned for. When a later build routes the affected shot
(or segment) differently — a re-route, a fallback, a coverage upgrade — those elements are stale:
the system flags the version `approximations_stale` and proposes an edit that removes or
re-plans them, with the coverage delta. Nothing is changed silently: the proposal carries typed
`EditOperation`s and is applied like any edit (Phase 6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ce_core.build import RouteDecision
from ce_core.edit.ops import EditOperation, EditScope, RemoveShot, SetCamera, SetEffects, SetMusic, SetSfx
from ce_core.enums import DerivedFromKind, ShotType
from ce_core.spec.anchors import WordRef, WordSpan
from ce_core.spec.paths import SpecPath, SpecPathError
from ce_core.spec.videospec import Scene, VideoSpec

from ce_behavior.scene import SceneWords

__all__ = ["StaleApproximation", "approximation_route_keys", "reproposal_ops", "stale_approximations"]


@dataclass(frozen=True)
class StaleApproximation:
    element_path: str
    element_kind: str  # shot | camera_move | music_cue | sfx | effect
    element_key: str
    approximates: str  # the requested item it stands in for
    route_key: str  # the node whose route decides (avatar render of the shot, TTS of the segment)
    planned_route_digest: str
    current_route_digest: str | None
    shot_key: str | None = None


def _item_anchor(spec: VideoSpec, ref: str) -> tuple[Scene | None, WordRef | None, str | None]:
    """The scene, the first word and the character of the item a `derived_from` points at."""
    try:
        segs = SpecPath.parse(ref).segments
    except SpecPathError:
        return None, None, None
    if len(segs) >= 3 and segs[0].field == "scenes" and segs[1].field == "acting":
        scene = next((s for s in spec.scenes if s.key == segs[0].selector), None)
        if scene is None or scene.acting is None:
            return scene, None, None
        if segs[2].field == "events":
            event = next((e for e in scene.acting.events if e.key == segs[2].selector), None)
            if event is not None:
                return scene, event.at or (event.span.start if event.span else None), event.character_key
        if segs[2].field == "states":
            state = next((s for s in scene.acting.states if s.key == segs[2].selector), None)
            if state is not None and isinstance(state.span, WordSpan):
                return scene, state.span.start, state.character_key
        return scene, None, None
    if len(segs) >= 2 and segs[0].field == "script" and segs[1].field == "segments" and segs[1].selector:
        segment_key = segs[1].selector
        scene = next((s for s in spec.scenes if segment_key in s.segment_keys), None)
        return scene, WordRef(segment_key=segment_key, word=0), spec.script.segment(segment_key).speaker_key
    return None, None, None


def _talking_shot(spec: VideoSpec, scene: Scene, anchor: WordRef, character: str | None) -> str | None:
    words = SceneWords.of(spec, scene)
    position = words.position(anchor)
    for shot in scene.shots:
        if str(shot.type) != ShotType.TALKING_HEAD or str(shot.layer) != "base":
            continue
        if character is not None and shot.character_key != character:
            continue
        rng = words.range(shot.span)
        if rng is not None and position is not None and rng[0] <= position <= rng[1]:
            return shot.key
    return None


def approximation_route_keys(spec: VideoSpec) -> dict[str, str]:
    """For each element `derived_from` a compiler approximation: the node key whose route it was
    planned for (`avatar.render:<shot>:c1:t1`, or `tts.segment:<segment>` for audio items)."""
    out: dict[str, str] = {}
    for path, _, derived in _elements(spec):
        for entry in derived:
            if entry.kind != DerivedFromKind.COMPILER_APPROXIMATION:
                continue
            scene, anchor, character = _item_anchor(spec, entry.ref)
            if scene is None or anchor is None:
                continue
            if entry.ref.startswith("/script/"):
                out[path] = f"tts.segment:{anchor.segment_key}"
                continue
            shot = _talking_shot(spec, scene, anchor, character)
            if shot is not None:
                out[path] = f"avatar.render:{shot}:c1:t1"
    return out


def _elements(spec: VideoSpec) -> list[tuple[str, tuple[str, str], list]]:  # type: ignore[type-arg]
    found: list[tuple[str, tuple[str, str], list]] = []  # type: ignore[type-arg]
    for scene in spec.scenes:
        for shot in scene.shots:
            found.append((f"/scenes[{scene.key}]/shots[{shot.key}]", ("shot", shot.key), shot.derived_from))
            for move in shot.camera.moves:
                path = f"/scenes[{scene.key}]/shots[{shot.key}]/camera/moves[{move.key}]"
                found.append((path, ("camera_move", move.key), move.derived_from))
    for cue in spec.audio.music.cues:
        found.append((f"/audio/music/cues[{cue.key}]", ("music_cue", cue.key), cue.derived_from))
    for sfx in spec.audio.sfx:
        found.append((f"/audio/sfx[{sfx.key}]", ("sfx", sfx.key), sfx.derived_from))
    for effect in spec.effects:
        found.append((f"/effects[{effect.key}]", ("effect", effect.key), effect.derived_from))
    return found


def stale_approximations(spec: VideoSpec, routes: Mapping[str, RouteDecision]) -> list[StaleApproximation]:
    """`routes` maps node keys to the routes of the build being planned."""
    keys = approximation_route_keys(spec)
    out: list[StaleApproximation] = []
    for path, (kind, key), derived in _elements(spec):
        for entry in derived:
            if entry.kind != DerivedFromKind.COMPILER_APPROXIMATION or entry.route_digest is None:
                continue
            route_key = keys.get(path)
            if route_key is None:
                continue
            route = routes.get(route_key)
            current = route.digest() if route is not None else None
            if current == entry.route_digest:
                continue
            out.append(
                StaleApproximation(
                    element_path=path,
                    element_kind=kind,
                    element_key=key,
                    approximates=entry.ref,
                    route_key=route_key,
                    planned_route_digest=entry.route_digest,
                    current_route_digest=current,
                    shot_key=route_key.split(":")[1] if route_key.startswith("avatar.render:") else None,
                )
            )
    return out


def reproposal_ops(stale: list[StaleApproximation], routes: Mapping[str, RouteDecision]) -> list[EditOperation]:
    """Typed operations that remove the stale approximations (§15.7, I1); the user may ask the
    Director to re-plan them instead (an NL edit)."""
    ops: list[EditOperation] = []
    moves: dict[tuple[str, str], list[str]] = {}
    cues: list[str] = []
    sfx: list[str] = []
    effects: list[str] = []
    reasons: list[str] = []
    for item in stale:
        route = routes.get(item.route_key)
        reason = (
            f"planned as a compiler approximation of {item.approximates} for route "
            f"{item.planned_route_digest[:19]}…; the build now routes {item.route_key} to "
            f"{route.adapter_id if route else 'nothing'}"
        )
        segments = SpecPath.parse(item.element_path).segments
        if item.element_kind == "shot":
            ops.append(RemoveShot(scene_key=segments[0].selector, shot_key=item.element_key, reason=reason))
        elif item.element_kind == "camera_move":
            moves.setdefault((str(segments[0].selector), str(segments[1].selector)), []).append(item.element_key)
            reasons.append(reason)
        elif item.element_kind == "music_cue":
            cues.append(item.element_key)
            reasons.append(reason)
        elif item.element_kind == "sfx":
            sfx.append(item.element_key)
            reasons.append(reason)
        elif item.element_kind == "effect":
            effects.append(item.element_key)
            reasons.append(reason)
    for (_, shot_key), keys in sorted(moves.items()):
        ops.append(SetCamera(scope=EditScope(shot_keys=[shot_key]), remove_moves=keys, reason="; ".join(reasons)[:500]))
    if cues:
        ops.append(SetMusic(remove=cues, reason="; ".join(reasons)[:500]))
    if sfx:
        ops.append(SetSfx(remove=sfx, reason="; ".join(reasons)[:500]))
    if effects:
        ops.append(SetEffects(remove=effects, reason="; ".join(reasons)[:500]))
    return ops
