"""The behavior build nodes as pure functions (§15.6, §15.7): what `behavior.compile_voice`,
`behavior.compile_visual` and `behavior.keyframe_state` compute from a spec, its CBS and the
routed engine. The executors (`ce_exec.behavior`) and the dirty-analysis evaluator
(`ce_behavior.evaluate`) both call these, so an evaluated digest is the digest the build produces.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ce_contracts.behavior import BehaviorMatrix
from ce_contracts.manifest import PluginManifest
from ce_core.behavior.cbs import CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.build import RouteDecision
from ce_core.canonical import content_digest
from ce_core.spec.anchors import WordRef
from ce_core.spec.videospec import Scene, Shot, VideoSpec
from ce_core.vocab import Vocabulary

from ce_behavior.compiler import CompileTarget, compile_behavior
from ce_behavior.directives import generation_digest
from ce_behavior.inputs import editorial_context
from ce_behavior.scene import SceneWords

__all__ = ["compile_digest", "keyframe_state", "keyframe_state_digest", "visual_target", "voice_target"]


def _target(
    manifest: PluginManifest,
    route: RouteDecision | None,
    *,
    unreliable: frozenset[str],
    editorial_methods: frozenset[str],
    **fields: Any,
) -> CompileTarget:
    return CompileTarget(
        route_digest=route.digest() if route is not None and route.adapter_id == manifest.id else None,
        matrix=manifest.behavior_matrix,
        knobs=dict(manifest.knobs),
        validation=str(manifest.validation),
        unreliable=unreliable,
        editorial_methods=editorial_methods,
        **fields,
    )


def voice_target(
    spec: VideoSpec,
    scene: Scene,
    segment_key: str,
    *,
    manifest: PluginManifest,
    route: RouteDecision | None,
    character_key: str | None = None,
    unreliable: frozenset[str] = frozenset(),
    editorial_methods: frozenset[str] = frozenset(),
) -> tuple[CompileTarget, SceneWords]:
    words = SceneWords.of(spec, scene)
    scope = words.segment_range(segment_key)
    if scope is None:
        raise ValueError(f"segment {segment_key} has no words in scene {scene.key}")
    target = _target(
        manifest,
        route,
        unreliable=unreliable,
        editorial_methods=editorial_methods,
        node_kind="behavior.compile_voice",
        stage="build_time",
        channel="audio",
        target_key=segment_key,
        character_key=character_key or spec.script.segment(segment_key).speaker_key,
        scope=scope,
        segment_key=segment_key,
        editorial=editorial_context(spec, scene, None, words),
    )
    return target, words


def visual_target(
    spec: VideoSpec,
    scene: Scene,
    shot: Shot,
    chunk: int,
    chunk_range: tuple[WordRef, WordRef],
    *,
    manifest: PluginManifest,
    route: RouteDecision | None,
    tts_matrix: BehaviorMatrix | None = None,
    unreliable: frozenset[str] = frozenset(),
    editorial_methods: frozenset[str] = frozenset(),
) -> tuple[CompileTarget, SceneWords]:
    """`chunk_range` is the chunk's first and last word (`params.range` of the compile node)."""
    words = SceneWords.of(spec, scene)
    first, last = words.position(chunk_range[0]), words.position(chunk_range[1])
    shot_range = words.range(shot.span)
    if first is None or last is None or shot_range is None:
        raise ValueError(f"shot {shot.key} chunk {chunk} does not resolve to words of scene {scene.key}")
    target = _target(
        manifest,
        route,
        unreliable=unreliable,
        editorial_methods=editorial_methods,
        node_kind="behavior.compile_visual",
        stage="build_time",
        channel="visual",
        target_key=f"{shot.key}:c{chunk}",
        character_key=shot.character_key or "",
        scope=(first, last),
        shot=shot_range,
        shot_key=shot.key,
        chunk=chunk,
        editorial=editorial_context(spec, scene, shot, words),
        tts_matrix=tts_matrix,
    )
    return target, words


def compile_digest(cbs: CBSContent, target: CompileTarget, words: SceneWords, vocab: Vocabulary) -> str:
    """The generation digest of one compile node's output (`directives.generation_digest`)."""
    compiled: CompiledBehavior = compile_behavior(cbs, target, vocab=vocab, words=words)
    return generation_digest(compiled, cbs)


def keyframe_state(spec: VideoSpec, scene: Scene, shot: Shot, cbs: CBSContent) -> dict[str, Any]:
    """The resolved behavior at the shot's first word (§15.7 `keyframe_conditioning`), without
    the CBS digest (the executor adds it)."""
    words = SceneWords.of(spec, scene)
    cast = next((c for c in scene.cast if c.character_key == shot.character_key), None)
    first = words.range(shot.span)
    state = None
    if first is not None:
        for candidate in cbs.trajectory:
            rng = words.range(candidate.span)
            if candidate.character_key == shot.character_key and rng and rng[0] <= first[0] <= rng[1]:
                state = candidate
                break
    posture = (
        str(state.strategies.posture)
        if state is not None
        else (str(cast.default_posture) if cast and cast.default_posture else "seated_upright")
    )
    return {
        "state_key": state.key if state else None,
        "expression": str(state.emotion.displayed.label) if state else "neutral",
        "intensity": float(state.emotion.displayed.intensity) if state else 0.0,
        "posture": posture,
        "camera_awareness": str(state.strategies.camera_awareness) if state else "direct_address",
        "placement": str(cast.placement) if cast and cast.placement else None,
        "attention_target": state.attention_target if state else "camera",
        "framing": shot.camera.framing,
    }


def keyframe_state_digest(data: Mapping[str, Any]) -> str:
    """What `image.keyframe` reads from a keyframe-state output (everything but the CBS digest)."""
    return content_digest({k: v for k, v in data.items() if k not in ("cbs_content_digest", "generation_digest")})
