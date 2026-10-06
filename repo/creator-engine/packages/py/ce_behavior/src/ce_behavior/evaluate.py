"""The behavior evaluator of dirty analysis (§12.9): `build_graph(..., evaluate=…)` and
`diff_graph(..., evaluate=…)` call it for the cheap behavior nodes of a derived version. It runs
them in process for the parent and the child and returns the digests their consumers read:

- `behavior.resolve` → the CBS content digest;
- `behavior.compile_voice` / `behavior.compile_visual` → the generation digest of the compiled
  output (`directives.generation_digest`);
- `behavior.keyframe_state` → the keyframe state without the CBS digest.

Equal digests mean the consumers' inputs are equal, so generation stays cached. Nodes that reuse
an artifact (`params.reuse`) are not evaluated (the graph marks them clean by construction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ce_build.refs import BuildRefs
from ce_config.loader import ConfigBundle
from ce_core.behavior.cbs import CBSContent
from ce_core.build import ExecutionNode
from ce_core.spec.anchors import WordRef
from ce_core.spec.videospec import VideoSpec
from ce_router import RouterCatalog

from ce_behavior.inputs import cast_inputs, previous_characters, world_input
from ce_behavior.nodes import compile_digest, keyframe_state, keyframe_state_digest, visual_target, voice_target
from ce_behavior.resolve import resolve

__all__ = ["BehaviorEvaluator", "VersionInputs"]


@dataclass
class VersionInputs:
    spec: VideoSpec
    refs: BuildRefs
    _cbs: dict[str, CBSContent] = field(default_factory=dict)

    def cbs(self, scene_key: str, bundle: ConfigBundle) -> CBSContent:
        found = self._cbs.get(scene_key)
        if found is None:
            scene = self.spec.scene(scene_key)
            found = resolve(
                self.spec,
                scene,
                cast=cast_inputs(self.spec, self.refs, scene),
                world=world_input(self.refs, scene),
                vocab=bundle.vocab,
                config=bundle.app.behavior,
                previous_scene_characters=previous_characters(self.spec, scene),
            )
            self._cbs[scene_key] = found
        return found


class BehaviorEvaluator:
    """`evaluator(old_node, new_node) -> (old_digest, new_digest)`; `None` when not computable."""

    def __init__(
        self,
        old: VersionInputs | None,
        new: VersionInputs,
        bundle: ConfigBundle,
        catalog: RouterCatalog,
    ) -> None:
        self.old = old
        self.new = new
        self.bundle = bundle
        self.catalog = catalog
        self._cache: dict[tuple[int, str, str], str | None] = {}

    def __call__(self, old_node: ExecutionNode | None, new_node: ExecutionNode) -> tuple[str | None, str | None]:
        before = self._digest(self.old, old_node) if self.old is not None and old_node is not None else None
        after = self._digest(self.new, new_node)
        return before, after

    def _mode_methods(self, spec: VideoSpec) -> frozenset[str]:
        mode = self.bundle.modes.get(str(spec.meta.mode))
        return frozenset(mode.editorial_methods) if mode else frozenset()

    def _digest(self, side: VersionInputs, node: ExecutionNode) -> str | None:
        key = (id(side), node.key, node.static_digest() + (node.route.digest() if node.route else "-"))
        if key not in self._cache:
            try:
                self._cache[key] = self._compute(side, node)
            except (KeyError, ValueError, LookupError):
                self._cache[key] = None  # not computable: treated as changed
        return self._cache[key]

    def _compute(self, side: VersionInputs, node: ExecutionNode) -> str | None:
        if node.params.get("reuse") is not None or node.scene_key is None:
            return None
        spec = side.spec
        scene = spec.scene(node.scene_key)
        cbs = side.cbs(scene.key, self.bundle)
        if node.kind == "behavior.resolve":
            return cbs.digest()
        vocab = self.bundle.vocab
        unreliable = frozenset(node.params.get("unreliable", []))
        if node.kind == "behavior.compile_voice":
            if node.route is None or node.segment_key is None:
                return None
            target, words = voice_target(
                spec,
                scene,
                node.segment_key,
                manifest=self.catalog.manifests[node.route.adapter_id],
                route=node.route,
                character_key=node.character_key,
                unreliable=unreliable,
                editorial_methods=self._mode_methods(spec),
            )
            return compile_digest(cbs, target, words, vocab)
        if node.kind == "behavior.compile_visual":
            if node.route is None or node.shot_key is None:
                return None
            shot = next(s for s in scene.shots if s.key == node.shot_key)
            first, last = node.params["range"]
            voice = node.params.get("voice_route")
            tts_matrix = None
            if voice is not None:
                for candidate in self.catalog.manifests.values():
                    if candidate.capability("voice.tts") is not None and _matrix_digest(candidate) == voice:
                        tts_matrix = candidate.behavior_matrix
                        break
            target, words = visual_target(
                spec,
                scene,
                shot,
                int(node.params["chunk"]),
                (WordRef(segment_key=first[0], word=int(first[1])), WordRef(segment_key=last[0], word=int(last[1]))),
                manifest=self.catalog.manifests[node.route.adapter_id],
                route=node.route,
                tts_matrix=tts_matrix,
                unreliable=unreliable,
                editorial_methods=self._mode_methods(spec),
            )
            return compile_digest(cbs, target, words, vocab)
        if node.kind == "behavior.keyframe_state":
            if node.shot_key is None:
                return None
            shot = next(s for s in scene.shots if s.key == node.shot_key)
            return keyframe_state_digest(keyframe_state(spec, scene, shot, cbs))
        return None


def _matrix_digest(manifest: Any) -> str:
    from ce_core.canonical import content_digest

    matrix = manifest.behavior_matrix.model_dump(mode="json") if manifest.behavior_matrix else None
    knobs = {k: v.model_dump(mode="json") for k, v in sorted(manifest.knobs.items())}
    return content_digest({"matrix": matrix, "knobs": knobs, "validation": manifest.validation})
