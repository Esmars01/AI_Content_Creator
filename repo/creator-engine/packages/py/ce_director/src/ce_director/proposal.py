"""An edit proposal, computed without persisting anything (§28 steps 3–7): operations → SpecPatch,
validation (schema, references and vocabulary, locks, policy), the impact of the derived version
(`diff_graph` with the behavior evaluator), the predicted coverage delta, and alternative
strategies (editorial-only, lip-sync patch, full re-performance).

`ce_exec.editing` loads the inputs from the database and stores the result as an
`edit_proposals` row; the acceptance tests call it directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ce_behavior.evaluate import BehaviorEvaluator, VersionInputs
from ce_behavior.plan import compile_version, predicted_coverage, version_cbs
from ce_build import BuildOptions, GraphError, Impact, ParentBuild, build_graph, diff_graph
from ce_build.refs import BuildRefs
from ce_config.loader import ConfigBundle
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_core.build import ExecutionGraph
from ce_core.edit.diff import area_of, spec_diff
from ce_core.edit.locks import lock_violations
from ce_core.edit.ops import (
    AddBehaviorEvent,
    EditOperation,
    EditScope,
    NewMove,
    SetActing,
    SetBehaviorEvent,
    SetCamera,
    operation_list,
)
from ce_core.edit.translate import EditError, TranslateContext, Translation, translate
from ce_core.errors import Issue
from ce_core.spec.validate import ReferenceLookup, ValidationContext, errors_only, validate_spec
from ce_core.spec.videospec import VideoSpec
from ce_policy import BlocklistChecker
from ce_router import RouterCatalog
from pydantic import ValidationError

__all__ = ["Proposal", "ProposalInputs", "compute_proposal"]

ACTING_OPS = (SetActing, AddBehaviorEvent, SetBehaviorEvent)
GENERATION_PREFIXES = (
    "tts.",
    "voice.",
    "image.",
    "avatar.",
    "video.",
    "world.plate",
    "audio.music",
    "audio.sfx",
    "lipsync.",
)


@dataclass
class ProposalInputs:
    parent_spec: VideoSpec
    parent_refs: BuildRefs
    bundle: ConfigBundle
    catalog: RouterCatalog
    references: ReferenceLookup  # approved records, for validate_spec
    refs_for: Callable[[VideoSpec], BuildRefs]  # the build references of a candidate spec
    parent_build: ParentBuild | None = None  # None: the parent never generated (previz review)
    actor: str = "user"
    allow_lock_removal: bool = False
    options: BuildOptions = field(default_factory=BuildOptions)


@dataclass
class Proposal:
    operations: list[EditOperation]
    status: str  # proposed | failed
    issues: list[Issue] = field(default_factory=list)
    spec: VideoSpec | None = None
    patch: dict[str, Any] = field(default_factory=dict)
    impact: dict[str, Any] = field(default_factory=dict)
    coverage_delta: dict[str, Any] = field(default_factory=dict)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    graph: ExecutionGraph | None = None
    build: dict[str, list[str]] = field(default_factory=dict)  # force_reroute, lipsync_patch_shots
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    coverage_report: BehaviorCoverageReport | None = None  # predicted coverage of the derived version

    def ops_json(self) -> list[dict[str, Any]]:
        return operation_list.dump_python(self.operations, mode="json")


def _graph(
    inputs: ProposalInputs, spec: VideoSpec, refs: BuildRefs, options: BuildOptions
) -> tuple[ExecutionGraph, Impact, BehaviorEvaluator]:
    parent = inputs.parent_build
    evaluator = BehaviorEvaluator(
        VersionInputs(inputs.parent_spec, inputs.parent_refs), VersionInputs(spec, refs), inputs.bundle, inputs.catalog
    )
    if parent is None:
        old = build_graph(inputs.parent_spec, inputs.parent_refs, inputs.bundle, inputs.catalog, options=inputs.options)
        parent = ParentBuild(old, _manifest_of(old), spec=inputs.parent_spec)
    graph = build_graph(spec, refs, inputs.bundle, inputs.catalog, parent=parent, options=options, evaluate=evaluator)
    impact = diff_graph(parent.graph, graph, evaluate=evaluator, parent_outputs=parent.outputs)
    return graph, impact, evaluator


def _manifest_of(graph: ExecutionGraph) -> Any:
    from ce_build import assemble, records_for

    return assemble([r for n in graph.nodes for r in records_for(n, artifact_id=None, effective_seed=n.seed_base)])


def _blocklist(bundle: ConfigBundle, before: VideoSpec, after: VideoSpec) -> list[Issue]:
    """Edited text is checked like a plan's (§32): changed segments, titles and prompts only."""
    old = {s.key: s.text for s in before.script.segments}
    texts = [(f"/script/segments[{s.key}]/text", s.text) for s in after.script.segments if old.get(s.key) != s.text]
    if after.meta.title != before.meta.title:
        texts.append(("/meta/title", after.meta.title))
    for scene, shot in after.shots():
        if shot.broll is not None:
            texts.append((f"/scenes[{scene.key}]/shots[{shot.key}]/broll/prompt", shot.broll.prompt))
    checker = BlocklistChecker.from_config(bundle.blocklists)
    return [
        Issue(code="policy_blocklist", message=f.message, path=f.refs[0] if f.refs else None, detail=f.detail)
        for f in checker.findings(texts)
    ]


def _coverage(
    inputs: ProposalInputs, spec: VideoSpec, refs: BuildRefs, graph: ExecutionGraph
) -> BehaviorCoverageReport:
    vocab = inputs.bundle.vocab
    cbs = version_cbs(spec, refs, vocab, inputs.bundle.app.behavior)
    routes = {n.key: n.route for n in graph.nodes if n.route is not None}
    mode = inputs.bundle.modes.get(str(spec.meta.mode))
    compiled = compile_version(
        spec,
        cbs,
        routes,
        inputs.catalog.manifests,
        vocab,
        editorial_methods=frozenset(mode.editorial_methods) if mode else frozenset(),
    )
    return predicted_coverage(cbs, compiled, vocab, stage="compiled")


def _requested(spec: VideoSpec, refs: BuildRefs, inputs: ProposalInputs) -> dict[tuple[str, str], dict[str, Any]]:
    cbs = version_cbs(spec, refs, inputs.bundle.vocab, inputs.bundle.app.behavior)
    return {
        (c.item_ref, c.dimension): c.model_dump(mode="json")
        for content in cbs.values()
        for c in content.requested_controls
    }


def _coverage_delta(
    inputs: ProposalInputs,
    spec: VideoSpec,
    refs: BuildRefs,
    old_graph: ExecutionGraph,
    new_graph: ExecutionGraph,
    impact: Impact,
) -> tuple[dict[str, Any], BehaviorCoverageReport]:
    """Predicted coverage before and after (§28 step 6), per requested item: what changes, what the
    engine cannot show (`no_visible_effect`) and what a lock holds back (`blocked_by_lock`). Also
    returns the derived version's predicted report (its plan report carries it)."""
    before = _coverage(inputs, inputs.parent_spec, inputs.parent_refs, old_graph)
    after = _coverage(inputs, spec, refs, new_graph)
    old_req = _requested(inputs.parent_spec, inputs.parent_refs, inputs)
    new_req = _requested(spec, refs, inputs)
    old_entries = {(e.item_ref, e.dimension): e for e in before.entries}
    new_entries = {(e.item_ref, e.dimension): e for e in after.entries}
    pinned_segments = {
        n.segment_key for n in new_graph.nodes if n.kind == "behavior.compile_voice" and n.params.get("reuse")
    }
    audio_dims = {name for name, d in inputs.bundle.vocab.dimensions.items() if d.channel == "audio"}
    executes = set(impact.executes)
    items: list[dict[str, Any]] = []
    for key in sorted(set(old_entries) | set(new_entries)):
        old, new = old_entries.get(key), new_entries.get(key)
        entry: dict[str, Any] = {
            "item_ref": key[0],
            "dimension": key[1],
            "before": {"level": str(old.compiled.level), "method": str(old.compiled.method)} if old else None,
            "after": {"level": str(new.compiled.level), "method": str(new.compiled.method)} if new else None,
        }
        requested_changed = old_req.get(key) != new_req.get(key)
        if old is None:
            entry["change"] = "added"
        elif new is None:
            entry["change"] = "removed"
        elif entry["before"] != entry["after"]:
            entry["change"] = "level_changed"
        elif requested_changed:
            entry["change"] = "request_changed"
        else:
            continue
        if requested_changed and new is not None and key[1] in audio_dims:
            segments = _segments_of_item(spec, key[0])
            if segments and segments <= pinned_segments:
                entry["blocked_by_lock"] = "voice"
                entry["reason"] = "the voice lock keeps the delivered audio; vocal expression cannot change"
        if requested_changed and new is not None and "blocked_by_lock" not in entry:
            if str(new.compiled.level) == "UNSUPPORTED":
                entry["no_visible_effect"] = True
                entry["reason"] = "the routed engines cannot show this; nothing is re-rendered for it"
            elif not any(k.startswith(GENERATION_PREFIXES) for k in executes):
                entry["no_visible_effect"] = True
                entry["reason"] = "the change leaves every compiled output unchanged"
        items.append(entry)
    summary_before = before.summary_counts()
    summary_after = after.summary_counts()
    delta = {
        "items": items,
        "summary": {"before": summary_before, "after": summary_after},
        "blocked_by_lock": sorted({i["blocked_by_lock"] for i in items if "blocked_by_lock" in i}),
        "no_visible_effect": [
            {"item_ref": i["item_ref"], "dimension": i["dimension"], "reason": i["reason"]}
            for i in items
            if i.get("no_visible_effect")
        ],
    }
    return delta, after.model_copy(update={"stage": "predicted", "version_id": spec.version_id})


def _segments_of_item(spec: VideoSpec, item_ref: str) -> set[str]:
    """The segments an acting item's span covers (its state or event), or the annotated segment."""
    from ce_core.spec.anchors import WordSpan
    from ce_core.spec.paths import SpecPath, SpecPathError

    try:
        segments = SpecPath.parse(item_ref).segments
    except SpecPathError:
        return set()
    if segments and segments[0].field == "script" and len(segments) > 1 and segments[1].selector:
        return {segments[1].selector}
    if len(segments) >= 3 and segments[0].field == "scenes" and segments[1].field == "acting":
        scene = next((s for s in spec.scenes if s.key == segments[0].selector), None)
        if scene is None or scene.acting is None:
            return set()
        if segments[2].field == "states":
            state = next((s for s in scene.acting.states if s.key == segments[2].selector), None)
            span = state.span if state else None
        else:
            event = next((e for e in scene.acting.events if e.key == segments[2].selector), None)
            span = (
                event.span
                if event and event.span
                else (WordSpan(start=event.at, end=event.at) if event and event.at else None)
            )
        if isinstance(span, WordSpan):
            order = list(scene.segment_keys)
            a, b = order.index(span.start.segment_key), order.index(span.end.segment_key)
            return set(order[a : b + 1])
    return set()


def _impact_dict(
    impact: Impact, graph: ExecutionGraph, translation: Translation, spec: VideoSpec, parent: VideoSpec
) -> dict[str, Any]:
    out = impact.as_dict()
    by_key = graph.by_key()
    blocking: list[dict[str, str]] = []
    for node in graph.nodes:
        reason = node.route.reason if node.route else ""
        if node.key in impact.executes and "lock" in reason:
            blocking.append({"node_key": node.key, "reason": reason})
    voice_kept = sorted(
        n.segment_key or "" for n in graph.nodes if n.kind == "behavior.compile_voice" and n.params.get("reuse")
    )
    if voice_kept:
        blocking.append(
            {"group": "voice", "reason": f"delivered audio kept for unchanged text ({', '.join(voice_kept)})"}
        )
    out["locks_blocking"] = [*out.get("locks_blocking", []), *blocking]
    out["notes"] = list(translation.notes)
    out["rebase"] = list(translation.rebase)
    out["generation"] = sorted(k for k in impact.executes if k.startswith(GENERATION_PREFIXES))
    out["diff"] = [
        {**e.as_dict(), "area": area_of(e.path)} for e in spec_diff(parent.content_dict(), spec.content_dict())
    ]
    out["model_nodes"] = [k for k in impact.executes if by_key[k].executor == "model"]
    return out


def _editorial_only(inputs: ProposalInputs, ops: Sequence[EditOperation], spec: VideoSpec) -> list[EditOperation]:
    """An editorial take on acting edits: a punch-in where each changed state or new event starts,
    keeping every performance as it is."""
    moves: list[EditOperation] = []
    for op in ops:
        if isinstance(op, SetActing):
            for scene in spec.scenes:
                if op.scope.scene_keys is not None and scene.key not in op.scope.scene_keys:
                    continue
                shots = [s.key for s in scene.shots if str(s.type) == "talking_head"]
                if not shots:
                    continue
                at = op.scope.span.start if op.scope.span else None
                moves.append(
                    SetCamera(
                        scope=EditScope(shot_keys=shots[:1]),
                        add_moves=[NewMove(type="punch_in", at=at, scale=1.12)],
                        reason="editorial emphasis instead of a new performance",
                    )
                )
        elif isinstance(op, AddBehaviorEvent) and op.event.at is not None:
            owner = next((s for s in spec.scenes if op.event.at.segment_key in s.segment_keys), None)
            shots = [s.key for s in owner.shots if str(s.type) == "talking_head"] if owner else []
            if shots:
                moves.append(
                    SetCamera(
                        scope=EditScope(shot_keys=shots[:1]),
                        add_moves=[NewMove(type="punch_in", at=op.event.at, scale=1.08)],
                        reason="a punch-in on the beat instead of the gesture",
                    )
                )
    return moves


def _try(
    inputs: ProposalInputs, ops: Sequence[EditOperation], *, options: BuildOptions | None = None
) -> tuple[Translation, VideoSpec, BuildRefs, ExecutionGraph, Impact] | None:
    try:
        translation = _translate(inputs, ops)
        spec = VideoSpec.model_validate(translation.document)
        refs = inputs.refs_for(spec)
        build = replace(
            options or inputs.options,
            force_reroute=frozenset(translation.force_reroute),
            lipsync_patch_shots=frozenset(translation.lipsync_patch_shots)
            | (options or inputs.options).lipsync_patch_shots,
        )
        graph, impact, _ = _graph(inputs, spec, refs, build)
    except (EditError, ValidationError, GraphError, KeyError, LookupError):
        return None
    return translation, spec, refs, graph, impact


def _emotion_range(inputs: ProposalInputs) -> Callable[[str, str], tuple[float, float] | None]:
    """Creator DNA intensity bounds per character (§10.6): edits stay inside them."""
    ranges: dict[str, dict[str, Any]] = {}
    for member in inputs.parent_spec.cast:
        creator = inputs.parent_refs.creators.get(member.creator_version_id)
        if creator is not None:
            ranges[member.key] = dict((creator.dna.get("behavior") or {}).get("emotion_ranges") or {})

    def lookup(character: str, label: str) -> tuple[float, float] | None:
        bounds = ranges.get(character, {}).get(label)
        return (float(bounds[0]), float(bounds[1])) if bounds else None

    return lookup


def _translate(inputs: ProposalInputs, ops: Sequence[EditOperation]) -> Translation:
    worlds = {str(w.version_id): w.dna for w in inputs.parent_refs.worlds.values()}
    ctx = TranslateContext(
        vocab=inputs.bundle.vocab,
        worlds=_WorldDNAs(inputs, worlds),
        camera_profiles=frozenset(inputs.bundle.camera_profiles),
        render_presets=frozenset(
            p.id for platform in inputs.bundle.platforms.values() for p in platform.render_presets
        ),
        platform_presets={
            pid: [(p.id, str(p.aspect)) for p in platform.render_presets]
            for pid, platform in inputs.bundle.platforms.items()
        },
        node_keys=[n.key for n in inputs.parent_build.graph.nodes] if inputs.parent_build else _node_keys(inputs),
        emotion_range=_emotion_range(inputs),
        actor=inputs.actor,
        allow_lock_removal=inputs.allow_lock_removal,
    )
    return translate(ops, inputs.parent_spec.model_dump(mode="json"), ctx)


class _WorldDNAs(dict[str, Mapping[str, Any]]):
    """World DNA by version id, loading the versions an operation names on demand."""

    def __init__(self, inputs: ProposalInputs, known: Mapping[str, Mapping[str, Any]]) -> None:
        super().__init__(known)
        self.inputs = inputs

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            lookup = self.inputs.references.world_version(_uuid(key))
            if lookup is not None:
                self[key] = lookup.dna.model_dump(mode="json")
        return super().get(key, default)


def _uuid(value: str) -> Any:
    from uuid import UUID

    try:
        return UUID(value)
    except ValueError:
        return value


def _node_keys(inputs: ProposalInputs) -> list[str]:
    graph = build_graph(inputs.parent_spec, inputs.parent_refs, inputs.bundle, inputs.catalog, options=inputs.options)
    return [n.key for n in graph.nodes]


def compute_proposal(inputs: ProposalInputs, operations: Sequence[EditOperation]) -> Proposal:
    ops = list(operations)
    try:
        translation = _translate(inputs, ops)
    except EditError as exc:
        return Proposal(ops, "failed", issues=exc.issues)
    try:
        spec = VideoSpec.model_validate(translation.document)
    except ValidationError as exc:
        problems = [
            Issue(code="invalid_spec", message=e["msg"], path="/" + "/".join(str(p) for p in e["loc"]))
            for e in exc.errors()[:20]
        ]
        return Proposal(ops, "failed", issues=problems)
    issues: list[Issue] = []
    removed = translation.removed_locks
    active = [lock for lock in inputs.parent_spec.model_dump(mode="json")["locks"] if lock not in removed]
    for violation in lock_violations(
        inputs.parent_spec.model_dump(mode="json"), translation.document, active, inputs.bundle.vocab
    ):
        issues.append(Issue(code="locked", message=violation.message, path=violation.path, detail=violation.as_dict()))
    vctx = ValidationContext(
        vocab=inputs.bundle.vocab,
        refs=inputs.references,
        multi_character_enabled=inputs.bundle.app.features.multi_character_enabled,
        duration_tolerance=inputs.bundle.app.spec.duration_tolerance,
        default_wpm=inputs.bundle.app.spec.default_wpm,
        require_memory_snapshots=inputs.bundle.app.spec.require_memory_snapshots,
    )
    issues += errors_only(validate_spec(spec, vctx))
    issues += _blocklist(inputs.bundle, inputs.parent_spec, spec)
    if issues:
        return Proposal(ops, "failed", issues=issues, spec=spec, patch=translation.patch.model_dump(mode="json"))
    refs = inputs.refs_for(spec)
    options = replace(
        inputs.options,
        force_reroute=frozenset(translation.force_reroute),
        lipsync_patch_shots=frozenset(translation.lipsync_patch_shots),
    )
    try:
        graph, impact, _ = _graph(inputs, spec, refs, options)
    except GraphError as exc:
        return Proposal(ops, "failed", issues=[Issue(code="graph", message=str(exc))], spec=spec)
    old_graph = (
        inputs.parent_build.graph
        if inputs.parent_build
        else build_graph(inputs.parent_spec, inputs.parent_refs, inputs.bundle, inputs.catalog, options=inputs.options)
    )
    coverage, predicted = _coverage_delta(inputs, spec, refs, old_graph, graph, impact)
    alternatives = _alternatives(inputs, ops, spec, impact, translation)
    return Proposal(
        ops,
        "proposed",
        spec=spec,
        patch=translation.patch.model_dump(mode="json"),
        impact=_impact_dict(impact, graph, translation, spec, inputs.parent_spec),
        coverage_delta=coverage,
        alternatives=alternatives,
        graph=graph,
        build={"force_reroute": translation.force_reroute, "lipsync_patch_shots": translation.lipsync_patch_shots},
        side_effects=translation.side_effects,
        coverage_report=predicted,
    )


def _summary(impact: Impact) -> dict[str, Any]:
    return {
        "estimate": impact.estimate,
        "generation": sorted(k for k in impact.executes if k.startswith(GENERATION_PREFIXES)),
    }


def _alternatives(
    inputs: ProposalInputs, ops: list[EditOperation], spec: VideoSpec, impact: Impact, translation: Translation
) -> list[dict[str, Any]]:
    """§28 step 7: the full re-performance (this proposal) and, for acting edits, an editorial-only
    take and — when the voice re-synthesizes but no wording changed — a lip-sync patch."""
    out = [{"strategy": "full_reperformance", "operations": None, **_summary(impact), "selected": True}]
    if not any(isinstance(op, ACTING_OPS) for op in ops):
        return out
    editorial = _editorial_only(inputs, ops, inputs.parent_spec)
    if editorial:
        tried = _try(inputs, editorial)
        if tried is not None:
            out.append(
                {
                    "strategy": "editorial_only",
                    "operations": operation_list.dump_python(editorial, mode="json"),
                    **_summary(tried[4]),
                    "selected": False,
                    "explanation": "Keeps the performance; emphasis comes from the edit (punch-ins on the beats).",
                }
            )
    resynthesized = {k.split(":", 1)[1] for k in impact.executes if k.startswith("tts.segment:")}
    texts_same = all(
        s.text == inputs.parent_spec.script.segment(s.key).text
        for s in spec.script.segments
        if s.key in resynthesized and any(p.key == s.key for p in inputs.parent_spec.script.segments)
    )
    if resynthesized and texts_same and inputs.parent_build is not None:
        talking = [
            shot.key
            for scene in spec.scenes
            for shot in scene.shots
            if str(shot.type) == "talking_head" and set(scene.segment_keys) & resynthesized
        ]
        tried = _try(inputs, ops, options=replace(inputs.options, lipsync_patch_shots=frozenset(talking)))
        if tried is not None:
            out.append(
                {
                    "strategy": "lipsync_patch",
                    "operations": None,
                    "lipsync_patch_shots": talking,
                    **_summary(tried[4]),
                    "selected": False,
                    "explanation": "New voice delivery; the existing takes are kept and their lips re-synced.",
                }
            )
    return out
