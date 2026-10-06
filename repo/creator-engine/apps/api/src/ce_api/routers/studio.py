"""Creator Studio and World Studio jobs (Phase 10, §30): identity packs, wardrobe references, voice
design and the test bench, the Creator Test with ratings and baselines, world plates, and the world
continuity report.

Every action that needs a model is a studio job (`202 {job_id}`; `ce_exec.studio`); the API only
validates, records choices and reviews on drafts (I3), and reads results.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_core.identity.creator import DefaultProsody, LexiconEntry
from ce_db.models.assets import Artifact, Asset, GenerationJob
from ce_db.models.creators import (
    AppearanceVersion,
    ConsistencyReport,
    Creator,
    CreatorBaseline,
    CreatorTest,
    CreatorVersion,
    Voice,
    VoiceCandidate,
    VoiceVersion,
    WardrobeVersion,
)
from ce_db.models.videos import Video, VideoVersion
from ce_db.models.worlds import World, WorldVersion
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import Field

from ce_api.common import audit
from ce_api.deps import Approver, DbSession, Reader, ServicesDep, Writer
from ce_api.jobs import start_studio_job
from ce_api.references import asset_issues
from ce_api.schemas import Body, Out, examples
from ce_api.security.rbac import Permission
from ce_api.versioning import get_scoped, lock_scoped, next_number, require_draft

router = APIRouter(tags=["studio"])

Rating = Annotated[int, Field(ge=1, le=5)]


class JobAccepted(Out):
    job_id: UUID
    status: str = "queued"


# ====================================================================== identity pack (§17.2)
class IdentityGenerateBody(Body):
    model_config = examples([{"candidates": 12}])
    candidates: int = Field(default=12, ge=1, le=16)


class IdentityChooseBody(Body):
    model_config = examples([{"asset_id": "0192f0a0-0000-7000-8000-000000000001"}])
    asset_id: UUID


class IdentityReviewBody(Body):
    model_config = examples(
        [
            {
                "approve": ["0192f0a0-0000-7000-8000-000000000001"],
                "reject": [],
                "age_review": None,
            }
        ]
    )
    approve: list[UUID] = Field(default_factory=list)
    reject: list[UUID] = Field(default_factory=list)
    age_review: Literal["approved", "rejected"] | None = Field(
        default=None, description="the review of a low VLM age estimate (approvers only)"
    )


class IdentityPackOut(Out):
    appearance_version_id: UUID
    status: str
    canonical_face_asset_id: UUID | None
    identity_pack: dict[str, Any]
    age_checks: dict[str, Any]


def _pack_out(row: AppearanceVersion) -> IdentityPackOut:
    return IdentityPackOut(
        appearance_version_id=row.id,
        status=row.status,
        canonical_face_asset_id=row.canonical_face_asset_id,
        identity_pack=dict(row.identity_pack or {}),
        age_checks=dict(row.age_checks or {}),
    )


@router.post(
    "/v1/appearance-versions/{appearance_version_id}/identity-pack:generate",
    response_model=JobAccepted,
    status_code=202,
)
async def generate_identity_candidates(
    appearance_version_id: UUID,
    body: IdentityGenerateBody,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    """`BuildIdentityPackWorkflow` (candidates): 8–16 face candidates from the Appearance DNA."""
    row = await lock_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    require_draft(row, "appearance version")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.IDENTITY_PACK, target_type="appearance_version", target_id=row.id,
        args={"mode": "candidates", "candidates": body.candidates},
    )  # fmt: skip
    return JobAccepted(job_id=job.id)


@router.post(
    "/v1/appearance-versions/{appearance_version_id}/identity-pack:choose", response_model=JobAccepted, status_code=202
)
async def choose_canonical_face(
    appearance_version_id: UUID,
    body: IdentityChooseBody,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    """Records the canonical face and starts the expansion (angles, expressions, identity scores)
    and the VLM apparent-age check. Earlier expansions and age checks are discarded: they are
    relative to the canonical face."""
    row = await lock_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    require_draft(row, "appearance version")
    if issues := await asset_issues(session, principal.org_id, [body.asset_id], path="/asset_id", family="image"):
        raise InvalidInputError("invalid canonical face", issues=issues)
    pack = dict(row.identity_pack or {})
    pack.pop("images", None)
    pack["canonical_asset_id"] = str(body.asset_id)
    row.identity_pack = pack
    row.canonical_face_asset_id = body.asset_id
    row.age_checks = {}
    await session.flush()
    await audit(
        session, principal, "appearance_version.choose_face", "appearance_version", row.id, request=request,
        after={"canonical_face_asset_id": str(body.asset_id)},
    )  # fmt: skip
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.IDENTITY_PACK, target_type="appearance_version", target_id=row.id, args={"mode": "expand"},
    )  # fmt: skip
    return JobAccepted(job_id=job.id)


@router.post("/v1/appearance-versions/{appearance_version_id}/identity-pack:review", response_model=IdentityPackOut)
async def review_identity_pack(
    appearance_version_id: UUID,
    body: IdentityReviewBody,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
) -> IdentityPackOut:
    """Approves or rejects expansion images and, for approvers, records the review of a low VLM age
    estimate (§17.3; the owner's adult attestation is part of the creator version's approval)."""
    row = await lock_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    require_draft(row, "appearance version")
    pack = dict(row.identity_pack or {})
    images = [dict(i) for i in pack.get("images", [])]
    known = {str(i.get("asset_id")) for i in images}
    unknown = [str(a) for a in [*body.approve, *body.reject] if str(a) not in known]
    if unknown or set(body.approve) & set(body.reject):
        raise InvalidInputError(
            "review only the identity pack's images, each once",
            issues=[Issue("asset_id", f"not in the identity pack: {a}", path="/approve") for a in unknown],
        )
    decisions = {str(a): "approved" for a in body.approve} | {str(a): "rejected" for a in body.reject}
    for image in images:
        if str(image.get("asset_id")) in decisions:
            image["decision"] = decisions[str(image["asset_id"])]
    pack["images"] = images
    row.identity_pack = pack
    checks = dict(row.age_checks or {})
    now = services.clock()
    if body.age_review is not None:
        if not principal.allows(Permission.APPROVE_IDENTITY, "POST"):
            raise ConflictError("an age review needs an approver role", issues=[Issue("age_review", "approver only")])
        checks["review"] = {"decision": body.age_review, "user_id": str(principal.user_id), "at": now.isoformat()}
    row.age_checks = checks
    await session.flush()
    await audit(
        session, principal, "appearance_version.review_identity_pack", "appearance_version", row.id, request=request,
        after={"decisions": decisions, "age_review": body.age_review},
    )  # fmt: skip
    await session.refresh(row)
    return _pack_out(row)


@router.get("/v1/appearance-versions/{appearance_version_id}/identity-pack", response_model=IdentityPackOut)
async def get_identity_pack(appearance_version_id: UUID, principal: Reader, session: DbSession) -> IdentityPackOut:
    return _pack_out(
        await get_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    )


# ====================================================================== wardrobe references
@router.post(
    "/v1/wardrobe-versions/{wardrobe_version_id}/references:generate", response_model=JobAccepted, status_code=202
)
async def generate_wardrobe_references(
    wardrobe_version_id: UUID,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    """`BuildWardrobeWorkflow`: reference images conditioned on the creator's canonical face, scored."""
    row = await lock_scoped(session, WardrobeVersion, principal.ctx, wardrobe_version_id, "wardrobe version")
    require_draft(row, "wardrobe version")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.WARDROBE_REFS, target_type="wardrobe_version", target_id=row.id,
    )  # fmt: skip
    return JobAccepted(job_id=job.id)


# ====================================================================== voices (§21)
class VoiceCreate(Body):
    model_config = examples(
        [
            {
                "name": "Alex (warm)",
                "kind": "designed",
                "description": "warm, mid-pitched adult voice with a light rasp",
                "language": "en-US",
                "creator_id": None,
                "count": 4,
            }
        ]
    )
    name: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Literal["designed"] = "designed"
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    language: str = Field(min_length=2, max_length=35)
    creator_id: UUID | None = None
    sample_text: str | None = Field(default=None, max_length=1000)
    count: int = Field(default=4, ge=1, le=8)


class VoiceAccepted(Out):
    voice_id: UUID
    job_id: UUID
    status: str = "queued"


class VoiceCandidateOut(Out):
    id: UUID
    voice_id: UUID
    job_id: UUID | None
    artifact_id: UUID | None
    asset_id: UUID | None = None
    description: str
    engine: str
    selected: bool
    created_at: datetime


class VoiceVersionDetail(Out):
    id: UUID
    voice_id: UUID
    number: int
    status: str
    description: str
    references: list[Any]
    wpm: dict[str, Any]
    lexicon: list[Any]
    default_prosody: dict[str, Any]
    created_at: datetime


class VoiceVersionPatch(Body):
    model_config = examples(
        [{"description": "warm and steady", "lexicon": [{"term": "Kubernetes", "respelling": "koo-ber-NET-eez"}]}]
    )
    description: str | None = Field(default=None, max_length=2000)
    lexicon: list[LexiconEntry] | None = None
    default_prosody: DefaultProsody | None = None


class VoiceTestBody(Body):
    model_config = examples([{"text": "Here is the thing [laughs] it actually works.", "language": "en-US"}])
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    engine_hint: str | None = None


@router.post("/v1/voices", response_model=VoiceAccepted, status_code=202)
async def design_voice(
    body: VoiceCreate,
    principal: Writer,
    request: Request,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> VoiceAccepted:
    """A designed voice (`VoiceDesignWorkflow`): candidates from the description; select one to
    create a draft voice version. Cloned voices need consent and stay off until V1 (§21, §32)."""
    if body.creator_id is not None:
        await get_scoped(session, Creator, principal.ctx, body.creator_id, "creator")
    voice = Voice(org_id=principal.org_id, creator_id=body.creator_id, name=body.name, kind=body.kind)
    session.add(voice)
    await session.flush()
    await audit(session, principal, "voice.create", "voice", voice.id, request=request, after={"name": body.name})
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.VOICE_DESIGN, target_type="voice", target_id=voice.id,
        args={"description": body.description, "language": body.language, "count": body.count,
              "sample_text": body.sample_text},
    )  # fmt: skip
    return VoiceAccepted(voice_id=voice.id, job_id=job.id)


async def _candidate_assets(session: DbSession, org_id: UUID, rows: list[VoiceCandidate]) -> dict[UUID, UUID]:
    """Candidate → its asset (the design job stores candidates as audio assets)."""
    ids = [r.artifact_id for r in rows if r.artifact_id]
    if not ids:
        return {}
    found = (
        await session.execute(
            sa.select(Artifact.id, Asset.id)
            .join(Asset, sa.and_(Asset.org_id == Artifact.org_id, Asset.sha256 == Artifact.sha256))
            .where(Artifact.org_id == org_id, Artifact.id.in_(ids), Asset.status == "ready")
        )
    ).all()
    return dict(found)


@router.get("/v1/voices/{voice_id}/candidates", response_model=list[VoiceCandidateOut])
async def list_voice_candidates(voice_id: UUID, principal: Reader, session: DbSession) -> list[VoiceCandidateOut]:
    voice = await get_scoped(session, Voice, principal.ctx, voice_id, "voice")
    rows = list(
        (
            await session.execute(
                sa.select(VoiceCandidate).where(VoiceCandidate.voice_id == voice.id).order_by(VoiceCandidate.created_at)
            )
        ).scalars()
    )
    assets = await _candidate_assets(session, principal.org_id, rows)
    return [
        VoiceCandidateOut.model_validate(r).model_copy(update={"asset_id": assets.get(r.artifact_id)})  # type: ignore[arg-type]
        for r in rows
    ]


@router.post(
    "/v1/voices/{voice_id}/candidates/{candidate_id}:select", response_model=VoiceVersionDetail, status_code=201
)
async def select_voice_candidate(
    voice_id: UUID, candidate_id: UUID, principal: Writer, request: Request, session: DbSession
) -> VoiceVersionDetail:
    voice = await lock_scoped(session, Voice, principal.ctx, voice_id, "voice")
    candidate = await get_scoped(session, VoiceCandidate, principal.ctx, candidate_id, "voice candidate")
    if candidate.voice_id != voice.id:
        raise NotFoundError("voice candidate not found for this voice")
    asset_id = (await _candidate_assets(session, principal.org_id, [candidate])).get(candidate.artifact_id)
    if asset_id is None:
        raise ConflictError("the candidate's audio is not available as an asset")
    job = await session.get(GenerationJob, candidate.job_id) if candidate.job_id else None
    inputs = dict(job.input or {}) if job else {}
    candidate.selected = True
    row = VoiceVersion(
        org_id=principal.org_id,
        voice_id=voice.id,
        number=await next_number(session, VoiceVersion, VoiceVersion.voice_id, voice.id),
        parent_version_id=voice.current_version_id,
        description=candidate.description,
        references=[
            {
                "asset_id": str(asset_id),
                "language": str(inputs.get("language", "en")),
                "transcript": str(inputs.get("sample_text") or ""),
            }
        ],
        status="draft",
    )
    session.add(row)
    await session.flush()
    await audit(
        session, principal, "voice.select_candidate", "voice", voice.id, request=request,
        after={"candidate_id": str(candidate.id), "voice_version_id": str(row.id)},
    )  # fmt: skip
    await session.refresh(row)
    return VoiceVersionDetail.model_validate(row)


@router.get("/v1/voice-versions/{voice_version_id}/detail", response_model=VoiceVersionDetail)
async def get_voice_version_detail(voice_version_id: UUID, principal: Reader, session: DbSession) -> VoiceVersionDetail:
    return VoiceVersionDetail.model_validate(
        await get_scoped(session, VoiceVersion, principal.ctx, voice_version_id, "voice version")
    )


@router.patch("/v1/voice-versions/{voice_version_id}", response_model=VoiceVersionDetail)
async def patch_voice_version(
    voice_version_id: UUID, body: VoiceVersionPatch, principal: Writer, session: DbSession
) -> VoiceVersionDetail:
    """Drafts only: description, lexicon (the pronunciation editor) and prosody defaults."""
    row = await lock_scoped(session, VoiceVersion, principal.ctx, voice_version_id, "voice version")
    require_draft(row, "voice version")
    if body.description is not None:
        row.description = body.description
    if body.lexicon is not None:
        row.lexicon = [e.model_dump(mode="json", exclude_none=True) for e in body.lexicon]
    if body.default_prosody is not None:
        row.default_prosody = body.default_prosody.model_dump(mode="json")
    await session.flush()
    await session.refresh(row)
    return VoiceVersionDetail.model_validate(row)


@router.post("/v1/voice-versions/{voice_version_id}:test", response_model=JobAccepted, status_code=202)
async def test_voice_version(
    voice_version_id: UUID,
    body: VoiceTestBody,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    """The test bench (`VoiceTestWorkflow`): `{audio_artifact_id, wer, speech_quality, wpm,
    speaker_similarity}` in the job result. A draft records the WPM a real engine measured."""
    row = await get_scoped(session, VoiceVersion, principal.ctx, voice_version_id, "voice version")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.VOICE_TEST, target_type="voice_version", target_id=row.id,
        args={"text": body.text, "tags": body.tags, "language": body.language, "engine_hint": body.engine_hint},
    )  # fmt: skip
    return JobAccepted(job_id=job.id)


@router.post("/v1/voice-versions/{voice_version_id}:approve", response_model=VoiceVersionDetail)
async def approve_voice_version(
    voice_version_id: UUID, principal: Approver, request: Request, session: DbSession
) -> VoiceVersionDetail:
    row = await lock_scoped(session, VoiceVersion, principal.ctx, voice_version_id, "voice version")
    voice = await lock_scoped(session, Voice, principal.ctx, row.voice_id, "voice")
    require_draft(row, "voice version")
    if voice.kind == "cloned" and voice.consent_id is None:
        raise ConflictError("a cloned voice needs a valid consent (§21, §32)", issues=[Issue("consent_id", "missing")])
    row.status = "approved"
    voice.current_version_id = row.id
    await session.flush()
    await audit(session, principal, "voice_version.approve", "voice_version", row.id, request=request)
    await session.refresh(row)
    return VoiceVersionDetail.model_validate(row)


# ====================================================================== Creator Test (§17.4)
class CreatorTestBody(Body):
    model_config = examples([{"world_version_id": None, "language": "en-US"}])
    world_version_id: UUID | None = None
    language: str = "en-US"


class CreatorTestAccepted(Out):
    creator_test_id: UUID
    job_id: UUID
    status: str = "queued"


class CreatorTestOut(Out):
    id: UUID
    creator_version_id: UUID
    appearance_version_id: UUID | None
    voice_version_id: UUID | None
    world_version_id: UUID | None
    job_id: UUID | None
    render_artifact_id: UUID | None
    scorecard: dict[str, Any]
    coverage_report: dict[str, Any]
    human_rating: int | None
    same_person_rating: int | None
    created_at: datetime


class CreatorTestRating(Body):
    model_config = examples([{"human_rating": 4, "same_person_rating": 5, "accent_rating": 4, "notes": "natural"}])
    human_rating: Rating | None = None
    same_person_rating: Rating | None = None
    accent_rating: Rating | None = None
    notes: str = Field(default="", max_length=2000)


class BaselineOut(Out):
    id: UUID
    creator_version_id: UUID
    source: str
    stats: dict[str, Any]
    window_n: int
    created_at: datetime


class ConsistencyOut(Out):
    id: UUID
    creator_version_id: UUID
    version_id: UUID
    metrics: dict[str, Any]
    verdict: str
    deviations: list[Any]
    created_at: datetime


@router.post("/v1/creator-versions/{creator_version_id}/tests", response_model=CreatorTestAccepted, status_code=202)
async def run_creator_test(
    creator_version_id: UUID,
    body: CreatorTestBody,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> CreatorTestAccepted:
    """`CreatorTestWorkflow`: the fixed test video in the creator's default world (or `world_version_id`),
    then the scorecard and a baseline. Drafts can be tested; the test records the tested versions."""
    version = await get_scoped(session, CreatorVersion, principal.ctx, creator_version_id, "creator version")
    if body.world_version_id is not None:
        world_version = await get_scoped(session, WorldVersion, principal.ctx, body.world_version_id, "world version")
        if world_version.status != "approved":
            raise ConflictError("test in an approved world version", issues=[Issue("world_version_id", "draft")])
    test = CreatorTest(
        org_id=principal.org_id,
        creator_version_id=version.id,
        appearance_version_id=version.appearance_version_id,
        voice_version_id=version.voice_version_id,
        world_version_id=body.world_version_id,
        created_by=principal.user_id,
        scorecard={"status": "queued"},
    )
    session.add(test)
    await session.flush()
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.CREATOR_TEST, target_type="creator_test", target_id=test.id, args={"language": body.language},
    )  # fmt: skip
    test.job_id = job.id
    return CreatorTestAccepted(creator_test_id=test.id, job_id=job.id)


@router.get("/v1/creator-tests/{creator_test_id}", response_model=CreatorTestOut)
async def get_creator_test(creator_test_id: UUID, principal: Reader, session: DbSession) -> CreatorTestOut:
    return CreatorTestOut.model_validate(
        await get_scoped(session, CreatorTest, principal.ctx, creator_test_id, "creator test")
    )


@router.get("/v1/creators/{creator_id}/tests", response_model=list[CreatorTestOut])
async def list_creator_tests(creator_id: UUID, principal: Reader, session: DbSession) -> list[CreatorTestOut]:
    creator = await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    rows = (
        await session.execute(
            sa.select(CreatorTest)
            .join(CreatorVersion, CreatorVersion.id == CreatorTest.creator_version_id)
            .where(CreatorTest.org_id == principal.org_id, CreatorVersion.creator_id == creator.id)
            .order_by(CreatorTest.created_at.desc())
            .limit(100)
        )
    ).scalars()
    return [CreatorTestOut.model_validate(r) for r in rows]


@router.post("/v1/creator-tests/{creator_test_id}/ratings", response_model=CreatorTestOut)
async def rate_creator_test(
    creator_test_id: UUID, body: CreatorTestRating, principal: Writer, request: Request, session: DbSession,
    services: ServicesDep,
) -> CreatorTestOut:  # fmt: skip
    """The human ratings of the scorecard: overall (1–5), "same person as the canonical face?" and accent."""
    row = await lock_scoped(session, CreatorTest, principal.ctx, creator_test_id, "creator test")
    card = dict(row.scorecard or {})
    now = services.clock().isoformat()
    rater = {"user_id": str(principal.user_id), "at": now}
    if body.human_rating is not None:
        row.human_rating = body.human_rating
        card["human_rating"] = {"status": "rated", "value": body.human_rating, **rater}
    if body.same_person_rating is not None:
        row.same_person_rating = body.same_person_rating
        card["same_person_rating"] = {"status": "rated", "value": body.same_person_rating, **rater}
    if body.accent_rating is not None:
        card["accent"] = {"status": "rated", "value": body.accent_rating, "method": "human rating", **rater}
    if body.notes:
        card["rating_notes"] = body.notes
    row.scorecard = card
    await session.flush()
    await audit(
        session, principal, "creator_test.rate", "creator_test", row.id, request=request,
        after=body.model_dump(mode="json"),
    )  # fmt: skip
    await session.refresh(row)
    return CreatorTestOut.model_validate(row)


@router.get("/v1/creators/{creator_id}/baselines", response_model=list[BaselineOut])
async def list_baselines(creator_id: UUID, principal: Reader, session: DbSession) -> list[BaselineOut]:
    creator = await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    rows = (
        await session.execute(
            sa.select(CreatorBaseline)
            .join(CreatorVersion, CreatorVersion.id == CreatorBaseline.creator_version_id)
            .where(CreatorBaseline.org_id == principal.org_id, CreatorVersion.creator_id == creator.id)
            .order_by(CreatorBaseline.created_at.desc())
            .limit(100)
        )
    ).scalars()
    return [BaselineOut.model_validate(r) for r in rows]


@router.get("/v1/creators/{creator_id}/consistency", response_model=list[ConsistencyOut])
async def list_consistency(
    creator_id: UUID, principal: Reader, session: DbSession, since: datetime | None = None
) -> list[ConsistencyOut]:
    """Consistency reports across videos (§20); `ConsistencyWorkflow` writes them from Phase 11."""
    creator = await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    query = (
        sa.select(ConsistencyReport)
        .where(ConsistencyReport.org_id == principal.org_id, ConsistencyReport.creator_id == creator.id)
        .order_by(ConsistencyReport.created_at.desc())
        .limit(200)
    )
    if since is not None:
        query = query.where(ConsistencyReport.created_at >= since)
    return [ConsistencyOut.model_validate(r) for r in (await session.execute(query)).scalars()]


# ====================================================================== world plates (§19.2)
class PlatesGenerateBody(Body):
    model_config = examples([{"camera_position_keys": ["cam_desk_front"], "times_of_day": ["late_afternoon"]}])
    camera_position_keys: list[str] = Field(default_factory=list)
    times_of_day: list[str] = Field(default_factory=list)
    weather: list[str] = Field(default_factory=list)
    candidates: int | None = Field(default=None, ge=1, le=8)


@router.post("/v1/world-versions/{world_version_id}/plates:generate", response_model=JobAccepted, status_code=202)
async def generate_plates(
    world_version_id: UUID,
    body: PlatesGenerateBody,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> JobAccepted:
    """`BuildWorldPlatesWorkflow`: candidate plates (no people) per permitted position × time × weather
    (defaults: every permitted position at the default time and weather)."""
    row = await lock_scoped(session, WorldVersion, principal.ctx, world_version_id, "world version")
    require_draft(row, "world version")
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.WORLD_PLATES, target_type="world_version", target_id=row.id,
        args={"mode": "generate", **body.model_dump(mode="json", exclude_none=True)},
    )  # fmt: skip
    return JobAccepted(job_id=job.id)


class ContinuityUse(Out):
    video_id: UUID
    video_title: str
    version_id: UUID
    version_number: int
    version_state: str
    scene_key: str
    world_version_id: UUID
    camera_position_key: str | None
    time_of_day: str | None
    weather: str | None
    has_overrides: bool


class ContinuityOut(Out):
    world_id: UUID
    uses: list[ContinuityUse]
    note: str = Field(
        default="World continuity QC scores (§19.6) are reported per shot in the versions' QC reports; "
        "cross-video scores and their calibration arrive with Phase 11."
    )


@router.get("/v1/worlds/{world_id}/continuity", response_model=ContinuityOut)
async def world_continuity(world_id: UUID, principal: Reader, session: DbSession) -> ContinuityOut:
    """Where the world is used: every scene of every video version bound to one of its versions."""
    world = await get_scoped(session, World, principal.ctx, world_id, "world")
    version_ids = {
        str(v)
        for v in (await session.execute(sa.select(WorldVersion.id).where(WorldVersion.world_id == world.id))).scalars()
    }
    rows = (
        await session.execute(
            sa.select(VideoVersion, Video.title)
            .join(Video, Video.id == VideoVersion.video_id)
            .where(VideoVersion.org_id == principal.org_id)
            .order_by(VideoVersion.created_at.desc())
            .limit(500)
        )
    ).all()
    uses = []
    for version, title in rows:
        for scene in dict(version.spec or {}).get("scenes", []):
            binding = dict(scene.get("world") or {})
            if str(binding.get("world_version_id")) not in version_ids:
                continue
            overrides = dict(binding.get("overrides") or {})
            uses.append(
                ContinuityUse(
                    video_id=version.video_id,
                    video_title=title,
                    version_id=version.id,
                    version_number=version.number,
                    version_state=version.state,
                    scene_key=str(scene.get("key")),
                    world_version_id=UUID(str(binding["world_version_id"])),
                    camera_position_key=binding.get("camera_position_key"),
                    time_of_day=binding.get("time_of_day"),
                    weather=binding.get("weather"),
                    has_overrides=any(bool(v) for v in overrides.values()),
                )
            )
    return ContinuityOut(world_id=world.id, uses=uses)


# ====================================================================== artifacts (studio playback)
class ArtifactLink(Out):
    artifact_id: UUID
    kind: str
    mime: str
    url: str
    expires_at: datetime


@router.get("/v1/artifacts/{artifact_id}/download", response_model=ArtifactLink)
async def download_artifact(
    artifact_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> ArtifactLink:
    """A presigned GET of an org artifact (the voice test bench's audio, a test video's render)."""
    row = await get_scoped(session, Artifact, principal.ctx, artifact_id, "artifact")
    ttl = services.config.storage.presign_ttl_s
    request = await services.storage.presign_get(services.settings.s3_bucket_artifacts, row.storage_key, ttl_s=ttl)
    from datetime import timedelta

    return ArtifactLink(
        artifact_id=row.id, kind=row.kind, mime=row.mime, url=request.url,
        expires_at=services.clock() + timedelta(seconds=ttl),
    )  # fmt: skip


class AppearanceSummary(Out):
    id: UUID
    name: str
    current_version_id: UUID | None
    versions: list[dict[str, Any]]


@router.get("/v1/creators/{creator_id}/appearances", response_model=list[AppearanceSummary])
async def list_appearances(creator_id: UUID, principal: Reader, session: DbSession) -> list[AppearanceSummary]:
    """A creator's appearances with their versions (the Appearance tab)."""
    from ce_db.models.creators import Appearance

    creator = await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    rows = (
        await session.execute(
            sa.select(Appearance).where(Appearance.org_id == principal.org_id, Appearance.creator_id == creator.id)
        )
    ).scalars()
    out = []
    for appearance in rows:
        versions = (
            await session.execute(
                sa.select(AppearanceVersion.id, AppearanceVersion.number, AppearanceVersion.status)
                .where(AppearanceVersion.appearance_id == appearance.id)
                .order_by(AppearanceVersion.number)
            )
        ).all()
        out.append(
            AppearanceSummary(
                id=appearance.id,
                name=appearance.name,
                current_version_id=appearance.current_version_id,
                versions=[{"id": str(v.id), "number": v.number, "status": v.status} for v in versions],
            )
        )
    return out
