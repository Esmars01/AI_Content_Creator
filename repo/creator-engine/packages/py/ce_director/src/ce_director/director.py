"""The AI Director (§13): typed stages 1–11 from free text to a validated VideoSpec and its
plan report.

| # | Stage | How |
| --- | --- | --- |
| 1 | interpret | LLM → `BriefOut`; exact scripts are located by character offsets, never retyped |
| 2 | context | code: cast choice, world options, the pinned MemorySnapshot (I7), recent usage |
| 3 | research | code: pasted text, URLs (SSRF-guarded) and attached persistent sources; hybrid |
|  |  | (keyword + embedding) retrieval; closed book = user-provided sources only |
| 4 | strategy | LLM → `StrategyOut`; the repetition guard rejects hooks too close to recent ones |
| 5 | script | LLM → `ScriptOut` (AI: lines; exact: boundaries, sliced and verified by code) |
| 6 | fact check | LLM → `FactCheckOut`; evidence re-checked by code (`ce_research.claims`), detected |
|  |  | claims added; contradiction checker vs canon and memory |
| 7 | scenes | LLM → `ScenesOut`; code binds worlds and plans shots |
| 8 | acting | LLM → `ActingOut` with a route preview; code builds states, triggers and events |
| 9–10 | camera, edit, sound | code: the intent policy engine with `derived_from: intent` |
| 11 | validate, resolve | code: validation, blocklists, testimonial guard, CBS, plan-time routing, compiler |
|  |  | proposals (`derived_from: compiler_approximation`), predicted coverage, plan report |

Every LLM stage goes through `ce_llm.structured` with a check that lists closed-vocabulary
problems (I13) and the stage's structural problems; still-unknown labels are mapped to the
nearest vocabulary item and recorded as assumptions. A stage whose output stays invalid falls
back to the template for that stage (recorded). A fixture miss plans the whole input with the
labeled template Director (`planner: template`, §37). Untrusted text reaches prompts only inside
data blocks (I10).
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar
from uuid import UUID

from ce_behavior.plan import compile_version, predicted_coverage, version_cbs
from ce_build import build_graph, planned_routes
from ce_build.refs import BuildRefs, SnapshotRef
from ce_config.loader import ConfigBundle
from ce_config.schemas import DirectorConfig, Mode
from ce_core.behavior.cbs import CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.plan_report import Finding, PlanReport, StateTiming, event_timings
from ce_core.build import RouteDecision
from ce_core.enums import RecordStatus
from ce_core.ids import new_id
from ce_core.keys import KeyKind
from ce_core.spec.validate import (
    CreatorVersionInfo,
    InMemoryReferences,
    OwnedVersionInfo,
    SnapshotInfo,
    ValidationContext,
    VoiceVersionInfo,
    WorldVersionInfo,
    validate_spec,
)
from ce_core.spec.videospec import Claim, VideoSpec
from ce_core.text import TOKENIZER_VERSION, tokenize
from ce_llm import (
    FixtureMiss,
    LLMProvider,
    PromptLibrary,
    StructuredOutputError,
    scenario_key,
    structured,
    wrap_data,
)
from ce_memory import MemoryRecord, MemoryRetriever, PersonaAssertion, RepetitionGuard, RetrievalContext, SnapshotDraft
from ce_memory.contradictions import script_contradictions
from ce_memory.usage import usage_payload
from ce_policy import BlocklistChecker, GuardResult, TestimonialGuard
from ce_research import GuardedFetcher, ResearchResult, gather
from ce_research.claims import CheckedClaim, ClaimEvidence, enforce
from ce_research.ingest import HybridIndex, StoredFact, fact_id
from ce_router.router import RouterCatalog, preview
from ce_voice import ExactScriptError, extract_segments, parse_tags, sentence_spans
from pydantic import BaseModel

from ce_director import build as B
from ce_director import template as T
from ce_director.context import ContextPack, DirectorContext, choose_creator
from ce_director.draft import SpecDraft, word_span
from ce_director.intent_policy import IntentPolicyEngine, PolicyDecision, PolicySet
from ce_director.models import (
    ActingOut,
    BriefOut,
    FactCheckOut,
    PlanRequest,
    ScenesOut,
    ScriptOut,
    StrategyOut,
    WordAt,
)
from ce_director.runs import RunLog, StageRun
from ce_director.timing import estimate_timeline, nearest_word, target_word_count
from ce_director.vocabmap import ControlField, VocabMapper

__all__ = ["Director", "DirectorDeps", "PlanOutcome", "PlanningError"]

Out = TypeVar("Out", bound=BaseModel)
RefsFor = Callable[[VideoSpec, Mapping[UUID, SnapshotRef]], Awaitable[BuildRefs]]
# texts, language → (vectors, model string) or None (no in-process embed.text adapter); Phase 12
EmbedFn = Callable[[Sequence[str], str | None], Awaitable[tuple[list[list[float]], str] | None]]

C = ControlField
INTERPRET_CONTROLS = [
    C("setting.world_kind", "world_kind"),
    C("setting.posture", "strategy.posture"),
    C("time_requests[].label", "emotion", optional=False),
]
STRATEGY_CONTROLS = [
    C("beats[].purpose", "intent.scene_purpose", optional=False),
    C("video_intent.narrative_goal", "intent.narrative_goal"),
    C("video_intent.audience_effect", "intent.audience_effect"),
    C("video_intent.persuasion_goal", "intent.persuasion_goal"),
    C("video_intent.information_goal", "intent.information_goal"),
    C("video_intent.emotional_arc[]", "emotion", optional=False),
    C("video_intent.attention_goal", "intent.attention_goal"),
    C("video_intent.cta_goal", "intent.cta_goal"),
]
SCRIPT_CONTROLS: list[ControlField] = []  # annotation types and tags are checked by `_annotation_problems`
SCENE_CONTROLS = [
    C("scenes[].purpose", "intent.scene_purpose", optional=False),
    C("scenes[].intent.narrative_goal", "intent.narrative_goal"),
    C("scenes[].intent.emotional_goal", "intent.emotional_goal"),
    C("scenes[].intent.audience_effect", "intent.audience_effect"),
    C("scenes[].intent.persuasion_goal", "intent.persuasion_goal"),
    C("scenes[].intent.information_goal", "intent.information_goal"),
    C("scenes[].intent.attention_goal", "intent.attention_goal"),
    C("scenes[].intent.reveal_strategy", "intent.reveal_strategy"),
    C("scenes[].intent.performance_strategy", "intent.performance_strategy"),
    C("scenes[].framing", "camera.framing"),
]
ACTING_CONTROLS = [
    C("scenes[].situation.kind", "situation_kind", optional=False),
    C("scenes[].situation.audience_stance", "audience_stance", optional=False),
    C("scenes[].situation.stimulus.kind", "stimulus_kind"),
    C("scenes[].states[].internal_state", "internal_state", optional=False),
    C("scenes[].states[].social_goal", "social_goal", optional=False),
    C("scenes[].states[].audience_goal", "audience_goal", optional=False),
    C("scenes[].states[].performance_intent", "performance_intent", optional=False),
    C("scenes[].states[].felt.label", "emotion", optional=False),
    C("scenes[].states[].displayed.label", "emotion", optional=False),
    C("scenes[].states[].strategies.prosody", "strategy.prosody", optional=False),
    C("scenes[].states[].strategies.gaze", "strategy.gaze", optional=False),
    C("scenes[].states[].strategies.gesture", "strategy.gesture", optional=False),
    C("scenes[].states[].strategies.posture", "strategy.posture", optional=False),
    C("scenes[].states[].strategies.reaction", "strategy.reaction", optional=False),
    C("scenes[].states[].strategies.camera_awareness", "strategy.camera_awareness", optional=False),
    C("scenes[].states[].transition.style", "transition_style"),
    C("scenes[].states[].transition.trigger_kind", "trigger_kind"),
    C("scenes[].events[].type", "event_type", optional=False),
    C("scenes[].events[].direction", "direction"),
    C("scenes[].events[].purpose", "event_purpose"),
]
ANNOTATION_TYPES = ("pause", "emphasis", "nonverbal_audio", "delivery", "pronunciation", "inserted_disfluency")
IGNORED_EARLY = frozenset({"missing_acting", "missing_states", "duration", "emotional_arc", "memory_pin"})


class PlanningError(Exception):
    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message + ("".join(f"\n- {p}" for p in problems[:20]) if problems else ""))
        self.problems = list(problems)


class _StageFailed(Exception):
    def __init__(self, stage: str, problems: Sequence[str]) -> None:
        super().__init__(f"{stage}: {'; '.join(problems[:5])}")
        self.stage = stage
        self.problems = list(problems)


@dataclass
class DirectorDeps:
    bundle: ConfigBundle
    catalog: RouterCatalog
    prompts: PromptLibrary
    provider: LLMProvider | None
    refs_for: RefsFor
    fetcher: GuardedFetcher | None = None
    allow_template: bool = True  # plan with the template Director when no fixture matches
    ids: Callable[[], UUID] = new_id
    embed: EmbedFn | None = None  # embed.text (Phase 12): memory ranking, hooks, evidence, vocabulary


@dataclass
class PlanOutcome:
    spec: VideoSpec
    report: PlanReport
    planner: str
    runs: list[StageRun]
    snapshots: list[tuple[UUID, SnapshotDraft]]  # new snapshots to persist (I7)
    planned_routes: dict[str, Any]
    decisions: list[PolicyDecision]
    research: ResearchResult
    cbs: dict[str, CBSContent]
    compiled: list[CompiledBehavior]
    testimonial: GuardResult | None
    route_preview: dict[str, list[str]]
    claims: list[CheckedClaim] = field(default_factory=list)  # the claim ledger's input (Phase 12)
    evidence: dict[str, StoredFact] = field(default_factory=dict)  # evidence id → fact (traceability)


@dataclass
class _Plan:
    request: PlanRequest
    ctx: DirectorContext
    scenario: str
    llm: bool
    log: RunLog = field(default_factory=RunLog)
    assumptions: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    brief: BriefOut | None = None
    mode: Mode | None = None
    pack: ContextPack | None = None
    research: ResearchResult | None = None
    index: HybridIndex | None = None  # in-memory chunks + persistent facts (Phase 12)
    evidence_by_id: dict[str, StoredFact] = field(default_factory=dict)
    claim_checks: list[CheckedClaim] = field(default_factory=list)
    strategy: StrategyOut | None = None
    draft: SpecDraft | None = None
    hints: list[B.TagHints] = field(default_factory=list)
    segment_beats: list[tuple[str, int]] = field(default_factory=list)
    reveal: dict[str, tuple[str, int] | None] = field(default_factory=dict)
    choices: dict[str, B.WorldChoice] = field(default_factory=dict)
    policies: PolicySet = field(default_factory=PolicySet)
    decisions: list[PolicyDecision] = field(default_factory=list)
    resolved_seconds: dict[str, dict[int, tuple[str, int]]] = field(default_factory=dict)
    requested_seconds: dict[tuple[str, int], float] = field(default_factory=dict)  # (scene, state idx) → s
    route_preview: dict[str, list[str]] = field(default_factory=dict)
    template_stages: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.assumptions:
            self.assumptions.append(text)


class Director:
    def __init__(self, deps: DirectorDeps) -> None:
        self.deps = deps
        self.bundle = deps.bundle
        config = deps.bundle.director
        if config is None or deps.bundle.intent_policies is None or deps.bundle.memory is None:
            raise PlanningError("config/director.yaml, intent_policies.yaml and memory.yaml are required")
        self.config: DirectorConfig = config
        self.vocab = deps.bundle.vocab
        self.mapper = VocabMapper(self.vocab)
        self.engine = IntentPolicyEngine(deps.bundle.intent_policies)
        self.guard = RepetitionGuard(deps.bundle.memory.repetition)
        self.retriever = MemoryRetriever(deps.bundle.memory.retrieval)

    # ================================================================== entry point
    async def plan(self, request: PlanRequest, ctx: DirectorContext) -> PlanOutcome:
        scenario = scenario_key(request.input)
        use_llm = self.deps.provider is not None
        try:
            return await self._run(_Plan(request, ctx, scenario, llm=use_llm))
        except FixtureMiss:
            if not self.deps.allow_template:
                raise
        plan = _Plan(request, ctx, scenario, llm=False)
        plan.note(
            "No recorded LLM responses match this input (fixture mode); planned by the labeled template Director."
        )
        return await self._run(plan)

    async def _run(self, p: _Plan) -> PlanOutcome:
        await self.stage_interpret(p)
        await self.stage_context(p)
        await self.stage_research(p)
        await self.stage_strategy(p)
        await self.stage_script(p)
        await self.stage_fact_check(p)
        await self.stage_scenes(p)
        await self.stage_acting(p)
        self.stage_policies(p)
        return await self.stage_finalize(p)

    # ================================================================== LLM plumbing
    async def _llm(
        self,
        p: _Plan,
        stage: str,
        output_type: type[Out],
        render: dict[str, Any],
        check: Callable[[Out], list[str]],
        controls: list[ControlField],
        fallback: Callable[[], Out],
        summary: dict[str, Any],
    ) -> Out:
        """One LLM stage (or its template when planning without an LLM)."""
        if not p.llm:
            value = fallback()
            p.log.template(stage, summary, value.model_dump(mode="json"))
            return value
        provider = self.deps.provider
        assert provider is not None
        prompt = self.deps.prompts.render(stage, **render)
        inputs = {**summary, "template_version": prompt.template_version}

        def full_check(value: Out) -> list[str]:
            return self.mapper.problems(value.model_dump(mode="json"), controls) + check(value)

        try:
            result = await structured(
                provider,
                stage=stage,
                output_type=output_type,
                messages=prompt.messages(),
                scenario_id=p.scenario,
                check=full_check,
                max_repairs=self.config.max_repairs,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        except StructuredOutputError as exc:
            p.log.failed(stage, prompt.template_version, inputs, exc)
            if exc.value is not None:
                raw_value = exc.value.model_dump(mode="json")
                await self._semantic_hints(raw_value, controls)
                data = self.mapper.coerce(raw_value, controls, p.note, stage=stage)
                coerced: Out | None
                try:
                    coerced = output_type.model_validate(data)
                except ValueError:
                    coerced = None
                if coerced is not None and not check(coerced):
                    return coerced
            p.note(
                f"{stage}: the model's output still failed validation after {self.config.max_repairs} repairs "
                f"({'; '.join(exc.problems[:3])}); this stage used the template."
            )
            p.template_stages.append(stage)
            value = fallback()
            p.log.template(stage, summary, value.model_dump(mode="json"))
            return value
        p.log.llm(stage, prompt.template_version, inputs, result)
        return result.value

    # ================================================================== 1. interpret
    async def stage_interpret(self, p: _Plan) -> None:
        req = p.request
        raw = req.input
        modes = {k: m for k, m in self.bundle.modes.items() if str(m.maturity) != "experimental"}

        def check(b: BriefOut) -> list[str]:
            problems: list[str] = []
            if b.mode not in modes:
                problems.append(f"mode {b.mode!r} is not available; use one of {sorted(modes)}")
            if b.language.split("-")[0].lower() not in (
                self.bundle.languages.languages if self.bundle.languages else {}
            ):
                problems.append(f"language {b.language!r} is not supported")
            unknown = [t for t in b.platform_targets if t not in self.bundle.platforms]
            if unknown:
                problems.append(f"unknown platforms {unknown}; use {sorted(self.bundle.platforms)}")
            if b.setting.camera_profile and b.setting.camera_profile not in self.bundle.camera_profiles:
                problems.append(f"camera profile {b.setting.camera_profile!r} does not exist")
            mode_wanted = req.input_mode if req.input_mode != "auto" else b.input_mode
            if mode_wanted == "exact_script":
                span = b.script_span
                if span is None:
                    problems.append("exact_script needs script_span: the offsets of the script in the input")
                elif span.end > len(raw) or not raw[span.start : span.end].strip():
                    problems.append(f"script_span {span.start}..{span.end} is outside the input or empty")
            previous = 0.0
            for i, t in enumerate(b.time_requests):
                if t.end_s <= t.start_s or t.start_s < previous:
                    problems.append(f"time_requests[{i}] must be ordered and end after it starts")
                previous = t.end_s
            return problems

        render = {
            "raw_input": raw,
            "instruction": req.instruction or "",
            "intent_hints": "\n".join(f"- {h}" for h in req.intent_hints),
            "acting_hints": "\n".join(f"- {h}" for h in req.acting_hints),
            "constraints": self._request_constraints(req),
            "modes": [{"id": k, "description": m.description} for k, m in sorted(modes.items())],
            "platforms": sorted(self.bundle.platforms),
            "world_kinds": sorted(self.vocab.tokens("world_kind")),
            "postures": sorted(self.vocab.tokens("strategy.posture")),
            "camera_profiles": sorted(self.bundle.camera_profiles),
            "emotions": sorted(self.vocab.tokens("emotion")),
        }
        brief = await self._llm(
            p,
            "interpret",
            BriefOut,
            render,
            check,
            INTERPRET_CONTROLS,
            lambda: T.template_brief(req, self.bundle),
            {"input_chars": len(raw), "constraints": render["constraints"]},
        )
        brief = self._apply_request(p, brief)
        p.brief = brief
        p.mode = self.bundle.modes[brief.mode]
        p.assumptions.extend(a for a in brief.assumptions if a not in p.assumptions)

    def _request_constraints(self, req: PlanRequest) -> dict[str, Any]:
        fields = (
            "input_mode",
            "mode",
            "target_duration_s",
            "language",
            "platform_targets",
            "primary_aspect",
            "sources_policy",
            "strategy_pack",
            "camera_profile_id",
            "music_mood",
        )
        out = {}
        for name in fields:
            value = getattr(req, name)
            if value not in (None, [], "auto"):
                out[name] = value
        return out

    def _apply_request(self, p: _Plan, brief: BriefOut) -> BriefOut:
        """The request's explicit fields are constraints: they win over the model's reading."""
        req = p.request
        update: dict[str, Any] = {}
        if req.input_mode != "auto" and brief.input_mode != req.input_mode:
            update["input_mode"] = req.input_mode
        if req.mode and req.mode in self.bundle.modes:
            update["mode"] = req.mode
        if req.target_duration_s:
            update["target_duration_s"] = req.target_duration_s
        if req.language:
            update["language"] = req.language
        if req.platform_targets:
            update["platform_targets"] = req.platform_targets
        if req.sources_policy:
            update["sources_policy"] = req.sources_policy
        if req.camera_profile_id:
            update["setting"] = brief.setting.model_copy(update={"camera_profile": req.camera_profile_id})
        merged = brief.model_copy(update=update)
        if merged.input_mode == "exact_script" and merged.script_span is None:
            stripped = req.input.strip()
            start = req.input.index(stripped)
            merged = merged.model_copy(update={"script_span": {"start": start, "end": start + len(stripped)}})
            merged = BriefOut.model_validate(merged.model_dump(mode="json"))
            p.note("The whole input is treated as the exact script.")
        mode = self.bundle.modes[merged.mode]
        if merged.target_duration_s is None:
            merged = merged.model_copy(update={"target_duration_s": mode.duration_s.default})
            p.note(f"No duration requested; using the {mode.label} default of {mode.duration_s.default:g} s.")
        if not merged.platform_targets:
            merged = merged.model_copy(update={"platform_targets": ["tiktok"]})
        return merged

    # ================================================================== 2. context
    async def stage_context(self, p: _Plan) -> None:
        assert p.brief is not None
        req, brief, ctx = p.request, p.brief, p.ctx
        requested = req.cast[0].creator_id if req.cast else None
        creator, notes = choose_creator(ctx.creators, requested=requested, age=brief.cast.age, gender=brief.cast.gender)
        for n in notes:
            p.note(n)
        pinned = ctx.pinned_snapshots.get(creator.creator_version_id)
        draft: SnapshotDraft | None = None
        if pinned is None:
            brief_text = " ".join([req.input, brief.angle, brief.audience])
            vector, model = None, None
            if any(r.embedding is not None for r in creator.memory):
                embedded = await self._embed([brief_text], brief.language)
                if embedded is not None:
                    vector, model = tuple(embedded[0][0]), embedded[1]
            memory_config = self.bundle.memory
            assert memory_config is not None
            draft = self.retriever.retrieve(
                creator.memory,
                RetrievalContext(
                    creator_version_id=creator.creator_version_id,
                    now=ctx.now,
                    brief=brief_text,
                    version_numbers=creator.version_numbers,
                    brief_vector=vector,
                    embedding_model=model,
                    embedding_weight=memory_config.embeddings.retrieval_weight,
                ),
            )
            snapshot_id = self.deps.ids()
            items = [i.model_dump(mode="json") for i in draft.items]
        else:
            snapshot_id, items = pinned  # a reused snapshot: exactly the items the version pinned (I7)
            p.note("Reused the version's pinned memory snapshot (replan without refresh_memory).")
        p.pack = ContextPack(
            creator=creator,
            character_key="char_" + _slug(creator.name),
            worlds=list(ctx.worlds),
            snapshot_id=snapshot_id,
            snapshot=draft,
            snapshot_items=items,
            recent_usage=list(creator.recent_usage),
        )
        p.log.code(
            "context",
            {"requested_creator": str(requested) if requested else None, "cast_hints": brief.cast.model_dump()},
            {
                "creator_version_id": str(creator.creator_version_id),
                "snapshot_id": str(snapshot_id),
                "memory_items": [i["item_id"] for i in items],
                "recent_videos": [str(u.video_id) for u in creator.recent_usage],
                "worlds": [str(w.world_version_id) for w in ctx.worlds],
            },
        )

    # ================================================================== 3. research
    async def stage_research(self, p: _Plan) -> None:
        assert p.brief is not None
        closed = p.brief.sources_policy == "closed_book"
        p.research = await gather(
            p.request.input, config=self.bundle.app.research, fetcher=self.deps.fetcher, closed_book=closed
        )
        facts: list[StoredFact] = []
        for source in p.research.sources:
            trust = "web" if source.kind == "url" else "user_provided"
            for chunk in p.research.chunks:
                if chunk.source_id == source.id:
                    facts.append(
                        StoredFact(
                            id=fact_id(chunk.source_id, chunk.index),
                            source_id=chunk.source_id,
                            index=chunk.index,
                            text=chunk.text,
                            start=chunk.start,
                            end=chunk.end,
                            trust=trust,
                            source_title=source.title,
                            source_uri=source.url,
                            ref=chunk.id,
                        )
                    )
        attached = list(p.ctx.facts)
        if closed:  # closed book (§13): only what the user provided counts as evidence
            attached = [f for f in attached if f.trust == "user_provided"]
            facts = [f for f in facts if f.trust == "user_provided"]
        facts += attached
        ingest = self.bundle.app.research.ingest
        p.index = HybridIndex(facts, vector_weight=ingest.vector_weight)
        p.evidence_by_id = {f.evidence_id: f for f in facts}
        for failure in p.research.failures:
            p.findings.append(
                Finding(
                    kind="fact_check",
                    severity="warning",
                    message=f"Source {failure.url} was not used: {failure.reason}",
                    detail={"check": "research", "blocked": failure.blocked},
                )
            )
        p.log.code(
            "research",
            {"closed_book": closed},
            {
                "sources": [
                    {"id": str(s.id), "kind": s.kind, "url": s.url, "title": s.title} for s in p.research.sources
                ],
                "chunks": len(p.research.chunks),
                "persistent_sources": [str(i) for i in p.ctx.source_ids],
                "persistent_facts": len(p.ctx.facts),
                "evidence": len(p.evidence_by_id),
                "failures": [{"url": f.url, "reason": f.reason, "blocked": f.blocked} for f in p.research.failures],
            },
        )

    # ================================================================== 4. strategy
    async def stage_strategy(self, p: _Plan) -> None:
        assert p.brief is not None and p.mode is not None and p.pack is not None
        brief, mode, pack = p.brief, p.mode, p.pack
        packs = {k: v for k, v in self.bundle.strategy_packs.items() if k in mode.strategy_packs}
        if p.request.strategy_pack and p.request.strategy_pack in self.bundle.strategy_packs:
            packs = {p.request.strategy_pack: self.bundle.strategy_packs[p.request.strategy_pack]}
        exact = brief.input_mode == "exact_script"

        def check(s: StrategyOut) -> list[str]:
            problems: list[str] = []
            if s.strategy_pack not in packs:
                problems.append(f"strategy_pack {s.strategy_pack!r} is not allowed; use one of {sorted(packs)}")
            if not exact and not (self.config.hooks.min <= len(s.hooks) <= self.config.hooks.max):
                problems.append(f"give {self.config.hooks.min}-{self.config.hooks.max} hooks, not {len(s.hooks)}")
            if s.selected_hook >= len(s.hooks):
                problems.append("selected_hook must index a hook")
            if not exact:
                for finding in self.guard.hooks(s.hooks, pack.recent_usage):
                    problems.append(
                        f"hook {finding.subject!r} is {finding.detail} (similarity {finding.score:.2f}); replace it"
                    )
            return problems

        render = {
            "brief": self._brief_view(brief),
            "raw_input": p.request.input,
            "mode": {"id": mode.id, "description": mode.description, "fragment": mode.prompt_fragment or ""},
            "packs": [
                {"id": k, "structure": v.structure, "guidance": v.guidance, "hooks": v.hook_count}
                for k, v in sorted(packs.items())
            ],
            "hooks_range": [self.config.hooks.min, self.config.hooks.max],
            "persona": self._persona(pack),
            "memory": pack.snapshot_items,
            "recent_hooks": [h for u in pack.recent_usage for h in u.hooks],
            "evidence": await self._evidence(p, " ".join([brief.title, brief.angle, p.request.input])),
            "purposes": sorted(self.vocab.tokens("intent.scene_purpose")),
            "intent_vocab": self._intent_vocab(video=True),
            "emotions": sorted(self.vocab.tokens("emotion")),
            "exact": exact,
        }
        strategy = await self._llm(
            p,
            "strategy",
            StrategyOut,
            render,
            check,
            STRATEGY_CONTROLS,
            lambda: T.template_strategy(brief, mode, packs, self.bundle),
            {"mode": mode.id, "packs": sorted(packs)},
        )
        if not exact and strategy.hooks:
            strategy = await self._semantic_hooks(p, strategy)
        p.strategy = strategy

    # ================================================================== 5. script
    async def stage_script(self, p: _Plan) -> None:
        assert p.brief is not None and p.mode is not None and p.pack is not None and p.strategy is not None
        brief, strategy, pack, raw = p.brief, p.strategy, p.pack, p.request.input
        exact = brief.input_mode == "exact_script"
        wpm = pack.creator.wpm(brief.language, self.bundle.app.spec.default_wpm)
        assert brief.target_duration_s is not None
        n_beats = len(strategy.beats)
        target = target_word_count(brief.target_duration_s, wpm, self.bundle.app.render.timeline, n_beats)
        tolerance = self.config.script_words_tolerance
        avoid = [*pack.creator.dna.avoidances.phrases, *self._memory_values(pack, "avoidance.phrase", "text")]
        signature = {s.text: s.max_per_video for s in pack.creator.dna.speech.signature_phrases}

        def check(s: ScriptOut) -> list[str]:
            problems: list[str] = []
            if exact:
                try:
                    self._exact_segments(brief, raw, s.boundaries)
                except ExactScriptError as exc:
                    problems += exc.problems
                count = len(s.boundaries) + 1
                if s.segment_beats and len(s.segment_beats) != count:
                    problems.append(f"segment_beats has {len(s.segment_beats)} entries for {count} segments")
                if any(b >= n_beats for b in s.segment_beats):
                    problems.append(f"segment_beats must index the {n_beats} beats")
                for j, ann in enumerate(s.annotations):
                    problems += self._annotation_problems(f"annotations[{j}]", ann.type, ann.tag)
                return problems
            for j, ann in enumerate(s.annotations):
                problems += self._annotation_problems(f"annotations[{j}]", ann.type, ann.tag)
            if not s.lines:
                return [*problems, "write the script as `lines`"]
            if any(line.beat >= n_beats for line in s.lines):
                problems.append(f"every line's beat must index the {n_beats} beats")
            beats = [line.beat for line in s.lines]
            if beats != sorted(beats):
                problems.append("lines must follow the beats in order")
            text = " ".join(parse_tags(line.text).text for line in s.lines)
            count = len(tokenize(text))
            if abs(count - target) > tolerance * target:
                problems.append(f"the script has {count} words; write about {target} (±{int(tolerance * 100)}%)")
            lowered = text.lower()
            for phrase in avoid:
                if phrase.lower() in lowered:
                    problems.append(f"never use the phrase {phrase!r} (the creator avoids it)")
            for phrase, limit in signature.items():
                if lowered.count(phrase.lower()) > limit:
                    problems.append(f"use the signature phrase {phrase!r} at most {limit} time(s)")
            for line in s.lines:
                issues = parse_tags(line.text).issues
                problems += [f"line {line.text[:30]!r}: {i}" for i in issues]
            return problems

        sentences = []
        if exact and brief.script_span is not None:
            sentences = [
                {"start": a, "end": b, "text": raw[a:b]}
                for a, b in sentence_spans(raw, brief.script_span.start, brief.script_span.end)
            ]
        render = {
            "brief": self._brief_view(brief),
            "beats": [b.model_dump() for b in strategy.beats],
            "hook": strategy.hooks[strategy.selected_hook] if strategy.hooks else "",
            "persona": self._persona(pack),
            "speech": pack.creator.dna.speech.model_dump(mode="json"),
            "avoid_phrases": avoid,
            "memory": pack.snapshot_items,
            "target_words": target,
            "tolerance": int(tolerance * 100),
            "tags": sorted(_canonical_tags()),
            "evidence": await self._evidence(p, " ".join(b.summary for b in strategy.beats) or raw),
            "exact": exact,
            "raw_input": raw,
            "script_span": brief.script_span.model_dump() if brief.script_span else None,
            "sentences": sentences,
            "testimonial_rule": p.mode.id
            in (self.bundle.testimonials.applies_to_modes if self.bundle.testimonials else []),
            "dramatization": p.mode.id
            in (self.bundle.testimonials.dramatization_modes if self.bundle.testimonials else []),
        }

        def fallback() -> ScriptOut:
            return T.template_script(brief, raw, n_beats)

        script = await self._llm(
            p,
            "script",
            ScriptOut,
            render,
            check,
            SCRIPT_CONTROLS,
            fallback,
            {"exact": exact, "target_words": target, "wpm": wpm},
        )
        self._start_draft(p)
        assert p.draft is not None
        speaker = pack.character_key
        if exact:
            segments = self._exact_segments(brief, raw, script.boundaries)
            keys, hints = B.add_segments(p.draft, [s.tagged for s in segments], speaker=speaker, source="user_tag")
            beats = script.segment_beats or T.distribute(len(keys), n_beats)
            p.draft.data["brief"]["hook_candidates"] = [{"key": "hk_1", "text": _first_sentence(segments[0].text)}]
            p.draft.data["brief"]["selected_hook_key"] = "hk_1"
            p.draft.data["locks"].append({"group": "script", "scope": {}, "set_by": "user"})
        else:
            keys, hints = B.add_segments(
                p.draft, [line.text for line in script.lines], speaker=speaker, source="director"
            )
            beats = [line.beat for line in script.lines]
        p.hints = hints
        p.segment_beats = list(zip(keys, beats, strict=True))
        for ann in script.annotations:
            if ann.segment_key in keys and not self._annotation_problems("", ann.type, ann.tag):
                p.draft.add_annotation(ann.segment_key, ann.type, ann.tag, ann.word, ann.end_word, source="director")
        if exact:
            self._fit_duration(p, wpm, "the exact script cannot change words")
        elif not p.llm or "script" in p.template_stages:
            self._fit_duration(p, wpm, "the template script is the input's own sentences")

    def _exact_segments(self, brief: BriefOut, raw: str, starts: Sequence[int]) -> list[Any]:
        assert brief.script_span is not None
        span = (brief.script_span.start, brief.script_span.end)
        bounds = sorted(set(starts))
        points = [span[0], *[b for b in bounds if span[0] < b < span[1]], span[1]]
        pairs: list[tuple[int, int]] = []
        for a, b in itertools.pairwise(points):
            while a < b and raw[a].isspace():
                a += 1
            end = b
            while end > a and raw[end - 1].isspace():
                end -= 1
            if end > a:
                pairs.append((a, end))
        outside = [b for b in bounds if not span[0] < b < span[1]]
        if outside:
            raise ExactScriptError([f"boundaries {outside} are outside the script span {span}"])
        return extract_segments(raw, span, pairs)

    def _fit_duration(self, p: _Plan, wpm: float, why: str) -> None:
        """Wording that cannot be rewritten to length (exact scripts, template scripts): pacing
        approaches the target within `max_pacing_delta`; otherwise the target follows the script
        and the gap is reported."""
        assert p.draft is not None and p.brief is not None
        target = float(p.brief.target_duration_s or 0)
        words = sum(len(p.draft.tokens(s["key"])) for s in p.draft.segments())
        speech = words * 60.0 / wpm
        if not target or abs(speech - target) / target <= self.bundle.app.spec.duration_tolerance:
            return
        needed = speech / target - 1.0
        delta = max(-self.config.max_pacing_delta, min(self.config.max_pacing_delta, needed))
        p.draft.data["_pacing_delta"] = round(delta, 3)
        fitted = speech / (1.0 + delta)
        if abs(fitted - target) / target > self.bundle.app.spec.duration_tolerance:
            p.draft.data["meta"]["target_duration_s"] = round(fitted, 1)
            p.note(
                f"The script runs about {speech:.0f} s at the creator's pace and {why}: the requested {target:g} s "
                f"becomes {fitted:.0f} s (pacing {delta:+.0%})."
            )
            p.findings.append(
                Finding(
                    kind="timing",
                    severity="warning",
                    message=f"Requested {target:g} s; the script needs about {fitted:.0f} s ({why}).",
                    refs=["/meta/target_duration_s"],
                    detail={"requested_s": target, "estimated_s": round(fitted, 1), "pacing_delta": delta},
                )
            )

    # ================================================================== 6. fact and persona check
    async def stage_fact_check(self, p: _Plan) -> None:
        assert p.draft is not None and p.pack is not None and p.research is not None
        draft, pack, research = p.draft, p.pack, p.research
        segment_keys = [s["key"] for s in draft.segments()]
        evidence = await self._evidence(p, " ".join(s["text"] for s in draft.segments()))
        known = {e["id"] for e in evidence} | set(p.evidence_by_id)

        def check(f: FactCheckOut) -> list[str]:
            problems = [
                f"claim on unknown segment {c.segment_key}" for c in f.claims if c.segment_key not in segment_keys
            ]
            problems += [
                f"assertion on unknown segment {a.segment_key}"
                for a in f.assertions
                if a.segment_key not in segment_keys
            ]
            for c in f.claims:
                bad = [e for e in c.evidence_ids if e not in known]
                if bad:
                    problems.append(f"claim {c.text[:40]!r} cites unknown evidence {bad}; cite only listed ids")
                if c.verdict == "supported" and not c.evidence_ids:
                    problems.append(f"claim {c.text[:40]!r} is supported only with evidence ids")
            return problems

        render = {
            "segments": [{"key": s["key"], "text": s["text"]} for s in draft.segments()],
            "evidence": evidence,
            "closed_book": research.closed_book,
            "canon": [c.model_dump(mode="json") for c in pack.creator.dna.identity.canon],
            "persona_memory": [i for i in pack.snapshot_items if str(i.get("kind")) in ("persona_fact", "stance")],
            "name": pack.creator.name,
        }
        result = await self._llm(
            p,
            "fact_check",
            FactCheckOut,
            render,
            check,
            [],
            T.template_fact_check,
            {"segments": len(segment_keys), "evidence": len(evidence)},
        )
        claims: list[Claim] = []
        checked = await self._check_claims(p, result, [(s["key"], s["text"]) for s in draft.segments()])
        p.claim_checks = checked
        for c in checked:
            key = f"clm_{len(claims) + 1}"
            claim = Claim(key=key, text=c.text, verdict=c.verdict, evidence_ids=list(c.evidence_ids))
            claims.append(claim)
            draft.segment(c.segment_key)["claim_keys"].append(key)
            why = f" ({'; '.join(c.reasons)})" if c.reasons else ""
            if c.blocking:
                closed_note = " — closed book: edit the script or add a source" if not c.overridable else ""
                p.findings.append(
                    Finding(
                        kind="fact_check",
                        severity="blocking",
                        message=f"{c.verdict.capitalize()} claim: {c.text!r}{why}{closed_note}",
                        refs=[f"/script/segments[{c.segment_key}]/text"],
                        detail={
                            "id": f"claim:{key}",
                            "check": "fact_check",
                            "claim_key": key,
                            "overridable": c.overridable,
                            "closed_book": research.closed_book,
                            "detected": c.detected,
                        },
                    )
                )
            elif c.verdict == "uncertain":
                p.findings.append(
                    Finding(
                        kind="fact_check",
                        severity="warning",
                        message=f"Uncertain claim: {c.text!r}{why}",
                        refs=[f"/script/segments[{c.segment_key}]/text"],
                        detail={"check": "fact_check", "claim_key": key, "detected": c.detected},
                    )
                )
        dossier = research.dossier(claims, extra_evidence=set(p.evidence_by_id), extra_sources=list(p.ctx.source_ids))
        draft.data["research"] = dossier.model_dump(mode="json")
        assertions = [
            PersonaAssertion(a.subject, a.predicate, a.object, a.segment_key, a.kind) for a in result.assertions
        ]
        conflicts = script_contradictions(
            assertions,
            canon=pack.creator.dna.identity.canon,
            memory=self._snapshot_records(p),  # memory reaches planning only through the snapshot (I7)
            self_names=[pack.creator.name, "I", "me", "my"],
        )
        for conflict in conflicts:
            a = conflict.assertion
            p.findings.append(
                Finding(
                    kind="contradiction",
                    severity=conflict.severity,
                    message=f"The script says {a.subject} {a.predicate} {a.object}; the {conflict.against} says "
                    f"{conflict.existing}.",
                    refs=[f"/script/segments[{a.segment_key}]/text"],
                    detail={
                        **conflict.as_dict(),
                        "id": f"contradiction:{conflict.ref}:{a.segment_key}",
                        "overridable": True,
                    },
                )
            )

    # ================================================================== 7. scenes, worlds, shots
    async def stage_scenes(self, p: _Plan) -> None:
        assert p.draft is not None and p.mode is not None and p.pack is not None and p.strategy is not None
        assert p.brief is not None
        draft, mode, pack, brief = p.draft, p.mode, p.pack, p.brief
        segment_keys = [s["key"] for s in draft.segments()]
        allowed_overlays = {"broll", "title_card"} & {str(t) for t in mode.allowed_shot_types}

        def check(s: ScenesOut) -> list[str]:
            problems: list[str] = []
            listed = [k for scene in s.scenes for k in scene.segment_keys]
            if listed != segment_keys:
                problems.append(f"scenes must list every segment once, in order: {segment_keys}")
                return problems
            for i, scene in enumerate(s.scenes):
                words = [(k, t.index) for k in scene.segment_keys for t in draft.tokens(k)]
                if scene.reveal_at and (scene.reveal_at.segment_key, scene.reveal_at.word) not in words:
                    problems.append(f"scenes[{i}].reveal_at is not a word of the scene")
                for j, o in enumerate(scene.overlays):
                    if o.kind not in allowed_overlays:
                        problems.append(f"scenes[{i}].overlays[{j}]: mode {mode.id} allows {sorted(allowed_overlays)}")
                    a, b = (o.start.segment_key, o.start.word), (o.end.segment_key, o.end.word)
                    if a not in words or b not in words or words.index(a) > words.index(b):
                        problems.append(f"scenes[{i}].overlays[{j}] must span words of the scene in order")
                    if o.kind == "broll" and not o.prompt:
                        problems.append(f"scenes[{i}].overlays[{j}] needs a prompt")
            if not problems:
                problems += self._scene_validation(p, s)
            return problems

        worlds = [
            {
                "id": str(w.world_version_id),
                "name": w.dna.name,
                "kind": str(w.dna.kind),
                "positions": [c.key for c in w.dna.camera_positions],
            }
            for w in pack.worlds
        ]
        render = {
            "brief": self._brief_view(brief),
            "segments": [
                {"key": s["key"], "text": s["text"], "words": _numbered(draft, s["key"])} for s in draft.segments()
            ],
            "beats": [{"index": i, **b.model_dump()} for i, b in enumerate(p.strategy.beats)],
            "segment_beats": [{"segment_key": k, "beat": b} for k, b in p.segment_beats],
            "mode": {
                "id": mode.id,
                "allowed_shot_types": [str(t) for t in mode.allowed_shot_types],
                "edit_grammar": mode.edit_grammar.model_dump(),
                "acting_density": mode.acting_density,
            },
            "worlds": worlds,
            "purposes": sorted(self.vocab.tokens("intent.scene_purpose")),
            "intent_vocab": self._intent_vocab(video=False),
            "framings": sorted(self.vocab.tokens("camera.framing")),
            "video_intent": p.strategy.video_intent.model_dump(),
        }
        scenes = await self._llm(
            p,
            "scenes",
            ScenesOut,
            render,
            check,
            SCENE_CONTROLS,
            lambda: T.template_scenes(p.segment_beats, p.strategy.beats, self.bundle),  # type: ignore[union-attr]
            {"segments": len(segment_keys)},
        )
        self._apply_scenes(p, draft, scenes, notes=True)

    def _apply_scenes(self, p: _Plan, draft: SpecDraft, scenes: ScenesOut, *, notes: bool) -> None:
        assert p.mode is not None and p.pack is not None and p.brief is not None
        mode, pack, brief = p.mode, p.pack, p.brief
        avoid = None
        if pack.recent_usage:
            last = pack.recent_usage[0].visual
            if last.get("camera_position") and last.get("time_of_day"):
                avoid = (str(last["camera_position"]), str(last["time_of_day"]))
        choices = []
        for scene_out in scenes.scenes:
            choice = B.choose_world(
                pack,
                self.bundle,
                mode,
                world_kind=brief.setting.world_kind,
                requested_world=p.request.world_id,
                camera_position=scene_out.camera_position,
                camera_profile=brief.setting.camera_profile,
                posture=brief.setting.posture,
                framing=scene_out.framing,
                avoid=avoid,
            )
            choices.append(choice)
            if notes:
                for n in choice.notes:
                    p.note(n)
        wardrobe = p.request.cast[0].wardrobe_version_id if p.request.cast else None
        wardrobe = wardrobe or (pack.creator.wardrobe_version_ids[0] if pack.creator.wardrobe_version_ids else None)
        reveal = B.add_scenes(draft, scenes, pack=pack, choices=choices, wardrobe_version_id=wardrobe)
        delta = draft.data.pop("_pacing_delta", None)
        if delta:
            for scene in draft.data["scenes"]:
                scene["pacing"] = {"target_wpm_delta": delta, "cut_cadence": None}
        policies = self.engine.evaluate(draft, mode.id)
        wpm = pack.creator.wpm(brief.language, self.bundle.app.spec.default_wpm)
        for scene_out, choice, scene in zip(scenes.scenes, choices, draft.scenes(), strict=True):
            ranges = B.plan_shots(
                draft,
                scene["key"],
                mode=mode,
                wpm=wpm,
                reveal=reveal[scene["key"]],
                policies=policies,
                hold_words=self.config.reveal_hold_words,
            )
            B.add_shots(
                draft,
                scene["key"],
                ranges,
                scene_out.overlays,
                choice=choice,
                character=pack.character_key,
                mode=mode,
                takes=p.request.takes,
            )
        if notes:
            p.reveal = reveal
            p.choices = {s["key"]: c for s, c in zip(draft.scenes(), choices, strict=True)}
            p.policies = policies

    def _scene_validation(self, p: _Plan, scenes: ScenesOut) -> list[str]:
        assert p.draft is not None
        trial = SpecDraft(copy.deepcopy(p.draft.data))
        try:
            self._apply_scenes(p, trial, scenes, notes=False)
            spec = trial.spec()
        except (LookupError, ValueError) as exc:
            return [f"the scenes do not build: {exc}"]
        return [f"{i.path}: {i.message}" for i in self._validate(p, spec) if i.code not in IGNORED_EARLY]

    # ================================================================== 8. acting
    async def stage_acting(self, p: _Plan) -> None:
        assert p.draft is not None and p.mode is not None and p.pack is not None and p.brief is not None
        draft, pack = p.draft, p.pack
        scene_keys = [s["key"] for s in draft.scenes()]
        p.route_preview = self._route_preview(p)
        timeline = self._timeline(p, draft)
        tag_constraints = self._tag_constraints(p)

        def check(a: ActingOut) -> list[str]:
            problems: list[str] = []
            if [s.scene_key for s in a.scenes] != scene_keys:
                return [f"give one entry per scene, in order: {scene_keys}"]
            resolved = self._resolve_seconds(p, draft, a, timeline)
            for i, scene_out in enumerate(a.scenes):
                words = draft.scene_words(scene_out.scene_key)
                starts = B.state_starts(
                    draft, scene_out.scene_key, scene_out.states, resolved.get(scene_out.scene_key, {})
                )
                if any(s is None for s in starts):
                    problems.append(f"scenes[{i}]: every state must start at a word of its scene")
                    continue
                if 0 not in starts:
                    problems.append(f"scenes[{i}]: the first state must start at the scene's first word {words[0]}")
                if len(set(starts)) != len(starts):
                    problems.append(f"scenes[{i}]: two states start at the same word")
                for j, ev in enumerate(scene_out.events):
                    if (ev.at.segment_key, ev.at.word) not in words:
                        problems.append(f"scenes[{i}].events[{j}] is not on a word of the scene")
                    if ev.reacts_to_state is not None and ev.reacts_to_state >= len(scene_out.states):
                        problems.append(f"scenes[{i}].events[{j}].reacts_to_state must index a state")
                for j, st in enumerate(scene_out.states):
                    if st.masking and st.felt == st.displayed:
                        problems.append(f"scenes[{i}].states[{j}]: masking needs felt ≠ displayed")
                for j, ann in enumerate(scene_out.annotations):
                    if (ann.segment_key, ann.word) not in words:
                        problems.append(f"scenes[{i}].annotations[{j}] is not on a word of the scene")
                    problems += self._annotation_problems(f"scenes[{i}].annotations[{j}]", ann.type, ann.tag)
            for c in tag_constraints:
                scene_out = next(s for s in a.scenes if s.scene_key == c["scene_key"])
                res = resolved.get(c["scene_key"], {})
                starts = B.state_starts(draft, c["scene_key"], scene_out.states, res)
                position = draft.position(c["scene_key"], (c["segment_key"], c["word"]))
                if not any(
                    s == position and st.displayed.label == c["label"]
                    for s, st in zip(starts, scene_out.states, strict=True)
                ):
                    problems.append(
                        f"the user tagged [{c['label']}] at {c['segment_key']} word {c['word']}: a state must start "
                        f"there displaying {c['label']}"
                    )
            if not problems:
                problems += self._acting_validation(p, a, timeline)
            return problems

        render = self._acting_render(p, draft, timeline, tag_constraints)
        posture = {k: p.choices[k].posture for k in scene_keys}
        firsts = [
            (s["key"], s["purpose"], WordAt(segment_key=draft.scene_words(s["key"])[0][0], word=0))
            for s in draft.scenes()
        ]

        def fallback() -> ActingOut:
            return T.template_acting(firsts, bundle=self.bundle, dna=pack.creator.dna, posture=posture)

        acting = await self._llm(
            p,
            "acting",
            ActingOut,
            render,
            check,
            ACTING_CONTROLS,
            fallback,
            {
                "scenes": scene_keys,
                "route_preview": p.route_preview,
                "policy_proposals": [
                    self._proposal_view(s, pr) for s, pr in p.policies.proposals() if pr.channel in ("acting", "voice")
                ],
            },
        )
        p.requested_seconds.clear()
        p.resolved_seconds = self._resolve_seconds(p, draft, acting, timeline, record=p.requested_seconds)
        B.add_acting(draft, acting, character=pack.character_key, hints=p.hints, resolved=p.resolved_seconds)
        for scene_out in acting.scenes:
            for deviation in scene_out.deviations:
                p.note(f"acting ({scene_out.scene_key}) departed from an intent-policy proposal: {deviation}")

    def _acting_validation(self, p: _Plan, acting: ActingOut, timeline: Any) -> list[str]:
        assert p.draft is not None
        assert p.pack is not None
        trial = SpecDraft(copy.deepcopy(p.draft.data))
        try:
            resolved = self._resolve_seconds(p, trial, acting, timeline)
            B.add_acting(trial, acting, character=p.pack.character_key, hints=p.hints, resolved=resolved)
            self._sync_arc(trial, notes=None)
            spec = trial.spec()
        except (LookupError, ValueError) as exc:
            return [f"the acting plan does not build: {exc}"]
        issues = self._validate(p, spec)
        relevant = [i for i in issues if i.severity == "error" and i.code not in {"duration", "memory_pin"}]
        return [f"{self._acting_path(trial, i.path)}: {i.message}" for i in relevant]

    @staticmethod
    def _acting_path(draft: SpecDraft, path: str | None) -> str:
        """Maps spec paths of generated keys back to the stage output's indices for the model."""
        if not path:
            return "spec"
        for i, scene in enumerate(draft.scenes()):
            acting = scene.get("acting") or {}
            for j, state in enumerate(acting.get("states", [])):
                path = path.replace(
                    f"/scenes[{scene['key']}]/acting/states[{state['key']}]", f"scenes[{i}].states[{j}]"
                )
            for j, event in enumerate(acting.get("events", [])):
                path = path.replace(
                    f"/scenes[{scene['key']}]/acting/events[{event['key']}]", f"scenes[{i}].events[{j}]"
                )
        return path

    def _resolve_seconds(
        self,
        p: _Plan,
        draft: SpecDraft,
        acting: ActingOut,
        timeline: Any,
        *,
        record: dict[tuple[str, int], float] | None = None,
    ) -> dict[str, dict[int, tuple[str, int]]]:
        """§15.4: a state asked for at a second starts at the word whose estimated start is closest."""
        out: dict[str, dict[int, tuple[str, int]]] = {}
        previous: tuple[str, int] | None = None
        for scene_out in acting.scenes:
            scene_words = set(draft.scene_words(scene_out.scene_key))
            for i, state in enumerate(scene_out.states):
                if state.start_s is None:
                    if state.start is not None:
                        previous = (state.start.segment_key, state.start.word)
                    continue
                word = nearest_word(timeline, state.start_s, after=previous)
                candidates = [w for w in timeline.words if (w.segment_key, w.word) in scene_words]
                if word is None or (word.segment_key, word.word) not in scene_words:
                    word = min(candidates, key=lambda w: abs(w.start_s - state.start_s)) if candidates else None  # type: ignore[operator]
                if word is not None:
                    ref = (word.segment_key, word.word)
                    out.setdefault(scene_out.scene_key, {})[i] = ref
                    if record is not None:
                        record[(scene_out.scene_key, i)] = float(state.start_s)
                    previous = ref
        return out

    def _tag_constraints(self, p: _Plan) -> list[dict[str, Any]]:
        assert p.draft is not None
        out = []
        for hint in p.hints:
            if hint.source != "user_tag":
                continue
            scene = p.draft.scene_of_segment(hint.segment_key)
            if scene is None:
                continue
            for label, first, _last in hint.emotions:
                out.append({"scene_key": scene["key"], "segment_key": hint.segment_key, "word": first, "label": label})
        return out

    # ================================================================== 9–10. intent policies
    def stage_policies(self, p: _Plan) -> None:
        assert p.draft is not None and p.mode is not None and p.pack is not None and p.brief is not None
        draft = p.draft
        music_mood = p.request.music_mood or (
            ", ".join(p.pack.creator.dna.editing.music_taste) + ", no vocals"
            if p.pack.creator.dna.editing.music_taste
            else self.config.music.default_mood
        )
        B.add_music(draft, mood=music_mood, duck_db=self.config.music.duck_db)
        self._add_disclosure(p, draft)
        reveal = {k: v for k, v in p.reveal.items() if v is not None}
        wpm = p.pack.creator.wpm(p.brief.language, self.bundle.app.spec.default_wpm)
        p.decisions = self.engine.apply(
            draft, p.policies, reveal=reveal, mode=p.mode, config=self.config, pause_ms=self.vocab.pause_ms, wpm=wpm
        )
        self._sync_arc(draft, notes=p)
        for d in p.decisions:
            if not d.applied and d.value not in ("optional", False):
                p.note(f"intent policy {d.rule_id} {d.channel}.{d.key} not applied in {d.scene_key}: {d.effect}")
        p.log.code("intent_policy", {"mode": p.mode.id}, {"decisions": [d.as_dict() for d in p.decisions]})

    def _sync_arc(self, draft: SpecDraft, *, notes: _Plan | None) -> None:
        """`intent.video.emotional_arc` must summarize the displayed trajectory (§15.4)."""
        displayed: list[str] = []
        for scene in draft.scenes():
            for state in (scene.get("acting") or {}).get("states", []):
                label = state["emotion"]["displayed"]["label"]
                if not displayed or displayed[-1] != label:
                    displayed.append(label)
        arc = list(draft.data["intent"]["video"].get("emotional_arc") or [])
        it = iter(displayed)
        if displayed and not all(label in it for label in arc):
            draft.data["intent"]["video"]["emotional_arc"] = displayed
            if notes is not None:
                notes.note(f"emotional_arc {arc} did not summarize the acting trajectory; set to {displayed}.")

    # ================================================================== 11. validate, resolve, report
    async def stage_finalize(self, p: _Plan) -> PlanOutcome:
        assert p.draft is not None and p.mode is not None and p.pack is not None and p.brief is not None
        draft, pack = p.draft, p.pack
        draft.data["brief"]["assumptions"] = list(p.assumptions)  # stage 1 records assumptions in the brief
        spec = draft.spec()
        issues = self._validate(p, spec)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise PlanningError("the plan does not validate", [f"{i.path}: {i.message} ({i.code})" for i in errors])
        for warning in (i for i in issues if i.severity == "warning"):
            p.findings.append(
                Finding(
                    kind="timing" if warning.code == "duration" else "other",
                    severity="warning",
                    message=warning.message,
                    refs=[warning.path] if warning.path else [],
                    detail={"code": warning.code},
                )
            )
        p.findings += self._blocklists(spec)
        testimonial = await self._testimonial(p, spec)
        snapshot_refs = self._snapshot_refs(p)
        refs = await self.deps.refs_for(spec, snapshot_refs)
        spec, _routes, cbs, compiled, graph_routes = self._plan_time(p, spec, refs)
        report = self._report(p, spec, cbs, compiled)
        p.log.code(
            "finalize",
            {"version_id": str(p.ctx.version_id)},
            {
                "findings": len(report.findings),
                "blocking": sum(1 for f in report.findings if f.severity == "blocking"),
                "predicted_coverage": report.predicted_coverage.summary_counts()
                if report.predicted_coverage is not None
                else None,
                "approximations": [d for _, d in self._approximations(spec)],
            },
        )
        snapshots = [(pack.snapshot_id, pack.snapshot)] if pack.snapshot is not None else []
        return PlanOutcome(
            spec=spec,
            report=report,
            planner="llm" if p.llm else "template",
            runs=p.log.runs,
            snapshots=snapshots,  # type: ignore[arg-type]
            planned_routes=graph_routes,
            decisions=p.decisions,
            research=p.research,  # type: ignore[arg-type]
            cbs=cbs,
            compiled=compiled,
            testimonial=testimonial,
            route_preview=p.route_preview,
            claims=p.claim_checks,
            evidence=dict(p.evidence_by_id),
        )

    def _blocklists(self, spec: VideoSpec) -> list[Finding]:
        checker = BlocklistChecker.from_config(self.bundle.blocklists)
        texts = [("/meta/title", spec.meta.title)]
        texts += [(f"/script/segments[{s.key}]/text", s.text) for s in spec.script.segments]
        for scene, shot in spec.shots():
            if shot.broll is not None:
                texts.append((f"/scenes[{scene.key}]/shots[{shot.key}]/broll/prompt", shot.broll.prompt))
            if shot.title is not None:
                texts.append((f"/scenes[{scene.key}]/shots[{shot.key}]/title/text", shot.title.text))
        return checker.findings(texts)

    async def _testimonial(self, p: _Plan, spec: VideoSpec) -> GuardResult | None:
        policy = self.bundle.testimonials
        if policy is None:
            return None
        guard = TestimonialGuard(policy, provider=self.deps.provider if p.llm else None, prompts=self.deps.prompts)
        result = await guard.check(spec, scenario_id=p.scenario)
        p.findings += result.findings
        if result.run is not None:
            p.log.llm(
                "testimonial_guard",
                "testimonial_guard/" + self.deps.prompts.latest("testimonial_guard"),
                {"segments": len(spec.script.segments)},
                result.run,
            )
        elif result.applies:
            p.log.code(
                "testimonial_guard",
                {"classifier": result.classifier_status},
                {"flagged": [a.segment_key for a in result.assessments if a.flagged]},
            )
        return result

    @staticmethod
    def _snapshot_records(p: _Plan) -> list[MemoryRecord]:
        """The pinned snapshot's items as retrieval records (they are active by construction)."""
        assert p.pack is not None
        out = []
        for item in p.pack.snapshot_items:
            kind = str(item.get("kind", ""))
            value = item.get("value")
            out.append(
                MemoryRecord(
                    id=UUID(str(item["item_id"])),
                    category=kind.split(".")[0],
                    kind=kind,
                    key="",
                    value=dict(value) if isinstance(value, dict) else {},
                    text=str(item.get("text") or ""),
                    confidence=float(item.get("confidence", 1.0)),  # type: ignore[arg-type]
                    last_seen_at=p.ctx.now,
                    pinned=bool(item.get("pinned", False)),
                )
            )
        return out

    def _snapshot_refs(self, p: _Plan) -> dict[UUID, SnapshotRef]:
        assert p.pack is not None
        if p.pack.snapshot is None:
            return {}
        draft = p.pack.snapshot
        return {
            p.pack.snapshot_id: SnapshotRef(
                p.pack.snapshot_id, draft.digest(), tuple(i.model_dump(mode="json") for i in draft.items)
            )
        }

    def _plan_time(
        self, p: _Plan, spec: VideoSpec, refs: BuildRefs
    ) -> tuple[VideoSpec, dict[str, RouteDecision], dict[str, CBSContent], list[CompiledBehavior], dict[str, Any]]:
        """CBS, plan-time routing and compilation; compiler proposals are written into the spec with
        `derived_from: compiler_approximation` and the planned route digest (§13 stage 11 b–e)."""
        assert p.mode is not None
        editorial = frozenset(p.mode.editorial_methods)
        applied: list[str] = []
        for _ in range(3):
            graph = build_graph(spec, refs, self.bundle, self.deps.catalog)
            routes = {n.key: n.route for n in graph.nodes if n.route is not None}
            cbs = version_cbs(spec, refs, self.vocab, self.bundle.app.behavior)
            compiled = compile_version(
                spec,
                cbs,
                routes,
                self.deps.catalog.manifests,
                self.vocab,
                editorial_methods=editorial,
                stage="plan_time",
            )
            draft = SpecDraft(spec.model_dump(mode="json"))
            changed = self._apply_proposals(draft, compiled, applied)
            if not changed:
                return spec, routes, cbs, compiled, planned_routes(graph)
            spec = draft.spec()
            issues = [i for i in self._validate(p, spec) if i.severity == "error"]
            if issues:
                raise PlanningError("compiler proposals broke the plan", [f"{i.path}: {i.message}" for i in issues])
        raise PlanningError("compiler proposals did not converge after 3 passes")

    def _apply_proposals(self, draft: SpecDraft, compiled: Sequence[CompiledBehavior], applied: list[str]) -> bool:
        changed = False
        for plan in compiled:
            if plan.route_digest is None:
                continue
            for action in plan.editorial_actions:
                derived = [
                    {"kind": "compiler_approximation", "ref": action.item_ref, "route_digest": plan.route_digest}
                ]
                scene_key = _scene_of_ref(action.item_ref)
                if scene_key is None:
                    continue
                if action.kind == "punch_in" and action.at is not None:
                    at = (action.at.segment_key, action.at.word)
                    position = draft.position(scene_key, at)
                    shot = draft.base_shot_at(scene_key, position) if position is not None else None
                    if shot and any((m["at"]["segment_key"], m["at"]["word"]) == at for m in shot["camera"]["moves"]):
                        continue
                    if draft.add_camera_move(
                        scene_key, at, "punch_in", scale=self.config.punch_scale, derived_from=derived
                    ):
                        changed = True
                        applied.append(f"punch_in for {action.item_ref}")
                elif action.kind == "cutaway" and action.span is not None:
                    changed |= self._add_cutaway(draft, scene_key, action, derived, applied)
                elif action.kind == "sfx_cue" and action.at is not None:
                    if any(
                        any(d.get("ref") == action.item_ref for d in s.get("derived_from", []))
                        for s in draft.data["audio"]["sfx"]
                    ):
                        continue
                    draft.data["audio"]["sfx"].append(
                        {
                            "key": draft.new_key(KeyKind.SFX),
                            "at": {"segment_key": action.at.segment_key, "word": action.at.word},
                            "description": _sfx_description(action.item_ref),
                            "gain_db": -12.0,
                            "asset_id": None,
                            "derived_from": derived,
                        }
                    )
                    changed = True
                    applied.append(f"sfx for {action.item_ref}")
                elif action.kind == "shot_split" and action.at is not None:
                    changed |= self._split_shot(draft, scene_key, action, derived, applied)
        return changed

    def _add_cutaway(
        self, draft: SpecDraft, scene_key: str, action: Any, derived: list[dict[str, Any]], applied: list[str]
    ) -> bool:
        scene = draft.scene(scene_key)
        for shot in scene["shots"]:
            if any(d.get("ref") == action.item_ref for d in shot.get("derived_from", [])):
                return False
        talking = next((s for s in scene["shots"] if s["type"] == "talking_head"), None)
        world = scene.get("world") or {}
        scene["shots"].append(
            {
                "key": draft.new_key(KeyKind.SHOT),
                "type": "broll",
                "layer": "overlay",
                "span": action.span.model_dump(mode="json"),
                "character_key": None,
                "camera": {
                    "profile_id": talking["camera"]["profile_id"] if talking else "desk_mirrorless",
                    "framing": "insert",
                    "angle": "eye_level",
                    "moves": [],
                },
                "broll": {
                    "source": "generate",
                    "asset_id": None,
                    "world_bound": bool(world),
                    "prompt": "a quiet cutaway detail of the room, no people, shallow depth of field",
                    "allow_text_in_frame": False,
                },
                "takes": {"count": 1, "selected_take_key": None},
                "derived_from": derived,
            }
        )
        applied.append(f"cutaway for {action.item_ref}")
        return True

    def _split_shot(
        self, draft: SpecDraft, scene_key: str, action: Any, derived: list[dict[str, Any]], applied: list[str]
    ) -> bool:
        at = (action.at.segment_key, action.at.word)
        position = draft.position(scene_key, at)
        if position is None:
            return False
        shot = draft.base_shot_at(scene_key, position)
        if shot is None:
            return False
        rng = draft.span_range(scene_key, shot["span"])
        if rng is None or position <= rng[0]:
            return False
        tail = copy.deepcopy(shot)
        tail["key"] = draft.new_key(KeyKind.SHOT)
        shot["span"] = word_span(draft.ref(scene_key, rng[0]), draft.ref(scene_key, position - 1))
        tail["span"] = word_span(draft.ref(scene_key, position), draft.ref(scene_key, rng[1]))
        tail["derived_from"] = [*tail.get("derived_from", []), *derived]
        moves = shot["camera"]["moves"]
        shot["camera"]["moves"] = [
            m for m in moves if (draft.position(scene_key, (m["at"]["segment_key"], m["at"]["word"])) or 0) < position
        ]
        tail["camera"]["moves"] = [m for m in moves if m not in shot["camera"]["moves"]]  # keys are kept
        scene = draft.scene(scene_key)
        scene["shots"].insert(scene["shots"].index(shot) + 1, tail)
        applied.append(f"shot split for {action.item_ref}")
        return True

    @staticmethod
    def _approximations(spec: VideoSpec) -> list[tuple[str, str]]:
        out = []
        for _scene, shot in spec.shots():
            out += [(shot.key, d.ref) for d in shot.derived_from if str(d.kind) == "compiler_approximation"]
            for move in shot.camera.moves:
                out += [(move.key, d.ref) for d in move.derived_from if str(d.kind) == "compiler_approximation"]
        for sfx in spec.audio.sfx:
            out += [(sfx.key, d.ref) for d in sfx.derived_from if str(d.kind) == "compiler_approximation"]
        return out

    def _report(
        self, p: _Plan, spec: VideoSpec, cbs: Mapping[str, CBSContent], compiled: Sequence[CompiledBehavior]
    ) -> PlanReport:
        assert p.pack is not None and p.brief is not None and p.strategy is not None
        timeline = self._timeline(p, SpecDraft(spec.model_dump(mode="json")))
        timings: list[StateTiming] = []
        for scene in sorted(spec.scenes, key=lambda s: s.order):
            if scene.acting is None:
                continue
            ordered = sorted(scene.acting.states, key=lambda st: _start_of(timeline, st))
            for index, state in enumerate(ordered):
                first = _start_of(timeline, state)
                nxt = _start_of(timeline, ordered[index + 1]) if index + 1 < len(ordered) else None
                end = nxt if nxt is not None else _end_of(timeline, state)
                requested = self._requested_for(p, scene.key, state, spec)
                timings.append(
                    StateTiming(
                        state_key=state.key,
                        start_s=round(first, 3),
                        end_s=round(end, 3),
                        requested_start_s=requested,
                        drift_s=round(first - requested, 3) if requested is not None else None,
                    )
                )
        for timing in timings:
            if timing.drift_s is not None and abs(timing.drift_s) > 1.0:
                p.findings.append(
                    Finding(
                        kind="timing",
                        severity="info",
                        message=f"{timing.state_key} starts {timing.drift_s:+.1f} s from the requested "
                        f"{timing.requested_start_s:g} s (time is word-anchored, ADR 0004)",
                        detail={"state_key": timing.state_key},
                    )
                )
        p.findings += self._repetition(p, spec)
        for stage in p.template_stages:  # visible in previz, never a silent downgrade (rule 5)
            p.findings.append(
                Finding(
                    kind="other",
                    severity="warning",
                    message=f"The {stage} stage used its template: the model's output stayed invalid after "
                    f"{self.config.max_repairs} repairs",
                    detail={"stage": stage, "check": "template_stage"},
                )
            )
        coverage = predicted_coverage(cbs, compiled, self.vocab, stage="predicted")
        return PlanReport(
            version_id=spec.version_id,
            input_mode=spec.brief.input_mode,
            planner="llm" if p.llm else "template",
            estimated_duration_s=round(timeline.total_s, 2),
            target_duration_s=float(spec.meta.target_duration_s),
            timing_source="estimated",
            state_timings=timings,
            event_timings=event_timings(spec, _word_map(timeline)),
            predicted_coverage=coverage,
            memory_items_used=[UUID(str(i["item_id"])) for i in p.pack.snapshot_items],
            findings=p.findings,
            assumptions=p.assumptions,
            cost_estimate_usd=None,
        )

    def _requested_for(self, p: _Plan, scene_key: str, state: Any, spec: VideoSpec) -> float | None:
        if not p.requested_seconds:
            return None
        scene = spec.scene(scene_key)
        assert scene.acting is not None
        index = [s.key for s in scene.acting.states].index(state.key)
        return p.requested_seconds.get((scene_key, index))

    def _repetition(self, p: _Plan, spec: VideoSpec) -> list[Finding]:
        assert p.pack is not None and p.strategy is not None
        history = p.pack.recent_usage
        if not history:
            return []
        payload = usage_payload(spec, p.pack.character_key)
        pack_cfg = self.bundle.strategy_packs.get(p.strategy.strategy_pack)
        signature = {s.text: s.max_per_video for s in p.pack.creator.dna.speech.signature_phrases}
        found = [
            *self.guard.phrases(" ".join(s.text for s in spec.script.segments), history, signature),
            *self.guard.arc(
                payload["arc_signature"]["labels"],
                history,
                signature_format=bool(pack_cfg and pack_cfg.signature_format),
            ),
            *self.guard.visual(payload["visual_signature"], history),
        ]
        return [
            Finding(kind="repetition", severity="warning", message=f"{f.check}: {f.detail}", detail=f.as_dict())
            for f in found
        ]

    # ================================================================== helpers
    def _start_draft(self, p: _Plan) -> None:
        assert p.brief is not None and p.mode is not None and p.pack is not None and p.strategy is not None
        brief, strategy, pack, ctx, req = p.brief, p.strategy, p.pack, p.ctx, p.request
        hooks = [{"key": f"hk_{i + 1}", "text": h} for i, h in enumerate(strategy.hooks)]
        aspect = req.primary_aspect or "9:16"
        brand = ctx.brand
        caption_style = (
            req.caption_style_id
            or (brand.caption_style_id if brand is not None else None)
            or pack.creator.dna.editing.caption_style_preference
            or "bold_pop_highlight"
        )
        if caption_style not in self.bundle.caption_styles:
            caption_style = "bold_pop_highlight"
        p.draft = SpecDraft(
            {
                "schema_version": "1.0",
                "vocab_version": self.vocab.version,
                "tokenizer_version": TOKENIZER_VERSION,
                "video_id": str(ctx.video_id),
                "version_id": str(ctx.version_id),
                "parent_version_id": None,
                "meta": {
                    "title": brief.title,
                    "mode": brief.mode,
                    "language": brief.language,
                    "platform_targets": brief.platform_targets,
                    "primary_aspect": aspect,
                    "target_duration_s": brief.target_duration_s,
                    "quality_tier": req.quality_tier,
                    "template_ids": [],
                    "strategy_pack": strategy.strategy_pack,
                },
                "brief": {
                    "input_mode": brief.input_mode,
                    "raw_input": req.input,
                    "audience": strategy.audience or brief.audience,
                    "angle": strategy.angle or brief.angle,
                    "assumptions": [],
                    "hook_candidates": hooks,
                    "selected_hook_key": hooks[strategy.selected_hook]["key"] if hooks else None,
                    "sources_policy": brief.sources_policy,
                    "constraints": brief.constraints,
                },
                "research": None,
                "intent": {"video": strategy.video_intent.model_dump(mode="json")},
                "memory": {"snapshots": [{"character_key": pack.character_key, "snapshot_id": str(pack.snapshot_id)}]},
                "cast": [
                    {
                        "key": pack.character_key,
                        "role": req.cast[0].role if req.cast else "host",
                        "creator_version_id": str(pack.creator.creator_version_id),
                        "overrides": {
                            "appearance_version_id": None,
                            "voice_version_id": str(req.cast[0].voice_version_id)
                            if req.cast and req.cast[0].voice_version_id
                            else None,
                        },
                        "voice_prosody": None,
                    }
                ],
                "products": [],
                "script": {"segments": []},
                "scenes": [],
                "audio": {
                    "music": {"cues": []},
                    "sfx": [],
                    "acoustics": {"source": "world", "mic_profile": None, "room_profile": None},
                    "loudness": {"integrated_lufs": -14.0, "true_peak_dbtp": -1.0},
                },
                "captions": {
                    "enabled": True,
                    "style_id": caption_style,
                    "language": brief.language.split("-")[0],
                    "max_words_per_line": 3,
                    "highlight": "active_word",
                    "placement": "platform_safe_zone",
                    "translations": [],
                    "review_state": {},
                },
                "brand": {
                    "brand_kit_id": str(brand.brand_kit_id) if brand and brand.brand_kit_id else None,
                    "logo_overlay": bool(brand and brand.brand_kit_id and brand.logo_overlay),
                },
                "render": {
                    "outputs": B.render_outputs(self.bundle, brief.platform_targets, aspect),
                    "reframe": {"strategy": "subject_aware", "regenerate_if_crop_loss_above": 0.25},
                },
                "provenance": {"visible_label": "auto", "consent_ids": []},
                "assets": [],
                "effects": [],
                "generation": {"seed_namespace": str(ctx.video_id), "seed_overrides": {}, "engine_hints": {}},
                "locks": [],
            }
        )

    def _add_disclosure(self, p: _Plan, draft: SpecDraft) -> None:
        """Dramatization modes add an on-screen `disclosure` effect over every scene (§32)."""
        policy = self.bundle.testimonials
        assert p.mode is not None
        if (
            policy is None
            or p.mode.id not in policy.dramatization_modes
            or not policy.require_disclosure_for_dramatization
        ):
            return
        for scene in draft.scenes():
            draft.data["effects"].append(
                {
                    "key": draft.new_key(KeyKind.EFFECT),
                    "type": "disclosure",
                    "span": {"kind": "scene", "scene_key": scene["key"]},
                    "params": {"text": policy.disclosure_text},
                    "derived_from": [],
                    "label": "disclosure",
                }
            )
        p.note(f"{p.mode.label} adds the on-screen disclosure {policy.disclosure_text!r} (§32).")

    def _validate(self, p: _Plan, spec: VideoSpec) -> list[Any]:
        ctx = ValidationContext(
            vocab=self.vocab,
            refs=self._references(p),
            multi_character_enabled=self.bundle.app.features.multi_character_enabled,
            duration_tolerance=self.bundle.app.spec.duration_tolerance,
            default_wpm=self.bundle.app.spec.default_wpm,
            require_memory_snapshots=self.bundle.app.spec.require_memory_snapshots,
        )
        return validate_spec(spec, ctx)

    def _references(self, p: _Plan) -> InMemoryReferences:
        approved = RecordStatus.APPROVED
        refs = InMemoryReferences()
        for c in p.ctx.creators:
            refs.creator_versions[c.creator_version_id] = CreatorVersionInfo(
                c.creator_version_id, c.creator_id, approved, c.dna
            )
            if c.appearance_version_id and c.voice_version_id:
                refs.defaults[c.creator_version_id] = (c.appearance_version_id, c.voice_version_id)
            if c.appearance_version_id:
                refs.appearance_versions[c.appearance_version_id] = OwnedVersionInfo(
                    c.appearance_version_id, approved, c.creator_id
                )
            if c.voice_version_id:
                refs.voice_versions[c.voice_version_id] = VoiceVersionInfo(
                    c.voice_version_id, approved, c.creator_id, c.voice
                )
            for wardrobe_id in c.wardrobe_version_ids:
                refs.wardrobe_versions[wardrobe_id] = OwnedVersionInfo(wardrobe_id, approved, c.creator_id)
        for w in p.ctx.worlds:
            refs.world_versions[w.world_version_id] = WorldVersionInfo(w.world_version_id, approved, w.dna)
        if p.pack is not None:
            refs.snapshots[p.pack.snapshot_id] = SnapshotInfo(p.pack.snapshot_id, p.pack.creator.creator_version_id)
        return refs

    def _timeline(self, p: _Plan, draft: SpecDraft) -> Any:
        assert p.pack is not None and p.brief is not None
        wpm = p.pack.creator.wpm(p.brief.language, self.bundle.app.spec.default_wpm)
        spec_like = _SpecView(draft)
        return estimate_timeline(
            spec_like,  # type: ignore[arg-type]
            {p.pack.character_key: wpm},
            default_wpm=self.bundle.app.spec.default_wpm,
            pause_ms=self.vocab.pause_ms,
            timeline=self.bundle.app.render.timeline,
        )

    def _route_preview(self, p: _Plan) -> dict[str, list[str]]:
        """Candidates passing the capability, language and routing-profile filters (§13 stage 8)."""
        assert p.brief is not None
        profiles = self.deps.catalog.profiles
        routing = p.request.routing_profile or (
            "draft" if "draft" in profiles else next(iter(sorted(profiles)), "draft")
        )
        if routing not in profiles:
            return {}
        return {cap: preview(cap, p.brief.language, routing, self.deps.catalog) for cap in ("avatar.a2v", "voice.tts")}

    def _acting_render(self, p: _Plan, draft: SpecDraft, timeline: Any, tags: list[dict[str, Any]]) -> dict[str, Any]:
        assert p.pack is not None and p.brief is not None
        pack = p.pack
        manifests = self.deps.catalog.manifests
        preview_view: dict[str, list[dict[str, Any]]] = {}
        for capability, ids in p.route_preview.items():
            preview_view[capability] = []
            for adapter_id in ids:
                matrix = manifests[adapter_id].behavior_matrix if adapter_id in manifests else None
                controls = {}
                if matrix is not None:
                    for dim in sorted(self.vocab.dimensions):
                        decl = matrix.control(dim)
                        if decl.control not in ("none",):
                            controls[dim] = str(decl.control)
                preview_view[capability].append({"adapter": adapter_id, "controls": controls})
        scenes = []
        for scene in draft.scenes():
            world = p.choices[scene["key"]].world.dna
            scenes.append(
                {
                    "key": scene["key"],
                    "purpose": scene["purpose"],
                    "intent": scene["intent"],
                    "segments": [{"key": k, "words": _numbered(draft, k)} for k in scene["segment_keys"]],
                    "posture_allowed": next(
                        (list(z.allowed_postures) for z in world.zones if z.key == p.choices[scene["key"]].zone), []
                    ),
                    "elements": [{"key": e.key, "kind": str(e.kind), "label": e.label} for e in world.elements],
                    "proposals": [
                        self._proposal_view(scene["key"], pr)
                        for pr in p.policies.by_scene.get(scene["key"], {}).values()
                        if pr.channel in ("acting", "voice")
                    ],
                    "reveal_at": p.reveal.get(scene["key"]),
                }
            )
        dna = pack.creator.dna
        words_at = [
            {"segment_key": w.segment_key, "word": w.word, "start_s": round(w.start_s, 2)} for w in timeline.words
        ]
        return {
            "brief": self._brief_view(p.brief),
            "acting_brief": p.brief.acting_brief,
            "time_requests": [t.model_dump() for t in p.brief.time_requests],
            "word_times": words_at if p.brief.time_requests else [],
            "scenes": scenes,
            "persona": self._persona(pack),
            "behavior": dna.behavior.model_dump(mode="json"),
            "avoid_gestures": list(dna.avoidances.gestures),
            "memory": [
                i for i in pack.snapshot_items if not str(i.get("kind", "")).startswith(("persona_fact", "stance"))
            ],
            "recent_arcs": [list(u.arc) for u in pack.recent_usage],
            "tag_constraints": tags,
            "route_preview": preview_view,
            "vocab": self._acting_vocab(),
        }

    def _acting_vocab(self) -> dict[str, Any]:
        cats = [
            "emotion",
            "internal_state",
            "social_goal",
            "audience_goal",
            "performance_intent",
            "situation_kind",
            "audience_stance",
            "stimulus_kind",
            "trigger_kind",
            "transition_style",
            "event_purpose",
            "direction",
            "attention_target",
            "strategy.prosody",
            "strategy.gaze",
            "strategy.gesture",
            "strategy.posture",
            "strategy.reaction",
            "strategy.camera_awareness",
        ]
        out: dict[str, Any] = {c: sorted(self.vocab.tokens(c)) for c in cats}
        out["events"] = {
            k: {"dimension": e.dimension, "params": list(e.params)} for k, e in sorted(self.vocab.events.items())
        }
        out["annotation_tags"] = {
            t: sorted(self.vocab.tokens(f"annotation_tag.{t}"))
            for t in ("pause", "nonverbal_audio", "emphasis", "delivery")
        }
        return out

    def _intent_vocab(self, *, video: bool) -> dict[str, list[str]]:
        fields = (
            ["narrative_goal", "audience_effect", "persuasion_goal", "information_goal", "attention_goal", "cta_goal"]
            if video
            else [
                "narrative_goal",
                "emotional_goal",
                "audience_effect",
                "persuasion_goal",
                "information_goal",
                "attention_goal",
                "reveal_strategy",
                "performance_strategy",
            ]
        )
        return {f: sorted(self.vocab.tokens(f"intent.{f}")) for f in fields}

    def _annotation_problems(self, where: str, type_: str, tag: str) -> list[str]:
        if type_ not in ANNOTATION_TYPES:
            return [f"{where}: type {type_!r} is not one of {list(ANNOTATION_TYPES)}"]
        if not self.vocab.has(f"annotation_tag.{type_}", tag):
            allowed = ", ".join(sorted(self.vocab.tokens(f"annotation_tag.{type_}")))
            return [f"{where}: tag {tag!r} is not a {type_} tag; use one of: {allowed}"]
        return []

    @staticmethod
    def _proposal_view(scene_key: str, proposal: Any) -> dict[str, Any]:
        return {
            "scene_key": scene_key,
            "rule": proposal.rule_id,
            "channel": proposal.channel,
            "key": proposal.key,
            "value": proposal.value,
        }

    @staticmethod
    def _brief_view(brief: BriefOut) -> dict[str, Any]:
        return brief.model_dump(
            mode="json",
            include={
                "input_mode",
                "title",
                "mode",
                "language",
                "target_duration_s",
                "audience",
                "angle",
                "constraints",
                "assumptions",
            },
        )

    @staticmethod
    def _persona(pack: ContextPack) -> str:
        dna = pack.creator.dna
        return wrap_data(
            dna.model_dump_json(include={"identity", "personality", "speech", "behavior", "gesture", "gaze"}),
            kind="creator_dna",
            source_id=str(pack.creator.creator_version_id),
        )

    @staticmethod
    def _memory_values(pack: ContextPack, kind: str, field_name: str) -> list[str]:
        out: list[str] = []
        for item in pack.snapshot_items:
            value = item.get("value")
            if item.get("kind") == kind and isinstance(value, dict) and value.get(field_name):
                out.append(str(value[field_name]))
        return out

    async def _embed(self, texts: Sequence[str], language: str | None) -> tuple[list[list[float]], str] | None:
        """embed.text through the deps (None without an in-process adapter or on failure: keyword path)."""
        if self.deps.embed is None or not texts:
            return None
        try:
            return await self.deps.embed(list(texts), language)
        except Exception:  # an index never blocks planning
            return None

    async def _evidence(self, p: _Plan, query: str) -> list[dict[str, str]]:
        """Hybrid retrieval (keyword + embeddings of the same model) over the plan's evidence."""
        if p.index is None or not p.index.facts:
            return []
        ingest = self.bundle.app.research.ingest
        vector, model = None, None
        if any(f.embedding is not None for f in p.index.facts):
            embedded = await self._embed([query], p.brief.language if p.brief else None)
            if embedded is not None:
                vector, model = embedded[0][0], embedded[1]
        hits = p.index.search(
            query, self.bundle.app.research.top_k, query_vector=vector, model=model, min_cosine=ingest.min_cosine
        )
        return [{"id": f.evidence_id, "text": f.text, "source": f.source_title or ""} for f, _ in hits]

    async def _check_claims(
        self, p: _Plan, result: FactCheckOut, segments: list[tuple[str, str]]
    ) -> list[CheckedClaim]:
        """`ce_research.claims.enforce` over the model's claims and the detector's (Phase 12)."""
        assert p.research is not None
        closed = p.research.closed_book
        ingest = self.bundle.app.research.ingest
        by_id = p.evidence_by_id

        def evidence_of(evidence_id: str) -> ClaimEvidence | None:
            fact = by_id.get(evidence_id)
            return ClaimEvidence(evidence_id, fact.text, fact.trust) if fact is not None else None

        def search(text: str) -> list[ClaimEvidence]:
            if p.index is None:
                return []
            hits = p.index.search(text, self.bundle.app.research.top_k)
            return [ClaimEvidence(f.evidence_id, f.text, f.trust) for f, _ in hits]

        reported = [(c.segment_key, c.text, c.verdict, list(c.evidence_ids)) for c in result.claims]
        return enforce(
            reported,
            segments=segments,
            evidence_of=evidence_of,
            search=search,
            closed_book=closed,
            min_support=ingest.min_claim_support,
        )

    async def _semantic_hooks(self, p: _Plan, strategy: StrategyOut) -> StrategyOut:
        """The repetition guard with embeddings (§18.6, Phase 12): hooks that mean the same as a
        recent hook (cosine above `memory.embeddings.hook_max_cosine`, same embedding model) are
        rejected; the selected hook moves to the first one left, else the plan is flagged."""
        assert p.pack is not None and p.brief is not None
        history = [u for u in p.pack.recent_usage if u.hook_vector is not None]
        memory_config = self.bundle.memory
        if not history or memory_config is None:
            return strategy
        embedded = await self._embed(strategy.hooks, p.brief.language)
        if embedded is None:
            return strategy
        vectors, model = embedded
        found = self.guard.hooks(
            strategy.hooks,
            history,
            vectors=vectors,
            model=model,
            max_cosine=memory_config.embeddings.hook_max_cosine,
        )
        rejected = {f.subject for f in found}
        if not rejected:
            return strategy
        for f in found:
            p.findings.append(
                Finding(
                    kind="repetition",
                    severity="warning",
                    message=f"Hook {f.subject!r} {f.detail} ({f.score:.2f})",
                    detail={**f.as_dict(), "embedding_model": model},
                )
            )
        selected = strategy.hooks[strategy.selected_hook]
        if selected not in rejected:
            return strategy
        keep = next((i for i, h in enumerate(strategy.hooks) if h not in rejected), None)
        if keep is None:
            p.note("Every hook candidate means the same as a recent hook (embedding similarity); kept the first.")
            return strategy
        p.note(f"Selected hook replaced: {selected!r} repeats a recent hook (embedding similarity).")
        return strategy.model_copy(update={"selected_hook": keep})

    async def _semantic_hints(self, data: dict[str, Any], controls: list[ControlField]) -> None:
        """Embedding similarity for labels still unknown after the repairs (Phase 12): cosine of the
        label vs each token's description, added to the mapper's lexical score."""
        unknown = self.mapper.unknown(data, controls)
        if not unknown or self.deps.embed is None:
            return
        for category, label in unknown:
            tokens = sorted(self.vocab.tokens(category))
            texts = [label] + [self.mapper.token_text(category, t) for t in tokens]
            embedded = await self._embed(texts, None)
            if embedded is None:
                return
            vectors = embedded[0]
            from ce_memory.text import cosine

            self.mapper.semantic[(category, label)] = {
                t: max(0.0, cosine(vectors[0], vectors[i + 1])) for i, t in enumerate(tokens)
            }


# ---------------------------------------------------------------------- module helpers


class _SpecView:
    """Duck-typed VideoSpec over a draft for `estimate_timeline` (before the spec validates)."""

    def __init__(self, draft: SpecDraft) -> None:
        spec = draft.data
        self.scenes = [_Obj(s) for s in spec["scenes"]]
        self.script = _Script(draft)


class _Obj:
    def __init__(self, data: dict[str, Any]) -> None:
        self.key = data["key"]
        self.order = data["order"]
        self.segment_keys = data["segment_keys"]
        pacing = data.get("pacing")
        self.pacing = type("P", (), {"target_wpm_delta": pacing["target_wpm_delta"]})() if pacing else None
        self.shots: list[Any] = []


class _Script:
    def __init__(self, draft: SpecDraft) -> None:
        self.draft = draft

    def segment(self, key: str) -> Any:
        from ce_core.spec.videospec import Segment

        return Segment.model_validate(self.draft.segment(key))


def _slug(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "host"


def _first_sentence(text: str) -> str:
    spans = sentence_spans(text, 0, len(text))
    return text[spans[0][0] : spans[0][1]] if spans else text


def _canonical_tags() -> list[str]:
    from ce_voice import CANONICAL_TAGS

    return [f"[{t}]" for t in CANONICAL_TAGS]


def _numbered(draft: SpecDraft, segment_key: str) -> str:
    return " ".join(f"{t.index}:{t.text}" for t in draft.tokens(segment_key))


def _scene_of_ref(ref: str) -> str | None:
    if ref.startswith("/scenes[") and "]" in ref:
        return ref[len("/scenes[") : ref.index("]")]
    return None


def _sfx_description(item_ref: str) -> str:
    return "soft accent under the moment" if "/events[" in item_ref else "light transition accent"


def _word_map(timeline: Any) -> dict[str, list[tuple[float, float]]]:
    """Estimated word times as `segment_key → [(start_s, end_s)]`, indexed by word."""
    sizes: dict[str, int] = {}
    for w in timeline.words:
        sizes[w.segment_key] = max(sizes.get(w.segment_key, 0), w.word + 1)
    out = {key: [(0.0, 0.0)] * size for key, size in sizes.items()}
    for w in timeline.words:
        out[w.segment_key][w.word] = (w.start_s, w.end_s)
    return out


def _start_of(timeline: Any, state: Any) -> float:
    span = state.span
    if hasattr(span, "start") and hasattr(span.start, "segment_key"):
        w = timeline.word(span.start.segment_key, span.start.word)
        return float(w.start_s) if w else 0.0
    return 0.0


def _end_of(timeline: Any, state: Any) -> float:
    span = state.span
    if hasattr(span, "end") and hasattr(span.end, "segment_key"):
        w = timeline.word(span.end.segment_key, span.end.word)
        return float(w.end_s) if w else 0.0
    return 0.0
