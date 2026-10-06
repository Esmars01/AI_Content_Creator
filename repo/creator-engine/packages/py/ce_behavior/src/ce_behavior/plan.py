"""Whole-version behavior planning without persistence (§15.6, §15.7): resolve every scene's CBS,
compile it against a set of routes, and report predicted (plan-time) or compiled coverage.

The executors compile per node (one segment, one shot chunk); these helpers compile each segment
and each rendered shot as a single target, which is what plan-time compilation (Director stage
11), the route preview and the invariant tests need. Nothing here touches a database or storage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from ce_build.refs import BuildRefs
from ce_config.schemas import BehaviorConfig
from ce_contracts.manifest import PluginManifest
from ce_core.behavior.cbs import CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_core.build import RouteDecision
from ce_core.spec.videospec import Scene, VideoSpec
from ce_core.vocab import Vocabulary

from ce_behavior.compiler import CompileTarget, compile_behavior, realized_methods
from ce_behavior.coverage import coverage_report
from ce_behavior.inputs import cast_inputs, editorial_context, previous_characters, world_input
from ce_behavior.resolve import resolve
from ce_behavior.scene import SceneWords

__all__ = ["compile_version", "predicted_coverage", "version_cbs"]

Stage = Literal["plan_time", "build_time"]


def version_cbs(spec: VideoSpec, refs: BuildRefs, vocab: Vocabulary, config: BehaviorConfig) -> dict[str, CBSContent]:
    """The CBS content of every scene, resolved exactly as `behavior.resolve` does."""
    return {
        scene.key: resolve(
            spec,
            scene,
            cast=cast_inputs(spec, refs, scene),
            world=world_input(refs, scene),
            vocab=vocab,
            config=config,
            previous_scene_characters=previous_characters(spec, scene),
        )
        for scene in spec.scenes
    }


def _target(
    manifest: PluginManifest,
    route: RouteDecision,
    unreliable: Callable[[str], frozenset[str]] | None,
    editorial_methods: frozenset[str],
    **fields: object,
) -> CompileTarget:
    return CompileTarget(
        route_digest=route.digest(),
        matrix=manifest.behavior_matrix,
        knobs=dict(manifest.knobs),
        validation=str(manifest.validation),
        unreliable=unreliable(manifest.id) if unreliable else frozenset(),
        editorial_methods=editorial_methods,
        **fields,  # type: ignore[arg-type]
    )


def compile_version(
    spec: VideoSpec,
    cbs_by_scene: Mapping[str, CBSContent],
    routes: Mapping[str, RouteDecision],
    manifests: Mapping[str, PluginManifest],
    vocab: Vocabulary,
    *,
    editorial_methods: frozenset[str] = frozenset(),
    stage: Stage = "build_time",
    unreliable: Callable[[str], frozenset[str]] | None = None,
) -> list[CompiledBehavior]:
    """One CompiledBehavior per segment (its TTS route) and per rendered shot (its avatar route,
    `avatar.render:<shot>:c1:t1`). `routes` maps node keys to routes, e.g. from `build_graph()`."""
    out: list[CompiledBehavior] = []
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        cbs = cbs_by_scene[scene.key]
        words = SceneWords.of(spec, scene)
        out += _compile_scene(spec, scene, cbs, words, routes, manifests, vocab, editorial_methods, stage, unreliable)
    return out


def _compile_scene(
    spec: VideoSpec,
    scene: Scene,
    cbs: CBSContent,
    words: SceneWords,
    routes: Mapping[str, RouteDecision],
    manifests: Mapping[str, PluginManifest],
    vocab: Vocabulary,
    editorial_methods: frozenset[str],
    stage: Stage,
    unreliable: Callable[[str], frozenset[str]] | None,
) -> list[CompiledBehavior]:
    out: list[CompiledBehavior] = []
    for segment_key in scene.segment_keys:
        route = routes.get(f"tts.segment:{segment_key}")
        scope = words.segment_range(segment_key)
        if route is None or scope is None or route.adapter_id not in manifests:
            continue
        target = _target(
            manifests[route.adapter_id],
            route,
            unreliable,
            editorial_methods,
            node_kind="plan_time" if stage == "plan_time" else "behavior.compile_voice",
            stage=stage,
            channel="audio",
            target_key=segment_key,
            character_key=spec.script.segment(segment_key).speaker_key,
            scope=scope,
            segment_key=segment_key,
            editorial=editorial_context(spec, scene, None, words),
        )
        out.append(compile_behavior(cbs, target, vocab=vocab, words=words))
    for shot in scene.shots:
        route = routes.get(f"avatar.render:{shot.key}:c1:t1")
        rng = words.range(shot.span)
        if route is None or rng is None or not shot.character_key or route.adapter_id not in manifests:
            continue
        first_segment = words.order[rng[0]][0]
        tts = routes.get(f"tts.segment:{first_segment}")
        target = _target(
            manifests[route.adapter_id],
            route,
            unreliable,
            editorial_methods,
            node_kind="plan_time" if stage == "plan_time" else "behavior.compile_visual",
            stage=stage,
            channel="visual",
            target_key=f"{shot.key}:c1",
            character_key=shot.character_key,
            scope=rng,
            shot=rng,
            shot_key=shot.key,
            chunk=1,
            editorial=editorial_context(spec, scene, shot, words),
            tts_matrix=manifests[tts.adapter_id].behavior_matrix if tts and tts.adapter_id in manifests else None,
        )
        out.append(compile_behavior(cbs, target, vocab=vocab, words=words))
    return out


def predicted_coverage(
    cbs_by_scene: Mapping[str, CBSContent],
    compiled: Sequence[CompiledBehavior],
    vocab: Vocabulary,
    *,
    stage: Literal["predicted", "compiled"] = "predicted",
) -> BehaviorCoverageReport:
    """Every requested control with its weakest realization across the targets (I4)."""
    return coverage_report(cbs_by_scene, realized_methods(compiled), vocab, stage=stage)
