"""`build_graph(spec, refs, bundle, catalog, parent=…)` → ExecutionGraph (§12.1, §12.4).

The graph is derived from the VideoSpec and never stored as the source of truth. For each node it
records the spec fragment, referenced DNA/world/memory fields and config files the node reads (as
digests), its params, seed basis and route.

Routes come from the router, pinned to the parent version's route (§12.4, §12.7):

- a route group (all TTS segments of one character; all chunks and takes of one talking shot) is
  routed once, so a voice never splices engines (§21) and a shot keeps one avatar engine;
- a group is *clean* when every member's route-independent inputs equal the parent's node with the
  same key and all upstream nodes are clean; clean groups reuse the parent's route;
- locked groups that pin routes (voice, appearance, world, music, sfx, broll) keep the parent's
  route even when dirty;
- `force_reroute` (an explicit `reroute` operation) and adapters that no longer pass the hard
  filters re-route even clean nodes, with the reason recorded;
- `replay` rebuilds a parent's own graph on its recorded routes (used to diff against it).

Shot support in Phase 2: talking_head, broll, insert, product (generated or asset), screen and
reaction_clip (asset preparation), title_card (a text card rendered by `render.final`).
`silent_hold` and `two_shot` raise GraphError until the phases that build them (§39.5).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ce_config.loader import ConfigBundle
from ce_core.build import BuildManifest, ExecutionGraph, ExecutionNode, RouteDecision
from ce_core.canonical import content_digest
from ce_core.enums import AnnotationType, ShotType
from ce_core.spec.anchors import DurationSpan, SceneSpan, ShotRef, WordSpan
from ce_core.spec.videospec import Scene, Segment, Shot, VideoSpec
from ce_core.text import tokenize
from ce_router import RouterCatalog, RouteRequest, estimate, route, unreliable_dimensions
from ce_voice.normalize import NORMALIZER_VERSION

from ce_build.dirty import BEHAVIOR_EVALUATED_KINDS, Evaluate
from ce_build.kinds import KINDS
from ce_build.refs import BuildRefs, CreatorRef, VoiceRef, WorldRef
from ce_build.seeds import seed_basis
from ce_build.world import behavior_view, plate_view, plate_view_is_empty

__all__ = [
    "BuildOptions",
    "GraphError",
    "ParentBuild",
    "build_graph",
    "generation_size",
    "metric_route_key",
    "shot_words",
]

PRIORITY_WEIGHT = {"must": 1.0, "should": 0.6, "nice": 0.3}
STATE_DIMENSIONS = ("gaze", "gesture", "posture", "reaction", "camera_awareness")
VOCAL_DIMENSIONS = (
    "emotion_vocal",
    "prosody_rate",
    "prosody_energy",
    "prosody_pitch",
    "prosody_emphasis",
    "prosody_pause",
)
DEFAULT_SFX_S = 1.0
EDITORIAL_MOVES = frozenset({"punch_in", "punch_out"})  # the camera moves editorial methods realize items with
OBSERVE_HZ = 5.0
EMPTY = content_digest({})


class GraphError(ValueError):
    """The spec cannot be turned into an execution graph (unsupported shot type, no route)."""


@dataclass(frozen=True)
class BuildOptions:
    routing_profile: str | None = None  # default: spec.meta.quality_tier (§23)
    provenance_mode: str = "mock_dev"
    lipsync_patch_shots: frozenset[str] = frozenset()
    force_reroute: frozenset[str] = frozenset()  # node keys (any member of a route group)
    sandbox: bool = False
    replay: bool = False
    planned: Mapping[str, RouteDecision] = field(default_factory=dict)  # the version's planned routes (§15.7)
    calibration_sha: str | None = None  # measured proxy calibrations (§16.2): judgement nodes key on them


DEFAULT_OPTIONS = BuildOptions()


@dataclass(frozen=True)
class ParentBuild:
    """The parent version's graph (replayed on its routes) and manifest; `spec` and `outputs`
    (node key → output document sha256) let a voice lock reuse the parent's compiled prosody."""

    graph: ExecutionGraph
    manifest: BuildManifest
    spec: VideoSpec | None = None
    outputs: Mapping[str, str] = field(default_factory=dict)


def _d(value: Any) -> str:
    return content_digest(value)


# Record ids in spec fragments: their content enters through `refs` (DNA digests, asset sha256),
# so a new record version with identical content keeps the cache (§12.2 "digests are content-only").
_RECORD_ID_FIELDS = frozenset({"asset_id", "snapshot_id", "brand_kit_id"})


def _strip_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_ids(v) for k, v in value.items() if k not in _RECORD_ID_FIELDS and not k.endswith("_version_id")
        }
    if isinstance(value, list):
        return [_strip_ids(v) for v in value]
    return value


def _dump(model: Any) -> Any:
    """A spec fragment as content: JSON mode, without record ids."""
    if model is None:
        return None
    if isinstance(model, list):
        return [_dump(m) for m in model]
    if isinstance(model, dict):
        return _strip_ids({k: _dump(v) if hasattr(v, "model_dump") else v for k, v in model.items()})
    return _strip_ids(model.model_dump(mode="json"))


def generation_size(aspect: str, height: int) -> tuple[int, int]:
    """Frame size of `aspect` at `height` pixels tall (`config/routing/*.yaml` budgets are heights,
    §22); dimensions are even. The overscan margin is added by the camera post in Phase 7."""
    w, h = (int(x) for x in aspect.split(":"))
    width = round(height * w / h)
    return width - width % 2, height - height % 2


def shot_words(spec: VideoSpec, scene: Scene, span: WordSpan) -> list[tuple[str, int]]:
    """The (segment_key, word index) pairs a word span covers, in timeline order."""
    order = list(scene.segment_keys)
    a, b = order.index(span.start.segment_key), order.index(span.end.segment_key)
    out: list[tuple[str, int]] = []
    for position in range(a, b + 1):
        key = order[position]
        count = len(tokenize(spec.script.segment(key).text))
        first = span.start.word if position == a else 0
        last = span.end.word if position == b else count - 1
        out += [(key, w) for w in range(first, last + 1)]
    return out


def _base(kind: str, spec: Any, refs: Any, config: Mapping[str, str]) -> str:
    """Route-independent inputs of a node (dirty analysis); params join the cache key only."""
    return _d([kind, KINDS[kind].impl_version, _d(spec), _d(refs), dict(config)])


def _base_of(node: ExecutionNode) -> str:
    return _d([node.kind, node.impl_version, node.spec_digest, node.refs_digest, dict(node.config_digests)])


@dataclass
class _Ctx:
    spec: VideoSpec
    refs: BuildRefs
    bundle: ConfigBundle
    catalog: RouterCatalog
    options: BuildOptions
    parent: ParentBuild | None
    profile: str
    nodes: dict[str, ExecutionNode] = field(default_factory=dict)
    dirty: dict[str, bool] = field(default_factory=dict)
    group_routes: dict[str, RouteDecision] = field(default_factory=dict)
    group_members: dict[str, list[str]] = field(default_factory=dict)
    group_requests: dict[str, RouteRequest] = field(default_factory=dict)
    work_units: dict[str, float] = field(default_factory=dict)
    _parent_index: dict[str, ExecutionNode] | None = None
    evaluate: Evaluate | None = None
    cbs_changed: set[str] = field(default_factory=set)  # resolve nodes whose CBS content changed

    # ------------------------------------------------------------------ lookups
    def segment_language(self, segment: Segment) -> str:
        return segment.language or self.spec.meta.language

    def cast(self, character_key: str) -> Any:
        for member in self.spec.cast:
            if member.key == character_key:
                return member
        raise GraphError(f"character {character_key} is not in the cast")

    def creator(self, character_key: str) -> CreatorRef:
        try:
            return self.refs.creator(self.cast(character_key).creator_version_id)
        except KeyError as exc:
            raise GraphError(str(exc)) from exc

    def voice(self, character_key: str) -> VoiceRef:
        member = self.cast(character_key)
        version_id = member.overrides.voice_version_id or self.creator(character_key).voice_version_id
        if version_id is None or version_id not in self.refs.voices:
            raise GraphError(f"no voice version resolved for {character_key}")
        return self.refs.voices[version_id]

    def appearance_digest(self, character_key: str) -> str:
        member = self.cast(character_key)
        version_id = member.overrides.appearance_version_id or self.creator(character_key).appearance_version_id
        appearance = self.refs.appearances.get(version_id) if version_id else None
        return appearance.digest() if appearance else EMPTY

    def world(self, scene: Scene) -> WorldRef | None:
        if scene.world is None:
            return None
        world = self.refs.worlds.get(scene.world.world_version_id)
        if world is None:
            raise GraphError(f"world version {scene.world.world_version_id} is not resolved")
        return world

    def snapshot_digest(self, character_key: str) -> str:
        for pin in self.spec.memory.snapshots:
            if pin.character_key == character_key:
                snap = self.refs.snapshots.get(pin.snapshot_id)
                return snap.digest if snap else _d(str(pin.snapshot_id))
        return EMPTY

    def asset_sha(self, asset_id: str) -> str:
        """Assets enter digests by content, never by id (§12.2)."""
        for known_id, info in self.refs.assets.items():
            if str(known_id) == asset_id:
                return info.sha256
        raise GraphError(f"asset {asset_id} is not resolved")

    def config(self, *paths: str) -> dict[str, str]:
        return {p: self.bundle.digests[p] for p in paths if p and p in self.bundle.digests}

    def vocab_config(self) -> dict[str, str]:
        return {p: d for p, d in self.bundle.digests.items() if p.startswith("vocab/")}

    def behavior_digest(self) -> str:
        """The behavior engine's settings (`default.yaml` → `behavior`): resolve and compile key on
        this section only, so unrelated `default.yaml` changes keep behavior caches."""
        return _d(self.bundle.app.behavior.model_dump(mode="json"))

    def route_behavior(self, adapter_id: str, anchor: str) -> dict[str, Any]:
        """Compile-node parameters that depend on the routed engine: its declared matrix and knobs
        (digest), the dimensions measured unreliable for it, and the planned engine when the build
        uses another one (coverage downgrade, §15.7)."""
        manifest = self.catalog.manifests[adapter_id]
        matrix = manifest.behavior_matrix.model_dump(mode="json") if manifest.behavior_matrix else None
        knobs = {k: v.model_dump(mode="json") for k, v in sorted(manifest.knobs.items())}
        threshold = self.bundle.qc_behavior.unreliable_control_success_rate if self.bundle.qc_behavior else 0.6
        unreliable = unreliable_dimensions(self.catalog, adapter_id, threshold=threshold)
        planned = self.options.planned.get(anchor)
        out: dict[str, Any] = {
            "matrix": _d({"matrix": matrix, "knobs": knobs, "validation": manifest.validation}),
            "unreliable": sorted(unreliable),
        }
        if planned is not None and planned.adapter_id != adapter_id and planned.adapter_id in self.catalog.manifests:
            out["planned_adapter"] = planned.adapter_id
        return out

    def platform_file(self, preset_id: str) -> str | None:
        for platform_id, platform in self.bundle.platforms.items():
            if any(p.id == preset_id for p in platform.render_presets):
                return f"platforms/{platform_id}.yaml"
        return None

    def locked(
        self, group: str, *, scene: str | None = None, character: str | None = None, shot: str | None = None
    ) -> bool:
        for lock in self.spec.locks:
            if lock.group != group:
                continue
            scope = lock.scope
            if scope.scene_keys is not None and scene is not None and scene not in scope.scene_keys:
                continue
            if scope.character_keys is not None and character is not None and character not in scope.character_keys:
                continue
            if scope.shot_keys is not None and shot is not None and shot not in scope.shot_keys:
                continue
            return True
        return False

    def pins_routes(self, group: str) -> bool:
        lock = self.bundle.vocab.lock_groups.get(group)
        return bool(lock and lock.pins_routes)

    # ------------------------------------------------------------------ cleanliness
    # A node is *dirty* when it is new, its route-independent inputs differ from the parent's node
    # with the same key, a dependency is dirty, or its route differs from the parent's (§12.4).
    def parent_node(self, key: str) -> ExecutionNode | None:
        if self.parent is None:
            return None
        if self._parent_index is None:
            self._parent_index = self.parent.graph.by_key()
        return self._parent_index.get(key)

    def changed(self, key: str, base: str) -> bool:
        """True when the parent has no node `key` or its route-independent inputs differ."""
        old = self.parent_node(key)
        return old is None or _base_of(old) != base

    def probe(
        self,
        kind: str,
        key: str,
        *,
        spec: Any = None,
        refs: Any = None,
        config: Mapping[str, str] | None = None,
        upstream_dirty: bool = False,
    ) -> bool:
        """For a would-be node: True when it existed in the parent and is clean."""
        return (
            not upstream_dirty
            and self.parent_node(key) is not None
            and not self.changed(key, _base(kind, spec, refs, config or {}))
        )

    def clean_in_parent(self, keys: Iterable[str]) -> bool:
        """True when any of `keys` existed in the parent and is clean."""
        return any(self.parent_node(k) is not None and not self.dirty.get(k, True) for k in keys)

    def deps_dirty(self, deps: Iterable[str]) -> bool:
        return any(self.dirty.get(d, True) for d in deps)

    # ------------------------------------------------------------------ nodes
    def add(
        self,
        kind: str,
        key: str,
        *,
        deps: Iterable[str] = (),
        spec: Any = None,
        refs: Any = None,
        config: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        take: int | None = None,
        seeded: bool = False,
        executor: str | None = None,
        capability: str | None = "__kind__",
        group: str | None = None,
        scene: str | None = None,
        shot: str | None = None,
        segment: str | None = None,
        character: str | None = None,
        chunk: int | None = None,
        work_units: float = 1.0,
        assets: Mapping[str, Any] | None = None,
    ) -> ExecutionNode:
        spec_kind = KINDS[kind]
        if assets and "source" in assets and assets["source"] is None:
            raise GraphError(f"{key}: reuses an asset but none is set (library resolution lands in Phase 7)")
        asset_inputs = {name: str(asset_id) for name, asset_id in (assets or {}).items() if asset_id is not None}
        if asset_inputs:
            refs = {"refs": refs, "assets": {name: self.asset_sha(asset_id) for name, asset_id in asset_inputs.items()}}
        deps = list(dict.fromkeys(deps))
        missing = [d for d in deps if d not in self.nodes]
        if missing:
            raise GraphError(f"{key}: dependencies {missing} are not in the graph (construction order)")
        if key in self.nodes:
            raise GraphError(f"duplicate node key {key}")
        node = ExecutionNode(
            key=key,
            kind=kind,
            executor=executor or spec_kind.executor,  # type: ignore[arg-type]
            capability=spec_kind.capability if capability == "__kind__" else capability,
            group=spec_kind.group,
            scene_key=scene,
            shot_key=shot,
            segment_key=segment,
            character_key=character,
            chunk=chunk,
            take=take,
            deps=deps,
            impl_version=spec_kind.impl_version,
            spec_digest=_d(spec),
            refs_digest=_d(refs),
            config_digests=dict(config or {}),
            params=dict(params or {}),
            seed_base=(
                seed_basis(self.spec.generation.seed_namespace, key, take, dict(self.spec.generation.seed_overrides))
                if seeded
                else None
            ),
            reads_requests=spec_kind.reads_requests,
            route_source=group,
            asset_inputs=asset_inputs,
        )
        if group is not None and group in self.group_routes:
            node = node.model_copy(update={"route": self.group_routes[group]})
        self.nodes[key] = node
        self.work_units[key] = work_units
        dirty_deps = [d for d in deps if self.dirty.get(d, True)]
        dirty = bool(dirty_deps) or self.changed(key, _base_of(node))
        rejudge = spec_kind.reads_requests and any(d in self.cbs_changed for d in deps)
        previous = self.parent_node(key)
        evaluable = (
            self.evaluate is not None
            and kind in BEHAVIOR_EVALUATED_KINDS
            and previous is not None
            and all(self.nodes[d].kind in BEHAVIOR_EVALUATED_KINDS for d in dirty_deps)
        )
        if (dirty or rejudge) and evaluable:
            # Cheap CPU behavior node: it is clean for its consumers when its output (the CBS content,
            # or the compiled output generation reads) is unchanged (§12.2, §12.9).
            before, after = self.evaluate(previous, node)  # type: ignore[misc,arg-type]
            dirty = before is None or before != after
        elif rejudge:
            dirty = True  # a judgement node re-reads changed requests
        if kind == "behavior.resolve" and dirty:
            self.cbs_changed.add(key)
        self.dirty[key] = dirty
        if group is not None:
            self.group_members.setdefault(group, []).append(key)
            if group in self.group_routes:
                self._stamp(key, self.group_routes[group])
        return self.nodes[key]

    def mark_clean(self, key: str) -> None:
        """A node whose output is the parent's by construction (a reused artifact)."""
        if self.parent_node(key) is not None:
            self.dirty[key] = False

    def _stamp(self, key: str, decision: RouteDecision) -> None:
        """Sets a member's route; a route that differs from the parent's makes the node dirty."""
        self.nodes[key] = self.nodes[key].model_copy(update={"route": decision})
        parent_route = self.parent.manifest.routes.get(key) if self.parent is not None else None
        if not decision.same_route(parent_route):
            self.dirty[key] = True

    # ------------------------------------------------------------------ routing
    def route_group(
        self,
        group: str,
        request: RouteRequest,
        *,
        dirty: bool,
        anchors: Iterable[str] = (),
        lock_group: str | None = None,
        lock_scope: Mapping[str, str | None] | None = None,
        prefixes: tuple[str, ...] = (),
    ) -> RouteDecision:
        """Routes a group once and stamps the route on its members (existing and future).
        `prefixes` name the node keys of members that do not exist yet (chunks, takes)."""
        if group in self.group_routes:
            return self.group_routes[group]
        members = list(anchors) + self.group_members.get(group, [])
        pinned: RouteDecision | None = None
        if self.parent is not None:
            routes = self.parent.manifest.routes
            candidates = members + sorted(k for k in routes if prefixes and k.startswith(prefixes))
            pinned = next((routes[k] for k in candidates if k in routes), None)
        forced = any(k in self.options.force_reroute for k in members) or any(
            k.startswith(prefixes) for k in self.options.force_reroute if prefixes
        )
        locked = bool(lock_group and self.pins_routes(lock_group) and self.locked(lock_group, **(lock_scope or {})))  # type: ignore[arg-type]
        use_pin = pinned is not None and not forced and (self.options.replay or not dirty or locked)
        req = replace(
            request, routing_profile=self.profile, pinned=pinned if use_pin else None, sandbox=self.options.sandbox
        )
        try:
            decision = route(req, self.catalog)
        except LookupError as exc:
            raise GraphError(str(exc)) from exc
        if use_pin and locked and dirty and decision.pinned:
            decision = decision.model_copy(update={"reason": f"pinned by the {lock_group} lock (§12.7)"})
        self.group_routes[group] = decision
        self.group_requests[group] = req
        for key in self.group_members.get(group, []):
            self._stamp(key, decision)
        return decision

    def single(self, group: str, request: RouteRequest, node: ExecutionNode, **kwargs: Any) -> RouteDecision:
        """Routes a one-node group from that node's own cleanliness."""
        if node.key not in self.group_members.get(group, []):
            self.group_members.setdefault(group, []).append(node.key)
            self.nodes[node.key] = self.nodes[node.key].model_copy(update={"route_source": group})
        return self.route_group(group, request, dirty=self.dirty[node.key], **kwargs)


def _baseline_wpm(ctx: _Ctx, speakers: Iterable[str]) -> dict[str, float]:
    """Each speaker's measured WPM: the baseline of speech-rate proxies (§16.2)."""
    out: dict[str, float] = {}
    for character in speakers:
        segment = next(s for s in ctx.spec.script.segments if s.speaker_key == character)
        out[character] = ctx.voice(character).wpm_for(ctx.segment_language(segment), ctx.bundle.app.spec.default_wpm)
    return out


def _overlay_fragment(spec: VideoSpec) -> list[dict[str, Any]]:
    """Overlay shots by span: viewer-level coverage maps takes through them (§16.4)."""
    return [
        {"scene": scene.key, "shot": shot.key, "span": _dump(shot.span)}
        for scene, shot in spec.shots()
        if str(shot.layer) == "overlay"
    ]


def _editorial_fragment(spec: VideoSpec, scene: Scene, shot: Shot | None) -> dict[str, Any]:
    """The spec elements editorial and structural methods may use (§15.7 build-time pass reads,
    never changes, them): the scene's overlays, the shot's camera moves and own traceability,
    cues anchored in the scene, and the caption settings."""
    overlays = [
        {"shot": s.key, "span": _dump(s.span), "derived_from": _dump(s.derived_from)}
        for s in scene.shots
        if str(s.layer) == "overlay"
    ]
    sfx = [{"key": e.key, "at": _dump(e.at)} for e in spec.audio.sfx]
    music = [{"key": c.key, "span": _dump(c.span)} for c in spec.audio.music.cues]
    return {
        "overlays": overlays,
        "moves": _dump([m for m in shot.camera.moves if str(m.type) in EDITORIAL_MOVES]) if shot is not None else [],
        "shot_derived_from": _dump(shot.derived_from) if shot is not None else [],
        "sfx": sfx,
        "music": music,
        "captions": {"enabled": spec.captions.enabled, "highlight": str(spec.captions.highlight)},
    }


def _requested_dimensions(ctx: _Ctx, scene: Scene, character: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    if scene.acting is None:
        return weights

    def bump(dim: str, priority: str) -> None:
        weights[dim] = max(weights.get(dim, 0.0), PRIORITY_WEIGHT.get(priority, 0.3))

    for state in scene.acting.states:
        if state.character_key == character:
            bump("emotion_visual", str(state.priority))
            for dim in STATE_DIMENSIONS:
                if getattr(state.strategies, dim, None):
                    bump(dim, str(state.priority))
    for event in scene.acting.events:
        definition = ctx.bundle.vocab.events.get(event.type)
        if event.character_key == character and definition is not None:
            bump(definition.dimension, str(event.priority))
    return weights


def _segments_of_span(spec: VideoSpec, span: Any, scene: Scene | None = None) -> list[str]:
    if isinstance(span, WordSpan):
        owner = scene or next(s for s in spec.scenes if span.start.segment_key in s.segment_keys)
        return list(dict.fromkeys(seg for seg, _ in shot_words(spec, owner, span)))
    if isinstance(span, SceneSpan):
        return list(spec.scene(span.scene_key).segment_keys)
    if isinstance(span, DurationSpan):
        segment_key = getattr(span.after, "segment_key", None)
        return [segment_key] if segment_key else []
    return []


def _chunks(
    ctx: _Ctx, scene: Scene, shot: Shot, words: list[tuple[str, int]], chunk_s: float, wpm: float
) -> list[dict[str, Any]]:
    """Chunk word ranges (§12.6): about `chunk_s` each; boundaries snap to state transitions."""
    pauses_ms = 0
    for seg_key in dict.fromkeys(s for s, _ in words):
        for annotation in ctx.spec.script.segment(seg_key).annotations:
            if annotation.type == AnnotationType.PAUSE:
                pauses_ms += annotation.pause_ms or ctx.bundle.vocab.pause_ms.get(annotation.tag, 300)
    estimate = len(words) * 60.0 / max(wpm, 1.0) + pauses_ms / 1000.0
    count = max(1, math.ceil(estimate / max(chunk_s, 1.0)))
    if count == 1:
        return [{"start": list(words[0]), "end": list(words[-1]), "estimated_s": round(estimate, 3)}]
    boundaries: set[tuple[str, int]] = set()
    if scene.acting is not None:
        for state in scene.acting.states:
            if isinstance(state.span, WordSpan) and state.character_key == shot.character_key:
                boundaries.add((state.span.start.segment_key, state.span.start.word))
    cuts: list[int] = []
    per = len(words) / count
    for i in range(1, count):
        target = round(i * per)
        window = range(max(1, target - 3), min(len(words) - 1, target + 3) + 1)
        snapped = next((j for j in sorted(window, key=lambda j: abs(j - target)) if words[j] in boundaries), target)
        if not cuts or snapped > cuts[-1]:
            cuts.append(snapped)
    edges = [0, *cuts, len(words)]
    return [
        {"start": list(words[a]), "end": list(words[b - 1]), "estimated_s": round(estimate * (b - a) / len(words), 3)}
        for a, b in itertools.pairwise(edges)
        if b > a
    ]


def _max_cer(bundle: ConfigBundle, tier: str, language: str) -> float:
    qc = bundle.qc_tiers.get(tier)
    if qc is None:
        return 0.03
    table = qc.checks.speech.max_cer
    return float(table.get(language.split("-")[0], table.get("default", 0.03)))


def _max_wer(bundle: ConfigBundle, tier: str, language: str) -> float:
    qc = bundle.qc_tiers.get(tier)
    if qc is None:
        return 0.05
    table = qc.checks.speech.max_wer
    return float(table.get(language.split("-")[0], table.get("default", 0.08)))


def build_graph(
    spec: VideoSpec,
    refs: BuildRefs,
    bundle: ConfigBundle,
    catalog: RouterCatalog,
    *,
    parent: ParentBuild | None = None,
    options: BuildOptions = DEFAULT_OPTIONS,
    evaluate: Evaluate | None = None,
) -> ExecutionGraph:
    """`evaluate(parent_node, node) -> (old_output_digest, new_output_digest)` runs the cheap
    behavior nodes (Phase 3); a behavior change that leaves a compiled output unchanged then keeps
    every downstream node clean, on its pinned route (§12.4, §12.9)."""
    profile = options.routing_profile or str(spec.meta.quality_tier)
    if profile not in bundle.routing:
        raise GraphError(f"unknown routing profile {profile}")
    ctx = _Ctx(spec, refs, bundle, catalog, options, parent, profile, evaluate=evaluate)
    scenes = sorted(spec.scenes, key=lambda s: s.order)
    primary = spec.render.outputs[0] if spec.render.outputs else None
    preset = bundle.render_preset(primary.preset_id) if primary else None
    aspect = primary.aspect if primary else str(spec.meta.primary_aspect)
    vocab_cfg = ctx.vocab_config()

    # 1. behavior.resolve per scene (CPU, no route)
    previous_characters: dict[str, list[str]] = {}
    for before, after in itertools.pairwise(scenes):
        previous_characters[after.key] = sorted(
            {c.character_key for c in before.cast} | {spec.script.segment(k).speaker_key for k in before.segment_keys}
        )
    for scene in scenes:
        world = ctx.world(scene)
        characters = list(
            dict.fromkeys(
                [c.character_key for c in scene.cast] + [spec.script.segment(k).speaker_key for k in scene.segment_keys]
            )
        )
        ctx.add(
            "behavior.resolve",
            f"behavior.resolve:{scene.key}",
            spec={
                "intent": _dump(scene.intent),
                "acting": _dump(scene.acting),
                "cast": _dump(scene.cast),
                "annotations": {k: _dump(spec.script.segment(k).annotations) for k in scene.segment_keys},
                "locks": [_dump(lock) for lock in spec.locks if lock.group in ("voice", "acting")],
                "world": behavior_view(scene, world, bundle.app.behavior.attention_element_kinds),
                "placement": [c.placement for c in scene.cast],
                "previous_scene_characters": previous_characters.get(scene.key, []),
            },
            refs={
                "dna": {c: ctx.creator(c).behavior_digest() for c in characters},
                "memory": {c: ctx.snapshot_digest(c) for c in characters},
                "world": world.behavior_digest() if world else None,
            },
            config={**vocab_cfg, **ctx.config("intent_policies.yaml")},
            params={"behavior": ctx.behavior_digest()},
            scene=scene.key,
        )

    # 2. voice: per character one TTS route group (no engine splicing within a voice, §21)
    scene_of = {k: s for s in scenes for k in s.segment_keys}
    align_keys: list[str] = []
    speakers = list(dict.fromkeys(seg.speaker_key for seg in spec.script.segments))
    for character in speakers:
        voice = ctx.voice(character)
        member = ctx.cast(character)
        segments = [s for s in spec.script.segments if s.speaker_key == character]
        language = ctx.segment_language(segments[0])
        group = f"tts:{character}"
        pinned = _voice_pins(ctx, character, segments)
        # The members depend on the route (the prepare node exists only for engines that need
        # conditioning), so the group's cleanliness is probed before they are added: the group
        # keeps the parent's route while any segment that existed in the parent is clean.
        voice_fragment = {"character": character}
        prepare_clean = ctx.parent_node(f"voice.prepare:{character}") is None or ctx.probe(
            "voice.prepare",
            f"voice.prepare:{character}",
            spec=voice_fragment,
            refs={"voice": voice.conditioning_digest()},
        )
        clean = False
        for segment in segments:
            fragments = _segment_fragments(ctx, segment, member, voice, pinned.get(segment.key))
            compile_clean = segment.key in pinned or ctx.probe(
                "behavior.compile_voice",
                f"behavior.compile_voice:{segment.key}",
                spec={**fragments["compile"], "editorial": _editorial_fragment(spec, scene_of[segment.key], None)},
                config={**vocab_cfg, **ctx.config(f"modes/{spec.meta.mode}.yaml")},
                upstream_dirty=ctx.dirty[f"behavior.resolve:{scene_of[segment.key].key}"],
            )
            clean = clean or ctx.probe(
                "tts.segment",
                f"tts.segment:{segment.key}",
                spec=fragments["tts"],
                refs={"voice": voice.synthesis_digest()},
                upstream_dirty=not (compile_clean and prepare_clean),
            )
        dirty = not clean
        decision = ctx.route_group(
            group,
            RouteRequest(
                capability="voice.tts",
                language=language,
                requested=dict.fromkeys(VOCAL_DIMENSIONS, 0.6),
                work_units=4.0 * len(segments),
            ),
            dirty=dirty,
            anchors=[f"tts.segment:{s.key}" for s in segments],
            lock_group="voice",
            lock_scope={"character": character},
        )
        if catalog.manifests[decision.adapter_id].capability("voice.clone_prepare") is not None:
            ctx.add(
                "voice.prepare",
                f"voice.prepare:{character}",
                spec=voice_fragment,
                refs={"voice": voice.conditioning_digest()},
                group=group,
                character=character,
            )
        for segment in segments:
            scene = scene_of[segment.key]
            pin = pinned.get(segment.key)
            fragments = _segment_fragments(ctx, segment, member, voice, pin)
            language = ctx.segment_language(segment)
            compile_key = f"behavior.compile_voice:{segment.key}"
            if pin is not None:
                # The voice lock pins delivered audio (§12.7): the parent's compiled prosody is reused
                # as is (by content), so the synthesis below stays the parent's.
                ctx.add(
                    "behavior.compile_voice",
                    compile_key,
                    spec={"reuse": True, "text": segment.text, "language": language},
                    params={"reuse": pin.output_sha},
                    executor="cpu",
                    group=group,
                    scene=scene.key,
                    segment=segment.key,
                    character=character,
                )
                ctx.mark_clean(compile_key)
            else:
                ctx.add(
                    "behavior.compile_voice",
                    compile_key,
                    deps=[f"behavior.resolve:{scene.key}"],
                    spec={**fragments["compile"], "editorial": _editorial_fragment(spec, scene, None)},
                    config={**vocab_cfg, **ctx.config(f"modes/{spec.meta.mode}.yaml")},
                    params={
                        "behavior": ctx.behavior_digest(),
                        **ctx.route_behavior(decision.adapter_id, f"tts.segment:{segments[0].key}"),
                    },
                    group=group,
                    scene=scene.key,
                    segment=segment.key,
                    character=character,
                )
            tts_key = f"tts.segment:{segment.key}"
            prepare = f"voice.prepare:{character}"
            wpm = voice.wpm_for(language, bundle.app.spec.default_wpm) * (1.0 + fragments["tts"]["wpm_delta"])
            ctx.add(
                "tts.segment",
                tts_key,
                deps=[compile_key, *([prepare] if prepare in ctx.nodes else [])],
                spec=fragments["tts"],
                refs={"voice": voice.synthesis_digest()},
                params={"wpm": round(wpm, 4), "sample_rate": 48_000, "normalizer": NORMALIZER_VERSION},
                work_units=len(fragments["tts"]["words"]) * 60.0 / max(wpm, 1.0),
                seeded=True,
                group=group,
                scene=scene.key,
                segment=segment.key,
                character=character,
            )
            verify = ctx.add(
                "asr.verify",
                f"asr.verify:{segment.key}",
                deps=[tts_key],
                spec={"text": segment.text, "language": language},
                params={
                    "max_wer": _max_wer(bundle, str(spec.meta.quality_tier), language),
                    "max_cer": _max_cer(bundle, str(spec.meta.quality_tier), language),
                    "normalizer": NORMALIZER_VERSION,
                },
                scene=scene.key,
                segment=segment.key,
                character=character,
            )
            # Audio from a mock engine is only intelligible to the mock ASR (§37): keep the mocks then.
            mock_audio = catalog.manifests[decision.adapter_id].mock
            ctx.single(
                f"asr:{segment.key}",
                RouteRequest(capability="asr.transcribe", language=language, prefer_mock=mock_audio),
                verify,
            )
            align = ctx.add(
                "align.segment",
                f"align.segment:{segment.key}",
                deps=[tts_key],
                spec={
                    "text": segment.text,
                    "words": fragments["tts"]["words"],
                    "language": language,
                    "tokenizer_version": spec.tokenizer_version,
                },
                scene=scene.key,
                segment=segment.key,
                character=character,
            )
            ctx.single(
                f"align:{segment.key}",
                RouteRequest(capability="asr.align", language=language, prefer_mock=mock_audio),
                align,
            )
            align_keys.append(align.key)

    # 3. per scene: world plate, acoustics, shots
    realism_keys: list[str] = []
    observe_keys: list[str] = []
    compile_keys = [k for k in ctx.nodes if k.startswith("behavior.compile_voice:")]
    room_keys: list[str] = []
    resolve_keys = [f"behavior.resolve:{s.key}" for s in scenes]
    for scene in scenes:
        world = ctx.world(scene)
        plate_key = _world_plate(ctx, scene, world, aspect)
        room = ctx.add(
            "audio.room",
            f"audio.room:{scene.key}",
            deps=[f"tts.segment:{k}" for k in scene.segment_keys],
            spec={
                "acoustics": _dump(spec.audio.acoustics),
                "override": _dump(scene.world.overrides.acoustics) if scene.world else None,
            },
            refs={"world": world.acoustics_digest() if world else None},
            scene=scene.key,
        )
        room_keys.append(room.key)
        for shot in scene.shots:
            key = _build_shot(ctx, scene, shot, world, plate_key, aspect, preset, observe_keys, compile_keys)
            if key:
                realism_keys.append(key)

    # 4. video level: music, SFX
    music_keys: list[str] = []
    for cue in spec.audio.music.cues:
        deps = [f"align.segment:{k}" for k in _segments_of_span(spec, cue.span)]
        key = f"audio.music:{cue.key}"
        if cue.mode == "generate":
            node = ctx.add("audio.music", key, deps=deps, spec=_dump(cue), seeded=True)
            ctx.single(
                f"music:{cue.key}", RouteRequest(capability="audio.music"), node, anchors=[key], lock_group="music"
            )
        else:
            ctx.add(
                "audio.music",
                key,
                deps=deps,
                spec=_dump(cue),
                executor="cpu",
                capability=None,
                assets={"source": cue.asset_id},
            )
        music_keys.append(key)
    sfx_keys: list[str] = []
    for sfx in spec.audio.sfx:
        key = f"audio.sfx:{sfx.key}"
        if sfx.asset_id is None:
            node = ctx.add(
                "audio.sfx",
                key,
                spec={"description": sfx.description},
                params={"duration_s": DEFAULT_SFX_S},
                seeded=True,
            )
            ctx.single(f"sfx:{sfx.key}", RouteRequest(capability="audio.sfx"), node, anchors=[key], lock_group="sfx")
        else:
            ctx.add("audio.sfx", key, executor="cpu", capability=None, assets={"source": sfx.asset_id})
        sfx_keys.append(key)

    # 5. captions
    caption_keys: list[str] = []
    platform = ctx.platform_file(primary.preset_id) if primary else None
    caption_language = spec.captions.language or spec.meta.language
    if spec.captions.enabled and spec.script.segments:
        key = f"captions.build:{caption_language}"
        node = ctx.add(
            "captions.build",
            key,
            deps=align_keys,
            spec={
                "captions": _dump(spec.captions),
                "segments": [
                    {
                        "key": s.key,
                        "text": s.text,
                        "emphasis": [_dump(a.span) for a in s.annotations if a.type == AnnotationType.EMPHASIS],
                    }
                    for s in spec.script.segments
                ],
                "aspect": aspect,
            },
            config=ctx.config(f"caption_styles/{spec.captions.style_id}.yaml", platform or ""),
            params={"width": preset.width if preset else 1080, "height": preset.height if preset else 1920},
        )
        ctx.single(
            "captions", RouteRequest(capability="captions.build", language=caption_language), node, anchors=[key]
        )
        caption_keys.append(key)
        for translation in spec.captions.translations:
            t_key = f"captions.translate:{translation.language}"
            t_node = ctx.add("captions.translate", t_key, deps=[key], spec={"language": translation.language})
            ctx.single(
                f"translate:{translation.language}",
                RouteRequest(capability="captions.translate"),
                t_node,
                anchors=[t_key],
            )

    # 6. mix, renders, provenance, QC, proxy, coverage
    mix = ctx.add(
        "mix.audio",
        "mix.audio:main",
        deps=[*room_keys, *music_keys, *sfx_keys, *align_keys],
        spec={
            "audio": _dump(spec.audio),
            "shots": [{"key": sh.key, "span": _dump(sh.span), "layer": str(sh.layer)} for _, sh in spec.shots()],
            "order": [s.key for s in scenes],
        },
    )
    kit_id = spec.brand.brand_kit_id
    kit = ctx.refs.brand_kits.get(kit_id) if kit_id is not None else None
    if kit_id is not None and kit is None:
        raise GraphError(f"brand kit {kit_id} is not resolved")
    logo = kit.logo.asset_id if kit is not None and kit.logo is not None and spec.brand.logo_overlay else None
    signed: list[str] = []
    for output in spec.render.outputs:
        out_preset = bundle.render_preset(output.preset_id)
        if out_preset is None:
            raise GraphError(f"unknown render preset {output.preset_id}")
        final_key = f"render.final:{output.preset_id}"
        ctx.add(
            "render.final",
            final_key,
            deps=[*realism_keys, mix.key, *caption_keys[:1], *align_keys],
            spec={
                "output": _dump(output),
                "reframe": _dump(spec.render.reframe),
                "shots": [
                    {
                        "scene": sc.key,
                        "key": sh.key,
                        "type": str(sh.type),
                        "layer": str(sh.layer),
                        "span": _dump(sh.span),
                        "title": _dump(sh.title),
                    }
                    for sc, sh in spec.shots()
                ],
                "order": [s.key for s in scenes],
                "captions": spec.captions.enabled,
                "visible_label": spec.provenance.visible_label,
                "effects": _dump(spec.effects),
            },
            refs={"brand_kit": kit.digest} if kit is not None else None,
            assets={"logo": logo} if logo is not None else None,
            config=ctx.config(ctx.platform_file(output.preset_id) or ""),
            params={
                "provenance_mode": options.provenance_mode,
                "width": out_preset.width,
                "height": out_preset.height,
                "fps": out_preset.fps,
            },
        )
        marks: list[str] = []
        for cap in ("provenance.watermark_video", "provenance.watermark_audio"):
            mark = ctx.add(cap, f"{cap}:{output.preset_id}", deps=[final_key], params={"mode": options.provenance_mode})
            ctx.single(f"{cap}:{output.preset_id}", RouteRequest(capability=cap), mark)
            marks.append(mark.key)
        sign = ctx.add(
            "provenance.sign",
            f"provenance.sign:{output.preset_id}",
            deps=marks,
            spec={"consent_ids": [str(c) for c in spec.provenance.consent_ids]},
            params={"mode": options.provenance_mode},
        )
        ctx.single(f"provenance.sign:{output.preset_id}", RouteRequest(capability="provenance.sign"), sign)
        ctx.add(
            "qc.render",
            f"qc.render:{output.preset_id}",
            deps=[sign.key],
            spec={"loudness": _dump(spec.audio.loudness), "target_duration_s": spec.meta.target_duration_s},
            config=ctx.config(f"qc/{spec.meta.quality_tier}.yaml"),
            params={"width": out_preset.width, "height": out_preset.height, "fps": out_preset.fps},
        )
        signed.append(sign.key)
    if signed:
        ctx.add("render.proxy", "render.proxy:proxy", deps=[signed[0]], config=ctx.config("default.yaml"))
        world_keys = [k for k in ctx.nodes if k.startswith("qc.world:")]
        if world_keys:  # background continuity across the video's shots (§19.6)
            ctx.add(
                "qc.continuity",
                "qc.continuity:video",
                deps=[f"render.final:{spec.render.outputs[0].preset_id}", *world_keys],
                config=ctx.config("qc/world.yaml"),
            )
        camera_keys = [k for k in ctx.nodes if k.startswith("post.camera:")]
        captions_keys = [k for k in ctx.nodes if k.startswith("captions.build:")][:1]
        ctx.add(
            "behavior.coverage",
            "behavior.coverage:video",
            deps=[
                f"render.final:{spec.render.outputs[0].preset_id}",
                "mix.audio:main",
                *resolve_keys,
                *compile_keys,
                *observe_keys,
                *align_keys,
                *camera_keys,
                *captions_keys,
            ],
            spec={"captions": _dump(spec.captions), "overlays": _overlay_fragment(spec)},
            refs={"voices": {c: ctx.voice(c).synthesis_digest() for c in speakers}},
            config={**vocab_cfg, **ctx.config("qc/behavior.yaml", f"modes/{spec.meta.mode}.yaml")},
            params={
                "behavior": ctx.behavior_digest(),
                "wpm": _baseline_wpm(ctx, speakers),
                **({"calibration": ctx.options.calibration_sha} if ctx.options.calibration_sha else {}),
            },
        )
        _metric_routes(ctx, "behavior.coverage:video", ("audio.prosody",))
    unrouted = [n.key for n in ctx.nodes.values() if n.executor == "model" and n.route is None]
    if unrouted:
        raise GraphError(f"model nodes without a route: {unrouted}")
    nodes = [_with_estimate(ctx, n) for n in ctx.nodes.values()]
    return ExecutionGraph(spec_content_digest=spec.content_digest(), routing_profile=profile, nodes=nodes)


def _with_estimate(ctx: _Ctx, node: ExecutionNode) -> ExecutionNode:
    """Seconds and USD for model nodes from their route (impact preview); CPU work is not priced."""
    if node.executor != "model" or node.route is None or node.route_source not in ctx.group_requests:
        return node
    request = replace(ctx.group_requests[node.route_source], work_units=ctx.work_units.get(node.key, 1.0), pinned=None)
    seconds, usd = estimate(node.route.adapter_id, request, ctx.catalog)
    return node.model_copy(update={"estimate": {"seconds": round(seconds, 3), "usd": round(usd, 6)}})


@dataclass(frozen=True)
class _VoicePin:
    output_sha: str  # the parent's compile_voice output document
    wpm_delta: float  # the parent's scene pacing, kept with the delivered audio


def _wpm_delta(scene: Scene | None) -> float:
    return float(scene.pacing.target_wpm_delta) if scene is not None and scene.pacing is not None else 0.0


def _voice_pins(ctx: _Ctx, character: str, segments: list[Segment]) -> dict[str, _VoicePin]:
    """Segments whose delivered audio a voice lock pins (§12.7): the character's voice is locked,
    the segment's text and language are the parent's, and the parent's compiled prosody is known."""
    parent = ctx.parent
    if parent is None or parent.spec is None or not ctx.locked("voice", character=character):
        return {}
    old_segments = {s.key: s for s in parent.spec.script.segments}
    old_scenes = {k: sc for sc in parent.spec.scenes for k in sc.segment_keys}
    out: dict[str, _VoicePin] = {}
    for segment in segments:
        old = old_segments.get(segment.key)
        sha = parent.outputs.get(f"behavior.compile_voice:{segment.key}")
        if old is None or sha is None or old.speaker_key != character or old.text != segment.text:
            continue
        if (old.language or parent.spec.meta.language) != ctx.segment_language(segment):
            continue
        out[segment.key] = _VoicePin(sha, _wpm_delta(old_scenes.get(segment.key)))
    return out


def _segment_fragments(
    ctx: _Ctx, segment: Segment, member: Any, voice: VoiceRef, pin: _VoicePin | None = None
) -> dict[str, Any]:
    """`compile_voice` reads the text, annotations and per-video prosody; `tts.segment` reads what
    the synthesis request carries (the text and words, its language and the pacing that scales the
    voice's rate, §19.5 "pacing: re-performance"); annotations reach it only through the compiled
    prosody."""
    language = ctx.segment_language(segment)
    scene = next((sc for sc in ctx.spec.scenes if segment.key in sc.segment_keys), None)
    return {
        "compile": {
            "text": segment.text,
            "language": language,
            "annotations": _dump(segment.annotations),
            "voice_prosody": _dump(member.voice_prosody),
        },
        "tts": {
            "text": segment.text,
            "language": language,
            "words": [t.text for t in tokenize(segment.text)],
            "voice_prosody": _dump(member.voice_prosody),
            "wpm_delta": pin.wpm_delta if pin is not None else _wpm_delta(scene),
        },
    }


def _continuity_refs(ctx: _Ctx, binding: Any, world: WorldRef) -> dict[str, Any]:
    """A `continuity_ref` by content (§12.2): the referenced plate, asset or shot look as a hash."""
    ref = binding.continuity_ref
    if ref is None:
        return {}
    kind = getattr(ref, "kind", None)
    if kind == "world_plate":
        plate = world.plate(ref.camera_position_key, str(binding.time_of_day), str(binding.weather)) or (
            world.nearest_plate(ref.camera_position_key)
        )
        return {"continuity": {"kind": kind, "plate": plate.sha256 if plate else ref.camera_position_key}}
    if kind == "asset":
        return {"continuity": {"kind": kind, "sha256": ctx.asset_sha(str(ref.asset_id))}}
    if kind == "shot":
        found = ctx.refs.shot_artifacts.get(f"{ref.version_id}:{ref.shot_key}")
        if found is None:
            raise GraphError(f"continuity shot {ref.shot_key} of version {ref.version_id} is not resolved")
        return {"continuity": {"kind": kind, "sha256": found}}
    return {}


def _world_plate(ctx: _Ctx, scene: Scene, world: WorldRef | None, aspect: str) -> str | None:
    """One plate per scene binding (§19.2). Its inputs are exactly what a plate from the bound
    camera position shows (§19.5): `plate_view` and the per-position world digest."""
    if scene.world is None or world is None:
        return None
    binding = scene.world
    key = f"world.plate:{scene.key}"
    view = plate_view(binding, world)
    fragment = {"plate": view}
    refs = {
        "world": world.plate_digest(binding.camera_position_key, str(binding.time_of_day), str(binding.weather)),
        **_continuity_refs(ctx, binding, world),
    }
    canonical = world.plate(binding.camera_position_key, str(binding.time_of_day), str(binding.weather))
    same_position_ref = binding.continuity_ref is None or (
        getattr(binding.continuity_ref, "kind", None) == "world_plate"
        and getattr(binding.continuity_ref, "camera_position_key", None) == binding.camera_position_key
    )
    if canonical is not None and plate_view_is_empty(view) and same_position_ref:
        ctx.add(
            "world.plate",
            key,
            spec=fragment,
            refs=refs,
            assets={"source": canonical.asset_id},
            executor="cpu",
            capability=None,
            scene=scene.key,
        )
        return key
    width, height = generation_size(aspect, ctx.bundle.routing[ctx.profile].generation.max_height)
    nearest = world.nearest_plate(binding.camera_position_key)
    capability = "image.edit" if nearest else "image.generate"
    node = ctx.add(
        "world.plate",
        key,
        spec=fragment,
        refs=refs,
        params={"width": width, "height": height},
        assets={"base_plate": nearest.asset_id if nearest else None},
        seeded=True,
        capability=capability,
        scene=scene.key,
    )
    ctx.single(
        f"plate:{scene.key}",
        RouteRequest(capability=capability, height=height, width=width),
        node,
        anchors=[key],
        lock_group="world",
        lock_scope={"scene": scene.key},
    )
    return key


def _build_shot(
    ctx: _Ctx,
    scene: Scene,
    shot: Shot,
    world: WorldRef | None,
    plate_key: str | None,
    aspect: str,
    preset: Any,
    observe_keys: list[str],
    compile_keys: list[str],
) -> str:
    """Builds one shot's chain and returns its `post.realism` key ("" for text cards)."""
    bundle = ctx.bundle
    routing = bundle.routing[ctx.profile]
    profile = bundle.camera_profiles.get(shot.camera.profile_id)
    camera_cfg = ctx.config(f"camera_profiles/{shot.camera.profile_id}.yaml", profile.color.lut if profile else "")
    width, height = generation_size(aspect, routing.generation.max_height)
    shot_type = ShotType(shot.type)
    if shot_type in (ShotType.SILENT_HOLD, ShotType.TWO_SHOT):
        raise GraphError(f"shot {shot.key}: {shot_type} shots are not built in Phase 2 (§39.5)")
    if shot_type == ShotType.TITLE_CARD:
        return ""

    if shot_type == ShotType.TALKING_HEAD:
        upstream = _talking_chain(ctx, scene, shot, plate_key, width, height, camera_cfg, observe_keys, compile_keys)
    elif shot_type in (ShotType.SCREEN, ShotType.REACTION_CLIP):
        asset = shot.screen.asset_id if shot.screen else shot.reaction_source.asset_id if shot.reaction_source else None
        # The recording runs on the shot's output clock (§27): its zoom and speed spans resolve on
        # the scene's aligned words, and on the spans of other shots they are anchored after.
        spans = (
            [shot.span, *(z.span for z in shot.screen.zooms), *(s.span for s in shot.screen.speed_segments)]
            if shot.screen
            else []
        )
        anchored = sorted(
            {
                sp.after.shot_key
                for sp in spans
                if isinstance(sp, DurationSpan) and isinstance(sp.after, ShotRef) and sp.after.shot_key != shot.key
            }
        )
        others = {s.key: s for s in scene.shots}
        node = ctx.add(
            "screen.prepare",
            f"screen.prepare:{shot.key}",
            deps=[f"align.segment:{k}" for k in scene.segment_keys] if shot.screen else [],
            spec={
                "screen": _dump(shot.screen),
                "reaction": _dump(shot.reaction_source),
                "span": _dump(shot.span),
                "anchor_shots": {k: _dump(others[k].span) for k in anchored if k in others},
            },
            params={
                "width": preset.width if preset else width,
                "height": preset.height if preset else height,
                "fps": preset.fps if preset is not None else routing.generation.fps,
            },
            assets={"source": asset},
            scene=scene.key,
            shot=shot.key,
        )
        upstream = [node.key]
    else:
        upstream = _broll_chain(ctx, scene, shot, plate_key, width, height)

    # final tier: mandatory upscale and frame interpolation (§22)
    target_height = preset.height if preset is not None else height
    target_fps = preset.fps if preset is not None else routing.generation.fps
    if routing.upscale_required and height < target_height:
        tw, th = (preset.width, preset.height) if preset else generation_size(aspect, target_height)
        node = ctx.add(
            "video.upscale",
            f"video.upscale:{shot.key}",
            deps=upstream,
            params={"target_width": tw, "target_height": th},
            scene=scene.key,
            shot=shot.key,
        )
        ctx.single(f"upscale:{shot.key}", RouteRequest(capability="video.upscale"), node)
        upstream = [node.key]
    if routing.interpolate == "video.interpolate" and routing.generation.fps != target_fps:
        node = ctx.add(
            "video.interpolate",
            f"video.interpolate:{shot.key}",
            deps=upstream,
            params={"target_fps": target_fps},
            scene=scene.key,
            shot=shot.key,
        )
        ctx.single(f"interpolate:{shot.key}", RouteRequest(capability="video.interpolate"), node)
        upstream = [node.key]

    camera = ctx.add(
        "post.camera",
        f"post.camera:{shot.key}",
        deps=upstream,
        spec={
            "camera": _dump(shot.camera),
            "aspect": aspect,
            "layer": str(shot.layer),
            "selected_take_key": shot.takes.selected_take_key,
            "cut_cadence": scene.pacing.cut_cadence if scene.pacing is not None else None,
        },
        config=camera_cfg,
        params={
            "width": preset.width if preset else width,
            "height": preset.height if preset else height,
            "fps": target_fps,
        },
        seeded=True,
        scene=scene.key,
        shot=shot.key,
    )
    if str(ctx.spec.render.reframe.strategy) == "subject_aware":
        _metric_routes(ctx, camera.key, ("face.detect",))  # reframing tracks the subject (§22)
    realism = ctx.add(
        "post.realism",
        f"post.realism:{shot.key}",
        deps=[camera.key],
        config=camera_cfg,
        seeded=True,
        scene=scene.key,
        shot=shot.key,
    )
    if world is not None and (
        shot_type == ShotType.TALKING_HEAD or (shot.broll is not None and shot.broll.world_bound)
    ):
        assert scene.world is not None
        binding = scene.world
        ctx.add(
            "qc.world",
            f"qc.world:{shot.key}",
            deps=[realism.key, *([plate_key] if plate_key else [])],
            spec={"plate": plate_view(binding, world)},
            refs={
                "world": world.plate_digest(
                    binding.camera_position_key, str(binding.time_of_day), str(binding.weather)
                ),
                **_continuity_refs(ctx, binding, world),
            },
            config=ctx.config("qc/world.yaml"),
            scene=scene.key,
            shot=shot.key,
        )
        _metric_routes(ctx, f"qc.world:{shot.key}", ("image.embed", "face.detect"), optional=("vision.image",))
    return realism.key


def _talking_chain(
    ctx: _Ctx,
    scene: Scene,
    shot: Shot,
    plate_key: str | None,
    width: int,
    height: int,
    camera_cfg: dict[str, str],
    observe_keys: list[str],
    compile_keys: list[str],
) -> list[str]:
    spec, bundle = ctx.spec, ctx.bundle
    character = shot.character_key
    assert character is not None
    if not isinstance(shot.span, WordSpan):
        raise GraphError(f"talking shot {shot.key} needs a word span")
    words = shot_words(spec, scene, shot.span)
    segments = list(dict.fromkeys(s for s, _ in words))
    scene_cast = next((c for c in scene.cast if c.character_key == character), None)
    wardrobe = (
        ctx.refs.wardrobes.get(scene_cast.wardrobe_version_id)
        if scene_cast and scene_cast.wardrobe_version_id
        else None
    )
    creator = ctx.creator(character)
    voice = ctx.voice(character)
    language = ctx.segment_language(spec.script.segment(segments[0]))
    resolve_key = f"behavior.resolve:{scene.key}"
    wpm = voice.wpm_for(language, bundle.app.spec.default_wpm)

    state = ctx.add(
        "behavior.keyframe_state",
        f"behavior.keyframe_state:{shot.key}",
        deps=[resolve_key],
        spec={"start": _dump(shot.span.start), "cast": _dump(scene_cast), "framing": shot.camera.framing},
        config=ctx.vocab_config(),
        params={"behavior": ctx.behavior_digest()},
        scene=scene.key,
        shot=shot.key,
        character=character,
    )
    keyframe_key = f"image.keyframe:{shot.key}"
    fragment = {
        "camera": {"profile_id": shot.camera.profile_id, "framing": shot.camera.framing, "angle": shot.camera.angle},
        "visual": _dump(shot.visual),
        "character": character,
    }
    identity = {
        "appearance": ctx.appearance_digest(character),
        "wardrobe": wardrobe.digest() if wardrobe else None,
        "display_name": creator.display_name,
    }
    visual = shot.visual
    if visual is not None and visual.keyframe.strategy == "asset" and visual.keyframe.asset_id is not None:
        ctx.add(
            "image.keyframe",
            keyframe_key,
            deps=[state.key],
            spec=fragment,
            refs=identity,
            executor="cpu",
            capability=None,
            assets={"source": visual.keyframe.asset_id},
            scene=scene.key,
            shot=shot.key,
            character=character,
        )
    else:
        node = ctx.add(
            "image.keyframe",
            keyframe_key,
            deps=[state.key, *([plate_key] if plate_key else [])],
            spec=fragment,
            refs=identity,
            config=camera_cfg,
            params={"width": width, "height": height},
            seeded=True,
            scene=scene.key,
            shot=shot.key,
            character=character,
        )
        ctx.single(
            f"keyframe:{shot.key}",
            RouteRequest(capability="image.edit", height=height, width=width),
            node,
            anchors=[keyframe_key],
            lock_group="appearance",
            lock_scope={"character": character},
        )

    # One avatar route per shot (all chunks and takes). The chunk plan depends on the route, so
    # cleanliness is probed with the parent's route: the shot keeps it while any chunk × take that
    # existed in the parent is clean.
    covered_tts = [f"tts.segment:{s}" for s in segments]
    covered_align = [f"align.segment:{s}" for s in segments]
    anchor = f"avatar.render:{shot.key}:c1:t1"
    group = f"avatar:{shot.key}"

    def plan(adapter_id: str) -> tuple[int, list[dict[str, Any]]]:
        manifest = ctx.catalog.manifests[adapter_id]
        temporal = manifest.behavior_matrix.temporal_control if manifest.behavior_matrix else None
        decl = manifest.capability("avatar.a2v")
        fps = decl.fps if decl and decl.fps else bundle.routing[ctx.profile].generation.fps
        return int(fps), _chunks(ctx, scene, shot, words, temporal.recommended_chunk_s if temporal else 20.0, wpm)

    editorial = _editorial_fragment(spec, scene, shot)

    def compile_fragment(chunk: dict[str, Any]) -> dict[str, Any]:
        return {"span": _dump(shot.span), "chunk": chunk, "editorial": editorial}

    compile_config = {**ctx.vocab_config(), **ctx.config(f"modes/{spec.meta.mode}.yaml")}

    def render_fragment(chunk: dict[str, Any]) -> dict[str, Any]:
        return {"chunk": chunk, "camera_profile": shot.camera.profile_id}

    render_refs = {"display_name": creator.display_name}
    parent_route = ctx.parent.manifest.routes.get(anchor) if ctx.parent is not None else None
    clean = False
    if parent_route is not None and parent_route.adapter_id in ctx.catalog.manifests:
        compile_upstream_dirty = ctx.deps_dirty([resolve_key, *covered_align])
        _, old_chunks = plan(parent_route.adapter_id)
        previous_clean = {t: True for t in range(1, shot.takes.count + 1)}
        for c_index, chunk in enumerate(old_chunks, start=1):
            compile_clean = ctx.probe(
                "behavior.compile_visual",
                f"behavior.compile_visual:{shot.key}:c{c_index}",
                spec=compile_fragment(chunk),
                config=compile_config,
                upstream_dirty=compile_upstream_dirty,
            )
            for take in previous_clean:
                take_clean = previous_clean[take] and ctx.probe(
                    "avatar.render",
                    f"avatar.render:{shot.key}:c{c_index}:t{take}",
                    spec=render_fragment(chunk),
                    refs=render_refs,
                    config=camera_cfg,
                    upstream_dirty=not compile_clean or ctx.deps_dirty([keyframe_key, *covered_tts]),
                )
                previous_clean[take] = take_clean  # chunk k continues from chunk k-1 (§12.6)
                clean = clean or take_clean
    decision = ctx.route_group(
        group,
        RouteRequest(
            capability="avatar.a2v",
            language=language,
            height=height,
            width=width,
            requested=_requested_dimensions(ctx, scene, character),
            engine_hint=spec.generation.engine_hints.get(anchor),
            work_units=len(words) * 60.0 / max(wpm, 1.0),
        ),
        dirty=not clean,
        anchors=[anchor],
        lock_group="appearance",
        lock_scope={"character": character},
        prefixes=(f"avatar.render:{shot.key}:", f"behavior.compile_visual:{shot.key}:"),
    )
    fps, chunks = plan(decision.adapter_id)
    tts_node = ctx.nodes.get(covered_tts[0]) if covered_tts else None
    voice_route = (
        ctx.route_behavior(tts_node.route.adapter_id, covered_tts[0])["matrix"] if tts_node and tts_node.route else None
    )

    chunk_keys: dict[int, list[str]] = {}
    shot_compile_keys: list[str] = []
    # Lip-sync patch strategy (§28 alternatives, §30 `strategy: lipsync_patch`): keep the parent's
    # renders of this shot by content and dub the new audio onto them (`lipsync.patch`).
    patched: dict[str, str] = {}
    if shot.key in ctx.options.lipsync_patch_shots and ctx.parent is not None:
        wanted = [
            f"avatar.render:{shot.key}:c{c}:t{t}"
            for c in range(1, len(chunks) + 1)
            for t in range(1, shot.takes.count + 1)
        ]
        found = {k: ctx.parent.outputs.get(k) for k in wanted}
        if all(found.values()):
            patched = {k: v for k, v in found.items() if v}
    for c_index, chunk in enumerate(chunks, start=1):
        compile_node = ctx.add(
            "behavior.compile_visual",
            f"behavior.compile_visual:{shot.key}:c{c_index}",
            deps=[resolve_key, *covered_align],
            spec=compile_fragment(chunk),
            config=compile_config,
            params={
                "chunk": c_index,
                "chunks": len(chunks),
                "range": [chunk["start"], chunk["end"]],
                "behavior": ctx.behavior_digest(),
                "voice_route": voice_route,
                **ctx.route_behavior(decision.adapter_id, anchor),
            },
            group=group,
            scene=scene.key,
            shot=shot.key,
            character=character,
            chunk=c_index,
        )
        compile_keys.append(compile_node.key)
        shot_compile_keys.append(compile_node.key)
        for take in range(1, shot.takes.count + 1):
            key = f"avatar.render:{shot.key}:c{c_index}:t{take}"
            previous = [f"avatar.render:{shot.key}:c{c_index - 1}:t{take}"] if c_index > 1 else []
            if key in patched:
                ctx.add(
                    "avatar.render",
                    key,
                    spec={"reuse": True, "chunk": chunk},
                    params={"reuse": patched[key]},
                    executor="cpu",
                    capability=None,
                    take=take,
                    scene=scene.key,
                    shot=shot.key,
                    character=character,
                    chunk=c_index,
                )
                ctx.mark_clean(key)
                chunk_keys.setdefault(take, []).append(key)
                continue
            ctx.add(
                "avatar.render",
                key,
                deps=[keyframe_key, compile_node.key, *covered_tts, *covered_align, *previous],
                spec=render_fragment(chunk),
                refs=render_refs,
                config=camera_cfg,
                params={
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "chunk": c_index,
                    "chunks": len(chunks),
                    "range": [chunk["start"], chunk["end"]],
                },
                work_units=float(chunk["estimated_s"]),
                take=take,
                seeded=True,
                group=group,
                scene=scene.key,
                shot=shot.key,
                character=character,
                chunk=c_index,
            )
            chunk_keys.setdefault(take, []).append(key)

    observe_group = f"observe:{shot.key}"
    take_observe: dict[int, str] = {}
    for take, keys in chunk_keys.items():
        observe = ctx.add(
            "behavior.observe",
            f"behavior.observe:{shot.key}:t{take}",
            deps=keys,
            params={"sample_hz": OBSERVE_HZ},
            take=take,
            scene=scene.key,
            shot=shot.key,
            character=character,
            group=observe_group,
        )
        _metric_routes(ctx, observe.key, ("body.landmarks",))
        take_observe[take] = observe.key
        observe_keys.append(observe.key)
    ctx.route_group(
        observe_group, RouteRequest(capability="face.landmarks"), dirty=not ctx.clean_in_parent(take_observe.values())
    )
    qc_keys: list[str] = []
    for take, keys in chunk_keys.items():
        qc = _qc_shot(
            ctx,
            f"qc.shot:{shot.key}:t{take}",
            deps=[*keys, take_observe[take], resolve_key, *shot_compile_keys],
            config=ctx.config(f"qc/{spec.meta.quality_tier}.yaml", "qc/behavior.yaml"),
            metrics=("qc.vqa", "qc.lipsync"),
            take=take,
            scene=scene.key,
            shot=shot.key,
            character=character,
        )
        qc_keys.append(qc)
    upstream = [k for keys in chunk_keys.values() for k in keys] + qc_keys
    if shot.key in ctx.options.lipsync_patch_shots or patched:
        patch = ctx.add(
            "lipsync.patch",
            f"lipsync.patch:{shot.key}",
            deps=[*upstream, *covered_align],
            seeded=True,
            scene=scene.key,
            shot=shot.key,
        )
        ctx.single(f"lipsync:{shot.key}", RouteRequest(capability="lipsync.dub"), patch)
        upstream = [*upstream, patch.key]
    expression = ctx.add(
        "post.expression",
        f"post.expression:{shot.key}",
        deps=upstream,
        spec={"selected_take_key": shot.takes.selected_take_key},
        scene=scene.key,
        shot=shot.key,
        character=character,
    )
    ctx.single(f"expression:{shot.key}", RouteRequest(capability="expression.edit"), expression)
    return [expression.key]


def metric_route_key(node_key: str, capability: str) -> str:
    """Manifest key of an in-process QC metric route (`qc.shot:sht_1:t1/qc.vqa`)."""
    return f"{node_key}/{capability}"


# The VLM judge's prompt version (`ce_qc.vlm_judge.JUDGE_VERSION`): a new prompt re-judges takes.
VLM_JUDGE_VERSION = "vlm-judge-v1"


def _qc_shot(
    ctx: _Ctx, key: str, *, deps: list[str], config: dict[str, str], metrics: tuple[str, ...], **where: Any
) -> str:
    """A `qc.shot` node. The metric adapters it calls in-process (VQA, lip sync) are routes like any
    other (`_metric_routes`); measured proxy calibrations enter its key (§16.2). The VLM judge
    (`vision.video`, §26) is routed when a VLM passes the hard filters, and skipped otherwise."""
    params: dict[str, Any] = {"calibration": ctx.options.calibration_sha} if ctx.options.calibration_sha else {}
    params["vlm_judge"] = VLM_JUDGE_VERSION
    ctx.add("qc.shot", key, deps=deps, config=config, params=params, **where)
    _metric_routes(ctx, key, metrics, optional=("vision.video",))
    return key


def _metric_routes(ctx: _Ctx, key: str, capabilities: tuple[str, ...], *, optional: tuple[str, ...] = ()) -> None:
    """In-process analyzer and metric routes of node `key`: part of its params (so of its key) and
    recorded in the manifest under `metric_route_key`. A clean node keeps its parent's routes while
    they still pass the hard filters (§12.4); otherwise each capability is routed."""
    node = ctx.nodes[key]
    pinned: dict[str, RouteDecision] = {}
    if ctx.parent is not None and (ctx.options.replay or not ctx.dirty[key]):
        for capability in (*capabilities, *optional):
            decision = ctx.parent.manifest.routes.get(metric_route_key(key, capability))
            if decision is not None:
                pinned[capability] = decision
    routes: dict[str, dict[str, Any]] = {}
    for capability in (*capabilities, *optional):
        request = RouteRequest(
            capability=capability,
            routing_profile=ctx.profile,
            sandbox=ctx.options.sandbox,
            pinned=pinned.get(capability),
            exclude=frozenset(),
        )
        cache_key = f"metric:{capability}:{request.pinned.digest() if request.pinned else '-'}"
        if cache_key not in ctx.group_routes:
            try:
                ctx.group_routes[cache_key] = route(request, ctx.catalog)
            except LookupError as exc:
                if capability in optional:  # e.g. no VLM may run here: the check is reported as skipped
                    continue
                raise GraphError(str(exc)) from exc
        routes[capability] = ctx.group_routes[cache_key].identity()  # identity only: scores never enter keys
    ctx.nodes[key] = node.model_copy(update={"params": {**node.params, "metrics": routes}})
    if any(not RouteDecision.model_validate(r).same_route(pinned.get(c)) for c, r in routes.items()):
        ctx.dirty[key] = True


def _broll_chain(ctx: _Ctx, scene: Scene, shot: Shot, plate_key: str | None, width: int, height: int) -> list[str]:
    """B-roll, inserts and product shots: generated takes (ranked by `qc.shot`) or a user asset."""
    spec = ctx.spec
    broll = shot.broll
    align_deps = [f"align.segment:{s}" for s in _segments_of_span(spec, shot.span, scene)]
    fragment = {"broll": _dump(broll), "span": _dump(shot.span), "product": shot.product_key, "type": str(shot.type)}
    if broll is None or broll.source != "generate":
        asset = broll.asset_id if broll else None
        node = ctx.add(
            "video.broll",
            f"video.broll:{shot.key}:t1",
            deps=align_deps,
            spec=fragment,
            executor="cpu",
            capability=None,
            assets={"source": asset},
            take=1,
            scene=scene.key,
            shot=shot.key,
        )
        return [node.key]
    use_plate = bool(broll.world_bound and plate_key)
    capability = "video.i2v" if use_plate else "video.t2v"
    group = f"broll:{shot.key}"
    take_keys: list[str] = []
    for take in range(1, shot.takes.count + 1):
        node = ctx.add(
            "video.broll",
            f"video.broll:{shot.key}:t{take}",
            deps=[*align_deps, *([plate_key] if use_plate and plate_key else [])],
            spec=fragment,
            params={"width": width, "height": height},
            take=take,
            seeded=True,
            capability=capability,
            group=group,
            scene=scene.key,
            shot=shot.key,
        )
        take_keys.append(node.key)
    decision = ctx.route_group(
        group,
        RouteRequest(capability=capability, height=height, width=width),
        dirty=not ctx.clean_in_parent(take_keys),
        lock_group="broll",
        lock_scope={"scene": scene.key, "shot": shot.key},
    )
    decl = ctx.catalog.manifests[decision.adapter_id].capability(capability)
    fps = decl.fps if decl and decl.fps else ctx.bundle.routing[ctx.profile].generation.fps
    for key in take_keys:
        ctx.nodes[key] = ctx.nodes[key].model_copy(update={"params": {**ctx.nodes[key].params, "fps": fps}})
    qc_keys = [
        _qc_shot(
            ctx,
            f"qc.shot:{shot.key}:t{take}",
            deps=[key, f"behavior.resolve:{scene.key}"],
            config=ctx.config(f"qc/{spec.meta.quality_tier}.yaml"),
            metrics=("qc.vqa",),
            take=take,
            scene=scene.key,
            shot=shot.key,
        )
        for take, key in enumerate(take_keys, start=1)
    ]
    return [*take_keys, *qc_keys]
