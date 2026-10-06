"""Planning, previz and approval (§30, §13): create a video from text (`PlanVideoWorkflow`), read the
previz plan report and the intent trace, replan, and approve into generation.

The API never calls the LLM (§9). `POST /v1/projects/{id}/videos` writes the video row and the plan
job and returns `202 {video_id, version_id, job_id}`. The version id is allocated here; its row
appears when planning finishes (a version's spec is an immutable identity column, §12.8). Follow
the job (`GET /v1/jobs/{id}` or the event stream) until the version is `planned`, then
`previz_ready`; then `:approve` starts generation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.behavior.plan_report import PlanReport
from ce_core.enums import JobKind, VersionFlag, VersionOrigin, VersionState
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_core.ids import new_id
from ce_core.spec.anchors import WordSpan
from ce_core.spec.videospec import VideoSpec
from ce_core.text import tokenize
from ce_db import execution as rec
from ce_db.models.assets import Artifact, Asset, ExecutionNode, GenerationJob
from ce_db.models.creators import Creator, VoiceVersion, WardrobeVersion
from ce_db.models.videos import DirectorRun, Project, Video, VideoVersion
from ce_db.models.worlds import World, WorldVersion
from ce_director.models import CastRequest, PlanRequest
from ce_storage.content import ContentStore
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from ce_api.common import audit, begin_idempotent, finish_idempotent
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.errors import ApprovalBlockedError
from ce_api.jobs import create_job, start_job, start_studio_job
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["planning"])

Hint = Annotated[str, Field(min_length=1, max_length=300)]


class StyleOptions(Body):
    camera_profile_id: str | None = None
    caption_style_id: str | None = None
    music_mood: str | None = None


class AdvancedOptions(Body):
    """Typed advanced options (§30): takes, routing profile, strategy pack, intent and acting hints.
    Hints are data for the Director (I10), never instructions."""

    takes: int = Field(default=1, ge=1, le=4)
    routing_profile: str | None = None
    strategy_pack: str | None = None
    intent_hints: list[Hint] = Field(default_factory=list, max_length=10)
    acting_hints: list[Hint] = Field(default_factory=list, max_length=10)


class CreateVideo(Body):
    """`POST /v1/projects/{project_id}/videos`: the fields become Director constraints (§30)."""

    model_config = examples(
        [
            {"input": "Create a 30-second TikTok explaining why most people misunderstand AI agents."},
            {
                "input": "Here is my exact script: Most people think a password manager is just a vault.",
                "input_mode": "exact_script",
                "target_duration_s": 20,
                "advanced": {"acting_hints": ["dry, deadpan delivery"]},
            },
        ]
    )
    input: str = Field(min_length=1, max_length=50_000)
    input_mode: Literal["auto", "idea", "structured", "exact_script", "brain_dump"] = "auto"
    cast: list[CastRequest] = Field(default_factory=list, max_length=1, description="one presenter in the MVP")
    world_id: UUID | None = None
    mode: str | None = None
    platform_targets: list[str] = Field(default_factory=list)
    primary_aspect: Literal["9:16", "16:9", "1:1", "4:5"] | None = None
    target_duration_s: float | None = Field(default=None, gt=0, le=600)
    language: str | None = None
    quality_tier: Literal["draft", "final"] = "draft"
    style: StyleOptions = Field(default_factory=StyleOptions)
    sources: list[UUID] = Field(
        default_factory=list, max_length=50, description="persistent research sources (Phase 12)"
    )
    sources_policy: Literal["open", "closed_book"] | None = None
    budget_usd: float | None = Field(default=None, ge=0)
    advanced: AdvancedOptions = Field(default_factory=AdvancedOptions)

    def to_request(self) -> PlanRequest:
        return PlanRequest(
            input=self.input,
            input_mode=self.input_mode,
            cast=self.cast,
            world_id=self.world_id,
            mode=self.mode,
            platform_targets=self.platform_targets,
            primary_aspect=self.primary_aspect,
            target_duration_s=self.target_duration_s,
            language=self.language,
            quality_tier=self.quality_tier,
            camera_profile_id=self.style.camera_profile_id,
            caption_style_id=self.style.caption_style_id,
            music_mood=self.style.music_mood,
            sources_policy=self.sources_policy,
            sources=self.sources,
            strategy_pack=self.advanced.strategy_pack,
            takes=self.advanced.takes,
            routing_profile=self.advanced.routing_profile,
            intent_hints=self.advanced.intent_hints,
            acting_hints=self.advanced.acting_hints,
        )


class PlanAccepted(Out):
    video_id: UUID
    version_id: UUID = Field(description="the version planning will create (its row appears once planned)")
    job_id: UUID


class ReplanBody(Body):
    model_config = examples([{"instruction": "make the opening more aggressive"}, {"refresh_memory": True}])
    scope: list[str] | None = Field(default=None, description="scene keys (recorded; Phase 4 replans the whole plan)")
    instruction: str | None = Field(default=None, min_length=1, max_length=2000)
    refresh_memory: bool = False


class ApproveBody(Body):
    model_config = examples([{}, {"overrides": ["claim:clm_1"], "reason": "The claim is common knowledge."}])
    overrides: list[str] = Field(default_factory=list, description="ids of overridable blocking findings")
    reason: str | None = Field(default=None, max_length=2000)


class GenerationAccepted(Out):
    job_id: UUID
    version_id: UUID


class BlockingFinding(Out):
    id: str | None
    kind: str
    message: str
    overridable: bool


class PrevizOut(Out):
    version_id: UUID
    state: str
    flags: list[str]
    planner: str | None
    timing_source: str | None
    cost_estimate_usd: float | None
    plan_report: dict[str, Any] | None = Field(
        description="durations and drift, predicted coverage, memory used, repetition and contradiction "
        "reports, world proposals, findings and cost"
    )
    blocking: list[BlockingFinding] = Field(description="what blocks approval; overridable ones need a reason")


class DerivedElement(Out):
    path: str
    derived_from: list[dict[str, Any]]


class SceneIntentTrace(Out):
    scene_key: str
    intent: dict[str, Any]
    derived: list[DerivedElement]
    annotations: list[dict[str, Any]] = Field(description="script annotations the intent policies added")
    decisions: list[dict[str, Any]] = Field(description="intent policy decisions (applied or not, and why)")


class IntentOut(Out):
    version_id: UUID
    video: dict[str, Any]
    derived: list[DerivedElement] = Field(description="video-level elements with a derived_from trace")
    scenes: list[SceneIntentTrace]


class Option(Out):
    id: str
    label: str


class ModeOption(Option):
    description: str
    maturity: str
    aspects: list[str]
    duration_s: dict[str, float] = Field(description="min, max and default duration")
    plannable: bool = Field(description="false for experimental modes (the API refuses them)")


class PlatformOption(Option):
    aspects: list[str]


class LanguageOption(Option):
    support: str = Field(description="production, beta or unsupported")
    rtl: bool


class RoutingOption(Option):
    quality_tier: str


class CreateOptions(Out):
    """Everything the Create wizard offers, read from configuration (the UI hard-codes none of it)."""

    input_modes: list[str]
    quality_tiers: list[str]
    modes: list[ModeOption]
    platforms: list[PlatformOption]
    aspects: list[str]
    camera_profiles: list[Option]
    caption_styles: list[Option]
    music_moods: list[str] = Field(description="suggestions; the mood is free text for the music engine")
    languages: list[LanguageOption]
    strategy_packs: list[Option]
    routing_profiles: list[RoutingOption]
    default_mode: str | None


class MediaLink(Out):
    url: str
    expires_at: datetime


class StoryboardShot(Out):
    scene_key: str
    shot_key: str
    order: int = Field(description="position in the video")
    type: str
    text: str = Field(description="the words the shot covers")
    camera_profile_id: str | None
    camera_position_key: str | None
    keyframe: MediaLink | None = Field(description="the previz or build keyframe, when one exists")
    plate: MediaLink | None = Field(description="the world plate for the scene's camera position and light")


class StoryboardOut(Out):
    version_id: UUID
    shots: list[StoryboardShot]


# ------------------------------------------------------------------------------------- helpers
def _build_arg(services: ServicesDep, org_id: UUID, job_id: UUID, version_id: UUID) -> dict[str, Any]:
    cfg = services.effective.bundle.app.build
    prefix = services.workflows.prefix
    return {
        "org_id": str(org_id),
        "job_id": str(job_id),
        "version_id": str(version_id),
        "queues": {"orchestrator": f"{prefix}orchestrator", "render": f"{prefix}render"},
        "max_parallel": cfg.max_parallel_nodes,
        "model_timeout_s": cfg.model_node_timeout_s,
        "cpu_timeout_s": cfg.cpu_node_timeout_s,
        "render_timeout_s": cfg.render_node_timeout_s,
    }


def _check_request(services: ServicesDep, request: PlanRequest) -> None:
    """Explicit fields must name configured things; the Director treats them as constraints."""
    bundle = services.effective.bundle
    issues: list[Issue] = []

    def check(value: str | None, known: Any, code: str, path: str) -> None:
        if value is not None and value not in known:
            issues.append(Issue(code, f"unknown {code.replace('_', ' ')} {value!r}", path))

    check(request.mode, bundle.modes, "mode", "/mode")
    if request.mode in bundle.modes and str(bundle.modes[request.mode].maturity) == "experimental":
        issues.append(Issue("mode", f"mode {request.mode!r} is experimental and cannot be planned", "/mode"))
    for target in request.platform_targets:
        check(target, bundle.platforms, "platform", "/platform_targets")
    check(request.strategy_pack, bundle.strategy_packs, "strategy_pack", "/advanced/strategy_pack")
    check(request.routing_profile, bundle.routing, "routing_profile", "/advanced/routing_profile")
    check(request.camera_profile_id, bundle.camera_profiles, "camera_profile", "/style/camera_profile_id")
    check(request.caption_style_id, bundle.caption_styles, "caption_style", "/style/caption_style_id")
    languages = bundle.languages.languages if bundle.languages else {}
    if request.language is not None and request.language.split("-")[0].lower() not in languages:
        issues.append(Issue("language", f"unsupported language {request.language!r}", "/language"))
    if issues:
        raise InvalidInputError("the request has invalid fields", issues=issues)


async def _plan_report(session: Any, services: ServicesDep, org_id: UUID, version: VideoVersion) -> PlanReport | None:
    if version.plan_report_artifact_id is None:
        return None
    artifact = await session.get(Artifact, version.plan_report_artifact_id)
    if artifact is None or artifact.org_id != org_id:
        return None
    store = ContentStore(services.storage, services.settings.s3_bucket_artifacts)
    return PlanReport.model_validate_json(await store.read_bytes(artifact.sha256))


def _blocking(report: PlanReport | None) -> list[BlockingFinding]:
    if report is None:
        return []
    return [
        BlockingFinding(
            id=f.detail.get("id"), kind=f.kind, message=f.message, overridable=bool(f.detail.get("overridable"))
        )
        for f in report.findings
        if f.severity == "blocking"
    ]


def _walk(node: Any, path: str, out: list[DerivedElement]) -> None:
    """Every spec element that records why it exists (`derived_from`), with its SpecPath."""
    if not isinstance(node, dict):
        return
    if node.get("derived_from"):
        out.append(DerivedElement(path=path, derived_from=list(node["derived_from"])))
    for name, value in node.items():
        if name == "derived_from":
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "key" in item:
                    _walk(item, f"{path}/{name}[{item['key']}]", out)
        elif isinstance(value, dict):
            _walk(value, f"{path}/{name}", out)


# ------------------------------------------------------------------------------------- routes
async def _check_sources(session: Any, org_id: UUID, project_id: UUID, source_ids: list[UUID]) -> None:
    """Attached research sources must be this project's (or org-wide) and ingested (Phase 12)."""
    from ce_db.models.research import ResearchSource

    if not source_ids:
        return
    rows = (
        await session.execute(
            sa.select(ResearchSource.id, ResearchSource.project_id, ResearchSource.status).where(
                ResearchSource.org_id == org_id, ResearchSource.id.in_(source_ids)
            )
        )
    ).all()
    found = {r[0]: r for r in rows}
    issues = []
    for i, source_id in enumerate(source_ids):
        row = found.get(source_id)
        if row is None or row[1] not in (project_id, None):
            issues.append(Issue("unknown_source", "not a research source of this project", path=f"/sources/{i}"))
        elif row[2] != "ingested":
            issues.append(Issue("source_not_ingested", f"the source is {row[2]}", path=f"/sources/{i}"))
    if issues:
        raise InvalidInputError("research sources cannot be used", issues=issues)


async def _check_cast_versions(session: Any, org_id: UUID, cast: list[Any]) -> None:
    """A cast member's voice and wardrobe versions exist in this org: otherwise the request was accepted
    and the plan job failed later on an unknown reference (audit A12)."""
    issues: list[Issue] = []
    for index, member in enumerate(cast):
        for field, model, what in (
            ("voice_version_id", VoiceVersion, "voice version"),
            ("wardrobe_version_id", WardrobeVersion, "wardrobe version"),
        ):
            ref = getattr(member, field)
            if ref is None:
                continue
            found = (
                await session.execute(sa.select(model.id).where(model.org_id == org_id, model.id == ref))
            ).scalar_one_or_none()
            if found is None:
                issues.append(
                    Issue(
                        "reference_missing", f"{what} not found", path=f"/cast/{index}/{field}", detail={"id": str(ref)}
                    )
                )
    if issues:
        raise InvalidInputError("unknown cast references", issues=issues)


@router.post("/v1/projects/{project_id}/videos", status_code=202, response_model=PlanAccepted)
async def create_video(
    project_id: UUID,
    body: CreateVideo,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    await get_scoped(session, Project, principal.ctx, project_id, "project")
    await _check_sources(session, principal.org_id, project_id, body.sources)
    try:
        plan_request = body.to_request()
    except ValidationError as exc:
        # The Director's PlanRequest is stricter than this body in places: answer 422, never a 500.
        issues = [Issue("invalid", e["msg"], path="/" + "/".join(str(p) for p in e["loc"])) for e in exc.errors()]
        raise InvalidInputError("the request does not form a valid plan request", issues=issues) from exc
    _check_request(services, plan_request)
    for member in body.cast:
        await get_scoped(session, Creator, principal.ctx, member.creator_id, "creator")
    await _check_cast_versions(session, principal.org_id, body.cast)
    if body.world_id is not None:
        await get_scoped(session, World, principal.ctx, body.world_id, "world")
    video = Video(
        org_id=principal.org_id,
        project_id=project_id,
        title=" ".join(body.input.split()[:8])[:200],
        mode=body.mode or "",
        budget_usd=body.budget_usd,
    )
    session.add(video)
    await session.flush()
    version_id = new_id()
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.PLAN,
        target_type="video",
        target_id=video.id,
        requested_by=principal.user_id,
        input={
            "request": plan_request.model_dump(mode="json"),
            "video_id": str(video.id),
            "version_id": str(version_id),
            "origin": VersionOrigin.PLAN.value,
        },
    )
    accepted = PlanAccepted(video_id=video.id, version_id=version_id, job_id=job.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    arg = _build_arg(services, principal.org_id, job.id, version_id)
    background.add_task(start_job, services, principal.org_id, job.id, JobKind.PLAN, arg)
    return accepted


@router.get("/v1/versions/{version_id}/previz", response_model=PrevizOut)
async def get_previz(version_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> PrevizOut:
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    report = await _plan_report(session, services, principal.org_id, version)
    return PrevizOut(
        version_id=version.id,
        state=version.state,
        flags=list(version.flags or []),
        planner=report.planner if report else None,
        timing_source=report.timing_source if report else None,
        cost_estimate_usd=float(version.cost_estimate_usd) if version.cost_estimate_usd is not None else None,
        plan_report=report.model_dump(mode="json") if report else None,
        blocking=_blocking(report),
    )


@router.get("/v1/versions/{version_id}/intent", response_model=IntentOut)
async def get_intent(version_id: UUID, principal: Reader, session: DbSession) -> IntentOut:
    """Video and scene intent with the `derived_from` trace (§14): which spec elements an intent
    policy, the compiler or the request produced, and every intent policy decision."""
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    spec = version.spec
    run = (
        await session.execute(
            sa.select(DirectorRun.output)
            .where(
                DirectorRun.org_id == principal.org_id,
                DirectorRun.version_id == version_id,
                DirectorRun.stage == "intent_policy",
            )
            .order_by(DirectorRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    decisions = list((run or {}).get("decisions", []))
    derived: list[DerivedElement] = []
    _walk(spec, "", derived)
    policy_annotations = [
        {"segment_key": seg["key"], "path": f"/script/segments[{seg['key']}]/annotations[{a['key']}]", **a}
        for seg in spec["script"]["segments"]
        for a in seg.get("annotations", [])
        if a.get("source") == "intent_policy"
    ]
    scenes = []
    for scene in sorted(spec["scenes"], key=lambda s: s["order"]):
        prefix = f"/scenes[{scene['key']}]"
        keys = set(scene["segment_keys"])
        scenes.append(
            SceneIntentTrace(
                scene_key=scene["key"],
                intent=scene["intent"],
                derived=[d for d in derived if d.path == prefix or d.path.startswith(prefix + "/")],
                annotations=[a for a in policy_annotations if a["segment_key"] in keys],
                decisions=[d for d in decisions if d.get("scene_key") == scene["key"]],
            )
        )
    return IntentOut(
        version_id=version.id,
        video=spec["intent"]["video"],
        derived=[d for d in derived if not d.path.startswith("/scenes[")],
        scenes=scenes,
    )


@router.get("/v1/create-options", response_model=CreateOptions)
async def create_options(principal: Reader, services: ServicesDep) -> CreateOptions:
    bundle = services.effective.bundle
    modes = [
        ModeOption(
            id=m.id,
            label=m.label,
            description=m.description,
            maturity=str(m.maturity),
            aspects=[str(a) for a in m.aspects],
            duration_s={"min": m.duration_s.min, "max": m.duration_s.max, "default": m.duration_s.default},
            plannable=str(m.maturity) != "experimental",
        )
        for m in sorted(bundle.modes.values(), key=lambda m: (str(m.maturity) == "experimental", m.label))
    ]
    platforms = [
        PlatformOption(id=pl.id, label=pl.label, aspects=sorted({str(r.aspect) for r in pl.render_presets}))
        for pl in sorted(bundle.platforms.values(), key=lambda pl: pl.label)
    ]
    music = bundle.director.music if bundle.director else None
    languages = bundle.languages.languages if bundle.languages else {}
    return CreateOptions(
        input_modes=["auto", "idea", "structured", "exact_script", "brain_dump"],
        quality_tiers=["draft", "final"],
        modes=modes,
        platforms=platforms,
        aspects=["9:16", "16:9", "1:1", "4:5"],
        camera_profiles=[
            Option(id=c.id, label=c.label)
            for c in sorted(bundle.camera_profiles.values(), key=lambda c: c.label)
            if str(c.maturity) != "experimental"
        ],
        caption_styles=[
            Option(id=c.id, label=c.label) for c in sorted(bundle.caption_styles.values(), key=lambda c: c.label)
        ],
        music_moods=[music.default_mood, music.tension_mood, music.resolve_mood] if music else [],
        languages=[
            LanguageOption(id=code, label=entry.label, support=str(entry.support), rtl=entry.rtl)
            for code, entry in languages.items()
        ],
        strategy_packs=[
            Option(id=k.id, label=k.label) for k in sorted(bundle.strategy_packs.values(), key=lambda k: k.label)
        ],
        routing_profiles=[
            RoutingOption(id=r.id, label=r.label, quality_tier=r.quality_tier) for r in bundle.routing.values()
        ],
        default_mode=bundle.director.template.mode if bundle.director else None,
    )


def _span_text(spec: VideoSpec, span: Any) -> str:
    if not isinstance(span, WordSpan):
        return ""
    words: list[str] = []
    keys = [k for scene in sorted(spec.scenes, key=lambda s: s.order) for k in scene.segment_keys]
    inside = False
    for key in keys:
        tokens = [t.text for t in tokenize(spec.script.segment(key).text)]
        for index, token in enumerate(tokens):
            if key == span.start.segment_key and index == span.start.word:
                inside = True
            if inside:
                words.append(token)
            if key == span.end.segment_key and index == span.end.word:
                return " ".join(words)
    return " ".join(words)


@router.get("/v1/versions/{version_id}/storyboard", response_model=StoryboardOut)
async def get_storyboard(
    version_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> StoryboardOut:
    """The previz storyboard (§31): one card per shot with its words, the keyframe previz (or the
    build) produced, and the world plate of the scene's camera position, time of day and weather."""
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    spec = VideoSpec.model_validate(version.spec)
    ttl = services.config.storage.presign_ttl_s
    nodes = (
        await session.execute(
            sa.select(ExecutionNode.shot_key, ExecutionNode.node_key, ExecutionNode.artifact_ids)
            .where(
                ExecutionNode.org_id == principal.org_id,
                ExecutionNode.version_id == version_id,
                ExecutionNode.node_kind == "image.keyframe",
                ExecutionNode.status.in_(("succeeded", "cached")),
            )
            .order_by(ExecutionNode.created_at)
        )
    ).all()
    artifact_ids = {a for n in nodes for a in (n.artifact_ids or [])}
    images: dict[UUID, Artifact] = {}
    if artifact_ids:
        rows = await session.execute(
            sa.select(Artifact).where(
                Artifact.org_id == principal.org_id, Artifact.id.in_(artifact_ids), Artifact.kind == "image"
            )
        )
        images = {a.id: a for a in rows.scalars()}
    keyframes: dict[str, Artifact] = {}
    for node in nodes:
        shot = node.shot_key or node.node_key.split(":", 1)[-1]
        found = next((images[a] for a in (node.artifact_ids or []) if a in images), None)
        if found is not None:
            keyframes[shot] = found  # the latest job wins
    plates: dict[tuple[str, str, str, str], UUID] = {}
    world_ids = {s.world.world_version_id for s in spec.scenes if s.world}
    if world_ids:
        worlds = await session.execute(
            sa.select(WorldVersion).where(WorldVersion.org_id == principal.org_id, WorldVersion.id.in_(world_ids))
        )
        for wv in worlds.scalars():
            for position, by_time in (wv.plates or {}).items():
                for time_of_day, by_weather in (by_time or {}).items():
                    for weather, asset_id in (by_weather or {}).items():
                        plates[(str(wv.id), position, time_of_day, weather)] = UUID(str(asset_id))
    assets: dict[UUID, Asset] = {}
    if plates:
        found_assets = await session.execute(
            sa.select(Asset).where(Asset.org_id == principal.org_id, Asset.id.in_(set(plates.values())))
        )
        assets = {a.id: a for a in found_assets.scalars()}

    async def link(bucket: str, key: str) -> MediaLink:
        signed = await services.storage.presign_get(bucket, key, ttl_s=ttl)
        return MediaLink(url=signed.url, expires_at=signed.expires_at)

    shots: list[StoryboardShot] = []
    for order, (scene, shot) in enumerate(
        (sc, sh) for sc in sorted(spec.scenes, key=lambda s: s.order) for sh in sc.shots
    ):
        world = scene.world
        keyframe = keyframes.get(shot.key)
        plate_asset = None
        if world is not None:
            plate_id = plates.get(
                (str(world.world_version_id), world.camera_position_key, str(world.time_of_day), str(world.weather))
            )
            plate_asset = assets.get(plate_id) if plate_id else None
        shots.append(
            StoryboardShot(
                scene_key=scene.key,
                shot_key=shot.key,
                order=order,
                type=str(shot.type),
                text=_span_text(spec, shot.span),
                camera_profile_id=shot.camera.profile_id,
                camera_position_key=world.camera_position_key if world else None,
                keyframe=await link(services.settings.s3_bucket_artifacts, keyframe.storage_key) if keyframe else None,
                plate=await link(services.settings.s3_bucket_assets, plate_asset.storage_key) if plate_asset else None,
            )
        )
    return StoryboardOut(version_id=version.id, shots=shots)


async def _plan_job_input(session: Any, org_id: UUID, version_id: UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.select(GenerationJob.input)
            .where(
                GenerationJob.org_id == org_id,
                GenerationJob.kind == JobKind.PLAN.value,
                GenerationJob.video_version_id == version_id,
                GenerationJob.status == "succeeded",
            )
            .order_by(GenerationJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or "request" not in row:
        raise ConflictError("this version was not planned from a request, so it cannot be replanned")
    return dict(row)


@router.post("/v1/versions/{version_id}:replan", status_code=202, response_model=PlanAccepted)
async def replan(
    version_id: UUID,
    body: ReplanBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """The previz "Regenerate plan" (§30): a new version planned from the same request (plus the
    instruction), reusing the pinned memory snapshots unless `refresh_memory` (§18.5). `scope` is
    recorded; scene-scoped replans arrive with edits (Phase 6)."""
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    source = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    allowed = (VersionState.PLANNED.value, VersionState.PREVIZ_READY.value, VersionState.FAILED.value)
    if source.state not in allowed:
        raise ConflictError(f"a {source.state} version cannot be replanned; replan before approval")
    if body.scope:
        unknown = sorted(set(body.scope) - {s["key"] for s in source.spec["scenes"]})
        if unknown:
            issues = [Issue("scope", f"no scene {k}", "/scope") for k in unknown]
            raise InvalidInputError("unknown scene keys", issues=issues)
    previous = await _plan_job_input(session, principal.org_id, version_id)
    plan_request = dict(previous["request"])
    if body.instruction:  # instructions accumulate across replans; the latest comes last
        earlier = plan_request.get("instruction")
        combined = f"{earlier}\nThen: {body.instruction}" if earlier else body.instruction
        plan_request["instruction"] = combined[-4000:]
    new_version = new_id()
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.PLAN,
        target_type="video",
        target_id=source.video_id,
        requested_by=principal.user_id,
        input={
            "request": PlanRequest.model_validate(plan_request).model_dump(mode="json"),
            "video_id": str(source.video_id),
            "version_id": str(new_version),
            "origin": VersionOrigin.REPLAN.value,
            "parent_version_id": str(source.id),
            "reuse_snapshots": not body.refresh_memory,
            "scope": body.scope,
        },
    )
    accepted = PlanAccepted(video_id=source.video_id, version_id=new_version, job_id=job.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    arg = _build_arg(services, principal.org_id, job.id, new_version)
    background.add_task(start_job, services, principal.org_id, job.id, JobKind.PLAN, arg)
    return accepted


async def _claim_ledger(session: Any, org_id: UUID, video_id: UUID) -> dict[str, Any]:
    from ce_db.models.research import Claim

    rows = (
        (
            await session.execute(
                sa.select(Claim).where(Claim.org_id == org_id, Claim.video_id == video_id).with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return {r.claim_key: r for r in rows}


@router.post("/v1/versions/{version_id}:approve", status_code=202, response_model=GenerationAccepted)
async def approve(
    version_id: UUID,
    body: ApproveBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Generation starts only after explicit approval (§13). Approval needs a `previz_ready` version,
    no pending world proposal and every blocking finding resolved: policy findings (blocklists, the
    testimonial guard) cannot be overridden; overridable ones (unsupported claims, contradictions of
    pinned facts) must be listed in `overrides` with a `reason`, and each override is audit-logged."""
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await lock_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if version.state != VersionState.PREVIZ_READY.value:
        raise ConflictError(f"approval needs a previz_ready version; this one is {version.state}")
    issues: list[Issue] = []
    if VersionFlag.NEEDS_WORLD_APPROVAL.value in (version.flags or []):
        issues.append(Issue("needs_world_approval", "approve the proposed world version first (§19.2)"))
    report = await _plan_report(session, services, principal.org_id, version)
    if report is None:
        raise NotFoundError("the version has no plan report", table="artifacts")
    overridden: list[BlockingFinding] = []
    ledger = await _claim_ledger(session, principal.org_id, version.video_id)
    for finding in _blocking(report):
        detail = {"finding_id": finding.id, "kind": finding.kind}
        claim = (
            ledger.get(finding.id.removeprefix("claim:")) if finding.id and finding.id.startswith("claim:") else None
        )
        if not finding.overridable:
            issues.append(Issue("blocking_finding", f"{finding.message} (cannot be overridden)", detail=detail))
        elif claim is not None and claim.override_by is not None and claim.overridable:
            continue  # overridden in the claim ledger (`POST /v1/claims/{id}:override`)
        elif finding.id not in body.overrides:
            issues.append(Issue("blocking_finding", f"{finding.message} (override it with a reason)", detail=detail))
        else:
            overridden.append(finding)
    unknown = sorted(set(body.overrides) - {f.id for f in overridden if f.id})
    if unknown:
        issues.append(Issue("override_unknown", f"no overridable blocking finding {', '.join(unknown)}", "/overrides"))
    if overridden and not (body.reason and body.reason.strip()):
        issues.append(Issue("override_reason", "overriding a blocking finding needs a reason", "/reason"))
    if issues:
        raise ApprovalBlockedError("the version cannot be approved yet", issues=issues)
    for finding in overridden:
        await audit(
            session,
            principal,
            "plan.finding_override",
            "video_version",
            version.id,
            request=request,
            after={"finding": finding.model_dump(), "reason": body.reason},
        )
        claim = (
            ledger.get(finding.id.removeprefix("claim:")) if finding.id and finding.id.startswith("claim:") else None
        )
        if claim is not None:  # the ledger records the override (Phase 12)
            claim.override_by, claim.override_reason, claim.override_at = (
                principal.user_id,
                body.reason,
                services.clock(),
            )
    await rec.set_version_state(session, principal.org_id, version.id, VersionState.APPROVED)
    await audit(session, principal, "video_version.approve", "video_version", version.id, request=request)
    memory_job = await start_studio_job(  # §18.4: persona facts and stances of the script → proposed
        session,
        services,
        background,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kind=JobKind.MEMORY_UPDATE,
        target_type="video_version",
        target_id=version.id,
        args={"trigger": "approved"},
    )
    memory_job.video_version_id = version.id
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.GENERATE,
        target_type="video_version",
        target_id=version.id,
        requested_by=principal.user_id,
        input={"source": "approve", "overrides": body.overrides},
        video_version_id=version.id,
    )
    accepted = GenerationAccepted(job_id=job.id, version_id=version.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    arg = _build_arg(services, principal.org_id, job.id, version.id)
    background.add_task(start_job, services, principal.org_id, job.id, JobKind.GENERATE, arg)
    return accepted
