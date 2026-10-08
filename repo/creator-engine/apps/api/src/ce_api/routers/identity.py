"""Creators, appearances, wardrobes and voices (§30, §17). Drafts are editable; approved versions are
immutable (I3). `:approve` only verifies recorded results (§10.2; Phase 1 scope): the checks that
produce them run in Creator Studio workflows (Phase 10; real engines in Phases 7–9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import InvalidInputError, Issue, NotFoundError, PolicyDeniedError
from ce_core.identity.creator import AppearanceDNA, CreatorDNA, WardrobeSpec, validate_creator_dna
from ce_db.models.assets import Asset
from ce_db.models.creators import (
    Appearance,
    AppearanceVersion,
    Creator,
    CreatorVersion,
    Voice,
    VoiceVersion,
    Wardrobe,
    WardrobeVersion,
)
from ce_db.models.worlds import World, WorldVersion
from fastapi import APIRouter, Query, Request
from pydantic import Field

from ce_api.common import Page, audit, page
from ce_api.deps import Approver, DbSession, Reader, ServicesDep, Writer
from ce_api.errors import ApprovalBlockedError
from ce_api.events import EventType
from ce_api.onboarding import FaceAttestation, attestation_missing, check_uploaded_reference
from ce_api.references import asset_issues, version_issues
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped, next_number, require_draft

router = APIRouter(tags=["creators"])

Name = Annotated[str, Field(min_length=1, max_length=200)]
MIN_VLM_AGE_ESTIMATE = 25  # §17.3: an estimate below 25 blocks approval pending review


def _size(services: ServicesDep, limit: int | None) -> int:
    return min(limit or services.config.api.default_page_size, services.config.api.max_page_size)


class VersionRef(Out):
    id: UUID
    number: int
    status: str
    created_at: datetime


def _dna_issues(dna: CreatorDNA, services: ServicesDep) -> list[Issue]:
    return [i for i in validate_creator_dna(dna, services.vocab) if i.severity == "error"]


# ====================================================================== creators
class CreatorDefaults(Body):
    world_ids: list[UUID] = Field(default_factory=list)
    wardrobe_version_ids: list[UUID] = Field(default_factory=list)


class CreatorCreate(Body):
    name: Name
    kind: Literal["synthetic", "digital_twin"] = "synthetic"
    dna: CreatorDNA

    model_config = examples(
        [
            {
                "name": "Alex",
                "kind": "synthetic",
                "dna": {"vocab_version": "2026.10.1", "identity": {"display_name": "Alex", "canon": []}},
            }
        ]
    )


class CreatorVersionCreate(Body):
    from_version_id: UUID | None = None
    dna: CreatorDNA | None = None
    appearance_version_id: UUID | None = None
    voice_version_id: UUID | None = None
    defaults: CreatorDefaults | None = None

    model_config = examples([{"from_version_id": "0192f0a0-0000-7000-8000-000000000011"}])


class CreatorVersionPatch(Body):
    dna: CreatorDNA | None = None
    appearance_version_id: UUID | None = None
    voice_version_id: UUID | None = None
    defaults: CreatorDefaults | None = None

    model_config = examples([{"voice_version_id": "0192f0a0-0000-7000-8000-000000000041"}])


class CreatorApprove(Body):
    attest_adult_presentation: bool = Field(
        description="the owner attests that this synthetic creator presents as an adult (§17.3); logged"
    )

    model_config = examples([{"attest_adult_presentation": True}])


class CreatorVersionOut(Out):
    id: UUID
    creator_id: UUID
    number: int
    parent_version_id: UUID | None
    status: str
    dna: dict[str, Any]
    appearance_version_id: UUID | None
    voice_version_id: UUID | None
    default_world_ids: list[UUID]
    default_wardrobe_version_ids: list[UUID]
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CreatorOut(Out):
    id: UUID
    name: str
    kind: str
    status: str
    current_version_id: UUID | None
    created_at: datetime


class CreatorDetail(CreatorOut):
    versions: list[VersionRef]
    current_version: CreatorVersionOut | None


async def _creator_detail(session: DbSession, creator: Creator) -> CreatorDetail:
    versions = (
        (
            await session.execute(
                sa.select(CreatorVersion).where(CreatorVersion.creator_id == creator.id).order_by(CreatorVersion.number)
            )
        )
        .scalars()
        .all()
    )
    current = next((v for v in versions if v.id == creator.current_version_id), None)
    return CreatorDetail(
        **CreatorOut.model_validate(creator).model_dump(),
        versions=[VersionRef.model_validate(v) for v in versions],
        current_version=CreatorVersionOut.model_validate(current) if current else None,
    )


async def _check_creator_refs(
    session: DbSession,
    org_id: UUID,
    creator_id: UUID,
    appearance_version_id: UUID | None,
    voice_version_id: UUID | None,
    defaults: CreatorDefaults | None,
) -> None:
    """References must exist in the org and belong to this creator (drafts may reference drafts)."""
    issues: list[Issue] = []
    if appearance_version_id is not None:
        owner = (
            await session.execute(
                sa.select(Appearance.creator_id)
                .join(AppearanceVersion, AppearanceVersion.appearance_id == Appearance.id)
                .where(AppearanceVersion.org_id == org_id, AppearanceVersion.id == appearance_version_id)
            )
        ).scalar_one_or_none()
        if owner != creator_id:
            issues.append(
                Issue("reference", "appearance version not found for this creator", path="/appearance_version_id")
            )
    if voice_version_id is not None:
        row = (
            await session.execute(
                sa.select(Voice.creator_id)
                .join(VoiceVersion, VoiceVersion.voice_id == Voice.id)
                .where(VoiceVersion.org_id == org_id, VoiceVersion.id == voice_version_id)
            )
        ).first()
        if row is None or row[0] not in (creator_id, None):
            issues.append(Issue("reference", "voice version not found for this creator", path="/voice_version_id"))
    if defaults is not None:
        if defaults.world_ids:
            found = set(
                (
                    await session.execute(
                        sa.select(World.id).where(World.org_id == org_id, World.id.in_(defaults.world_ids))
                    )
                ).scalars()
            )
            issues += [
                Issue("reference", "world not found", path="/defaults/world_ids", detail={"id": str(w)})
                for w in defaults.world_ids
                if w not in found
            ]
        if defaults.wardrobe_version_ids:
            owned = set(
                (
                    await session.execute(
                        sa.select(WardrobeVersion.id)
                        .join(Wardrobe, Wardrobe.id == WardrobeVersion.wardrobe_id)
                        .where(
                            WardrobeVersion.org_id == org_id,
                            WardrobeVersion.id.in_(defaults.wardrobe_version_ids),
                            Wardrobe.creator_id == creator_id,
                        )
                    )
                ).scalars()
            )
            issues += [
                Issue(
                    "reference",
                    "wardrobe version not found for this creator",
                    path="/defaults/wardrobe_version_ids",
                    detail={"id": str(w)},
                )
                for w in defaults.wardrobe_version_ids
                if w not in owned
            ]
    if issues:
        raise InvalidInputError("invalid references", issues=issues)


@router.post("/v1/creators", response_model=CreatorDetail, status_code=201)
async def create_creator(
    body: CreatorCreate, principal: Writer, session: DbSession, services: ServicesDep
) -> CreatorDetail:
    if body.kind == "digital_twin" and not services.config.features.digital_twins_enabled:
        raise PolicyDeniedError("digital twins are disabled until V1 (digital_twins_enabled=false, §17.3)")
    if issues := _dna_issues(body.dna, services):
        raise InvalidInputError("the creator DNA is invalid", issues=issues)
    creator = Creator(org_id=principal.org_id, name=body.name, kind=body.kind)
    session.add(creator)
    await session.flush()
    session.add(
        CreatorVersion(
            org_id=principal.org_id,
            creator_id=creator.id,
            number=1,
            dna=body.dna.model_dump(mode="json"),
            created_by=principal.user_id,
        )
    )
    await session.flush()
    await session.refresh(creator)
    return await _creator_detail(session, creator)


@router.get("/v1/creators", response_model=Page[CreatorOut])
async def list_creators(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[CreatorOut]:
    query = sa.select(Creator).where(Creator.org_id == principal.org_id)
    rows, next_cursor = await page(session, query, Creator.id, cursor=cursor, limit=_size(services, limit))
    return Page[CreatorOut](items=[CreatorOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/creators/{creator_id}", response_model=CreatorDetail)
async def get_creator(creator_id: UUID, principal: Reader, session: DbSession) -> CreatorDetail:
    return await _creator_detail(session, await get_scoped(session, Creator, principal.ctx, creator_id, "creator"))


@router.post("/v1/creators/{creator_id}/versions", response_model=CreatorVersionOut, status_code=201)
async def create_creator_version(
    creator_id: UUID, body: CreatorVersionCreate, principal: Writer, session: DbSession, services: ServicesDep
) -> CreatorVersionOut:
    creator = await lock_scoped(session, Creator, principal.ctx, creator_id, "creator")
    base_id = body.from_version_id or creator.current_version_id
    base: CreatorVersion | None = None
    if base_id is not None:
        base = await get_scoped(session, CreatorVersion, principal.ctx, base_id, "creator version")
        if base.creator_id != creator.id:
            raise NotFoundError("creator version not found for this creator")
    if body.dna is None and base is None:
        raise InvalidInputError("the first version needs dna", issues=[Issue("dna", "required", path="/dna")])
    dna = body.dna.model_dump(mode="json") if body.dna is not None else dict(base.dna)  # type: ignore[union-attr]
    if issues := _dna_issues(CreatorDNA.model_validate(dna), services):
        raise InvalidInputError("the creator DNA is invalid", issues=issues)
    appearance = body.appearance_version_id or (base.appearance_version_id if base else None)
    voice = body.voice_version_id or (base.voice_version_id if base else None)
    defaults = body.defaults or CreatorDefaults(
        world_ids=list(base.default_world_ids) if base else [],
        wardrobe_version_ids=list(base.default_wardrobe_version_ids) if base else [],
    )
    await _check_creator_refs(session, principal.org_id, creator.id, appearance, voice, defaults)
    row = CreatorVersion(
        org_id=principal.org_id,
        creator_id=creator.id,
        number=await next_number(session, CreatorVersion, CreatorVersion.creator_id, creator.id),
        parent_version_id=base.id if base else None,
        dna=dna,
        appearance_version_id=appearance,
        voice_version_id=voice,
        default_world_ids=defaults.world_ids,
        default_wardrobe_version_ids=defaults.wardrobe_version_ids,
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return CreatorVersionOut.model_validate(row)


@router.get("/v1/creator-versions/{creator_version_id}", response_model=CreatorVersionOut)
async def get_creator_version(creator_version_id: UUID, principal: Reader, session: DbSession) -> CreatorVersionOut:
    return CreatorVersionOut.model_validate(
        await get_scoped(session, CreatorVersion, principal.ctx, creator_version_id, "creator version")
    )


@router.patch("/v1/creator-versions/{creator_version_id}", response_model=CreatorVersionOut)
async def patch_creator_version(
    creator_version_id: UUID, body: CreatorVersionPatch, principal: Writer, session: DbSession, services: ServicesDep
) -> CreatorVersionOut:
    row = await lock_scoped(session, CreatorVersion, principal.ctx, creator_version_id, "creator version")
    require_draft(row, "creator version")
    values = body.model_dump(exclude_unset=True)
    if body.dna is not None:
        if issues := _dna_issues(body.dna, services):
            raise InvalidInputError("the creator DNA is invalid", issues=issues)
        row.dna = body.dna.model_dump(mode="json")
    appearance = body.appearance_version_id if "appearance_version_id" in values else row.appearance_version_id
    voice = body.voice_version_id if "voice_version_id" in values else row.voice_version_id
    await _check_creator_refs(session, principal.org_id, row.creator_id, appearance, voice, body.defaults)
    row.appearance_version_id = appearance
    row.voice_version_id = voice
    if body.defaults is not None:
        row.default_world_ids = body.defaults.world_ids
        row.default_wardrobe_version_ids = body.defaults.wardrobe_version_ids
    await session.flush()
    await session.refresh(row)
    return CreatorVersionOut.model_validate(row)


async def creator_approval_issues(
    session: DbSession, services: ServicesDep, org_id: UUID, row: CreatorVersion, creator: Creator, attested: bool
) -> list[Issue]:
    issues = _dna_issues(CreatorDNA.model_validate(row.dna), services)
    issues += await version_issues(
        session,
        AppearanceVersion,
        org_id,
        row.appearance_version_id,
        path="/appearance_version_id",
        what="appearance version",
    )
    issues += await version_issues(
        session, VoiceVersion, org_id, row.voice_version_id, path="/voice_version_id", what="voice version"
    )
    for wardrobe_version_id in row.default_wardrobe_version_ids:
        issues += await version_issues(
            session,
            WardrobeVersion,
            org_id,
            wardrobe_version_id,
            path="/default_wardrobe_version_ids",
            what="wardrobe version",
        )
    for world_id in row.default_world_ids:
        approved = (
            await session.execute(
                sa.select(sa.func.count()).where(
                    WorldVersion.org_id == org_id, WorldVersion.world_id == world_id, WorldVersion.status == "approved"
                )
            )
        ).scalar_one()
        if not approved:
            issues.append(
                Issue(
                    "reference_not_approved",
                    "default world has no approved version",
                    path="/default_world_ids",
                    detail={"id": str(world_id)},
                )
            )
    if creator.kind == "synthetic" and not attested:
        issues.append(
            Issue(
                "age_attestation", "the owner must attest adult presentation (§17.3)", path="/attest_adult_presentation"
            )
        )
    if creator.kind == "digital_twin":
        issues.append(Issue("consent", "digital twins need a valid consent (V1)"))
    return issues


@router.post("/v1/creator-versions/{creator_version_id}:approve", response_model=CreatorVersionOut)
async def approve_creator_version(
    creator_version_id: UUID,
    body: CreatorApprove,
    principal: Approver,
    request: Request,
    session: DbSession,
    services: ServicesDep,
) -> CreatorVersionOut:
    row = await lock_scoped(session, CreatorVersion, principal.ctx, creator_version_id, "creator version")
    creator = await lock_scoped(session, Creator, principal.ctx, row.creator_id, "creator")
    require_draft(row, "creator version")
    if issues := await creator_approval_issues(
        session, services, principal.org_id, row, creator, body.attest_adult_presentation
    ):
        raise ApprovalBlockedError("the creator version cannot be approved yet", issues=issues)
    appearance = await session.get_one(AppearanceVersion, row.appearance_version_id)
    row.status = "approved"
    row.approved_at = services.clock()
    creator.current_version_id = row.id
    await session.flush()
    flagged = await flag_dna_conflicts(session, principal.org_id, creator.id, row)
    await audit(
        session,
        principal,
        "creator_version.approve",
        "creator_version",
        row.id,
        request=request,
        after={
            "attest_adult_presentation": body.attest_adult_presentation,
            "appearance_version_id": str(row.appearance_version_id),
            "age_checks": appearance.age_checks,
        },
    )
    if flagged:
        await services.events.publish(
            principal.org_id,
            EventType.MEMORY_CONFLICT,
            {"creator_id": str(creator.id), "memory_item_ids": flagged, "reason": "dna"},
        )
    await session.refresh(row)
    return CreatorVersionOut.model_validate(row)


async def flag_dna_conflicts(session: Any, org_id: UUID, creator_id: UUID, version: CreatorVersion) -> list[str]:
    """§18.7 "DNA vs memory": active items the new DNA contradicts are flagged for review
    (`conflict_state: unresolved`, the reason in `source.dna_review`), never deleted. Resolving them
    is the memory API's `resolve_conflict` (keep both, or forget/supersede the item)."""
    from ce_core.identity.creator import CreatorDNA
    from ce_db.models.assets import Notification
    from ce_db.models.memory import CreatorMemoryItem
    from ce_memory import dna_conflicts
    from ce_memory.store import load_records

    records = await load_records(session, org_id, creator_id)
    found = dna_conflicts(CreatorDNA.model_validate(version.dna), records)
    ids: list[str] = []
    for item_id, reason in found:
        item = await session.get(CreatorMemoryItem, item_id)
        if item is None or item.org_id != org_id:
            continue
        item.conflict_state = "unresolved"
        item.source = {
            **dict(item.source or {}),
            "dna_review": {"creator_version_id": str(version.id), "reason": reason},
        }
        ids.append(str(item_id))
    if ids:
        session.add(
            Notification(
                org_id=org_id,
                kind="memory_conflict",
                payload={"creator_id": str(creator_id), "memory_item_ids": ids, "reason": "dna"},
            )
        )
        await session.flush()
    return ids


# ====================================================================== appearances
class AppearanceCreate(Body):
    name: Name
    dna: AppearanceDNA
    canonical_face_asset_id: UUID | None = None
    face_attestation: FaceAttestation | None = Field(
        default=None,
        description="required with an uploaded face: it is not a photo of a real, identifiable person "
        "(real people need the digital-twin consent path, V1)",
    )

    model_config = examples([{"name": "Default look", "dna": {"age_appearance": 31, "hair": "short brown"}}])


class AppearanceVersionCreate(Body):
    from_version_id: UUID | None = None
    dna: AppearanceDNA | None = None

    model_config = examples([{"dna": {"age_appearance": 31, "hair": "buzz cut"}}])


class AppearanceVersionPatch(Body):
    dna: AppearanceDNA | None = None
    canonical_face_asset_id: UUID | None = None
    face_attestation: FaceAttestation | None = Field(
        default=None, description="required with an uploaded face (see AppearanceCreate)"
    )

    model_config = examples([{"canonical_face_asset_id": "0192f0a0-0000-7000-8000-0000000000c1"}])


class AppearanceVersionOut(Out):
    id: UUID
    appearance_id: UUID
    number: int
    parent_version_id: UUID | None
    status: str
    dna: dict[str, Any]
    canonical_face_asset_id: UUID | None
    identity_pack: dict[str, Any]
    age_checks: dict[str, Any]
    created_at: datetime


class AppearanceOut(Out):
    id: UUID
    creator_id: UUID
    name: str
    current_version_id: UUID | None
    created_at: datetime


class AppearanceDetail(AppearanceOut):
    versions: list[VersionRef]


async def _appearance_detail(session: DbSession, appearance: Appearance) -> AppearanceDetail:
    versions = (
        await session.execute(
            sa.select(AppearanceVersion)
            .where(AppearanceVersion.appearance_id == appearance.id)
            .order_by(AppearanceVersion.number)
        )
    ).scalars()
    return AppearanceDetail(
        **AppearanceOut.model_validate(appearance).model_dump(),
        versions=[VersionRef.model_validate(v) for v in versions],
    )


@router.post("/v1/creators/{creator_id}/appearances", response_model=AppearanceDetail, status_code=201)
async def create_appearance(
    creator_id: UUID, body: AppearanceCreate, principal: Writer, session: DbSession, services: ServicesDep
) -> AppearanceDetail:
    await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    pack: dict[str, Any] = {}
    if body.canonical_face_asset_id is not None:
        pack = await _face_source(session, services, principal, body.canonical_face_asset_id, body.face_attestation)
    appearance = Appearance(org_id=principal.org_id, creator_id=creator_id, name=body.name)
    session.add(appearance)
    await session.flush()
    session.add(
        AppearanceVersion(
            org_id=principal.org_id,
            appearance_id=appearance.id,
            number=1,
            dna=body.dna.model_dump(mode="json"),
            canonical_face_asset_id=body.canonical_face_asset_id,
            identity_pack=pack,
        )
    )
    await session.flush()
    await session.refresh(appearance)
    return await _appearance_detail(session, appearance)


@router.get("/v1/appearances/{appearance_id}", response_model=AppearanceDetail)
async def get_appearance(appearance_id: UUID, principal: Reader, session: DbSession) -> AppearanceDetail:
    return await _appearance_detail(
        session, await get_scoped(session, Appearance, principal.ctx, appearance_id, "appearance")
    )


@router.post("/v1/appearances/{appearance_id}/versions", response_model=AppearanceVersionOut, status_code=201)
async def create_appearance_version(
    appearance_id: UUID, body: AppearanceVersionCreate, principal: Writer, session: DbSession
) -> AppearanceVersionOut:
    appearance = await lock_scoped(session, Appearance, principal.ctx, appearance_id, "appearance")
    base_id = body.from_version_id or appearance.current_version_id
    base = (
        await get_scoped(session, AppearanceVersion, principal.ctx, base_id, "appearance version") if base_id else None
    )
    if base is not None and base.appearance_id != appearance.id:
        raise NotFoundError("appearance version not found for this appearance")
    if body.dna is None and base is None:
        raise InvalidInputError("the first version needs dna", issues=[Issue("dna", "required", path="/dna")])
    row = AppearanceVersion(
        org_id=principal.org_id,
        appearance_id=appearance.id,
        number=await next_number(session, AppearanceVersion, AppearanceVersion.appearance_id, appearance.id),
        parent_version_id=base.id if base else None,
        dna=body.dna.model_dump(mode="json") if body.dna is not None else dict(base.dna),  # type: ignore[union-attr]
        canonical_face_asset_id=base.canonical_face_asset_id if base else None,
        # A changed look needs a new identity pack and new age checks (§17.2): neither is carried over.
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return AppearanceVersionOut.model_validate(row)


@router.get("/v1/appearance-versions/{appearance_version_id}", response_model=AppearanceVersionOut)
async def get_appearance_version(
    appearance_version_id: UUID, principal: Reader, session: DbSession
) -> AppearanceVersionOut:
    return AppearanceVersionOut.model_validate(
        await get_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    )


@router.patch("/v1/appearance-versions/{appearance_version_id}", response_model=AppearanceVersionOut)
async def patch_appearance_version(
    appearance_version_id: UUID,
    body: AppearanceVersionPatch,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
) -> AppearanceVersionOut:
    row = await lock_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    require_draft(row, "appearance version")
    if body.dna is not None:
        row.dna = body.dna.model_dump(mode="json")
    if body.canonical_face_asset_id is not None:
        pack = await _face_source(session, services, principal, body.canonical_face_asset_id, body.face_attestation)
        if body.canonical_face_asset_id != row.canonical_face_asset_id:
            row.canonical_face_asset_id = body.canonical_face_asset_id
            row.identity_pack = pack  # expansions and scores are relative to the canonical face
            row.age_checks = {}
    await session.flush()
    await session.refresh(row)
    return AppearanceVersionOut.model_validate(row)


async def _face_source(
    session: DbSession,
    services: ServicesDep,
    principal: Writer,
    asset_id: UUID,
    attestation: str | None,
) -> dict[str, Any]:
    """Checks a canonical face; an uploaded one needs the attestation and a usable size. Returns the
    identity pack's `face_source` record ({} for a generated or placeholder face)."""
    if issues := await asset_issues(
        session, principal.org_id, [asset_id], path="/canonical_face_asset_id", family="image"
    ):
        raise InvalidInputError("invalid canonical face", issues=issues)
    record = await check_uploaded_reference(
        session, principal.org_id, asset_id, use="face", attestation=attestation,
        limits=services.config.uploads.references, user_id=principal.user_id, at=services.clock(),
        path="/canonical_face_asset_id",
    )  # fmt: skip
    return {"face_source": record} if record else {}


async def appearance_approval_issues(session: DbSession, org_id: UUID, row: AppearanceVersion) -> list[Issue]:
    issues: list[Issue] = []
    AppearanceDNA.model_validate(row.dna)  # age_appearance >= 18 is part of the model (§17.3)
    if row.canonical_face_asset_id is None:
        issues.append(Issue("canonical_face", "choose a canonical face first", path="/canonical_face_asset_id"))
    else:
        issues += await asset_issues(
            session, org_id, [row.canonical_face_asset_id], path="/canonical_face_asset_id", family="image"
        )
        if attestation_missing(await session.get(Asset, row.canonical_face_asset_id), "face"):
            issues.append(
                Issue(
                    "upload_attestation",
                    "the uploaded canonical face has no recorded attestation that it is not a real person",
                    path="/canonical_face_asset_id",
                )
            )
    checks = row.age_checks or {}
    estimate = checks.get("vlm_estimate")
    reviewed = isinstance(checks.get("review"), dict) and checks["review"].get("decision") == "approved"
    if estimate is None and not reviewed:
        issues.append(
            Issue(
                "age_check_missing",
                "no recorded VLM apparent-age estimate (BuildIdentityPackWorkflow)",
                path="/age_checks/vlm_estimate",
            )
        )
    elif estimate is not None and float(estimate) < MIN_VLM_AGE_ESTIMATE and not reviewed:
        issues.append(
            Issue(
                "age_check_review",
                f"apparent-age estimate {estimate} is below {MIN_VLM_AGE_ESTIMATE}; needs review",
                path="/age_checks",
            )
        )
    images = (row.identity_pack or {}).get("images") or []
    if not any(isinstance(i, dict) and i.get("decision") == "approved" for i in images):
        issues.append(Issue("identity_pack", "the identity pack has no approved images", path="/identity_pack/images"))
    for index, image in enumerate(images):
        if isinstance(image, dict) and image.get("decision") == "approved" and image.get("similarity") is None:
            issues.append(
                Issue(
                    "identity_score_missing",
                    "approved image without a recorded identity score",
                    path=f"/identity_pack/images/{index}",
                )
            )
    return issues


@router.post("/v1/appearance-versions/{appearance_version_id}:approve", response_model=AppearanceVersionOut)
async def approve_appearance_version(
    appearance_version_id: UUID, principal: Approver, request: Request, session: DbSession, services: ServicesDep
) -> AppearanceVersionOut:
    row = await lock_scoped(session, AppearanceVersion, principal.ctx, appearance_version_id, "appearance version")
    appearance = await lock_scoped(session, Appearance, principal.ctx, row.appearance_id, "appearance")
    require_draft(row, "appearance version")
    if issues := await appearance_approval_issues(session, principal.org_id, row):
        raise ApprovalBlockedError("the appearance version cannot be approved yet", issues=issues)
    row.status = "approved"
    appearance.current_version_id = row.id
    await session.flush()
    await audit(
        session,
        principal,
        "appearance_version.approve",
        "appearance_version",
        row.id,
        request=request,
        after={"age_checks": row.age_checks},
    )
    await session.refresh(row)
    return AppearanceVersionOut.model_validate(row)


# ====================================================================== wardrobes
class WardrobeCreate(Body):
    name: Name
    spec: WardrobeSpec

    model_config = examples(
        [{"name": "grey hoodie", "spec": {"name": "grey hoodie", "description": "plain grey hoodie"}}]
    )


class WardrobeVersionCreate(Body):
    from_version_id: UUID | None = None
    spec: WardrobeSpec | None = None

    model_config = examples([{"spec": {"name": "grey hoodie", "description": "hood up"}}])


class WardrobeVersionPatch(Body):
    spec: WardrobeSpec

    model_config = examples([{"spec": {"name": "grey hoodie", "description": "sleeves rolled"}}])


class WardrobeVersionOut(Out):
    id: UUID
    wardrobe_id: UUID
    number: int
    parent_version_id: UUID | None
    status: str
    spec: dict[str, Any]
    reference_asset_ids: list[UUID]
    created_at: datetime


class WardrobeOut(Out):
    id: UUID
    creator_id: UUID
    name: str
    current_version_id: UUID | None
    created_at: datetime


class WardrobeDetail(WardrobeOut):
    versions: list[VersionRef]


async def _wardrobe_detail(session: DbSession, wardrobe: Wardrobe) -> WardrobeDetail:
    versions = (
        await session.execute(
            sa.select(WardrobeVersion)
            .where(WardrobeVersion.wardrobe_id == wardrobe.id)
            .order_by(WardrobeVersion.number)
        )
    ).scalars()
    return WardrobeDetail(
        **WardrobeOut.model_validate(wardrobe).model_dump(), versions=[VersionRef.model_validate(v) for v in versions]
    )


async def _check_wardrobe_spec(session: DbSession, org_id: UUID, spec: WardrobeSpec) -> None:
    if issues := await asset_issues(session, org_id, spec.reference_asset_ids, path="/spec/reference_asset_ids"):
        raise InvalidInputError("invalid wardrobe references", issues=issues)


@router.post("/v1/creators/{creator_id}/wardrobes", response_model=WardrobeDetail, status_code=201)
async def create_wardrobe(
    creator_id: UUID, body: WardrobeCreate, principal: Writer, session: DbSession
) -> WardrobeDetail:
    await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    await _check_wardrobe_spec(session, principal.org_id, body.spec)
    wardrobe = Wardrobe(org_id=principal.org_id, creator_id=creator_id, name=body.name)
    session.add(wardrobe)
    await session.flush()
    session.add(
        WardrobeVersion(
            org_id=principal.org_id,
            wardrobe_id=wardrobe.id,
            number=1,
            spec=body.spec.model_dump(mode="json"),
            reference_asset_ids=list(body.spec.reference_asset_ids),
        )
    )
    await session.flush()
    await session.refresh(wardrobe)
    return await _wardrobe_detail(session, wardrobe)


@router.get("/v1/creators/{creator_id}/wardrobes", response_model=list[WardrobeDetail])
async def list_wardrobes(creator_id: UUID, principal: Reader, session: DbSession) -> list[WardrobeDetail]:
    await get_scoped(session, Creator, principal.ctx, creator_id, "creator")
    rows = (
        await session.execute(
            sa.select(Wardrobe)
            .where(Wardrobe.org_id == principal.org_id, Wardrobe.creator_id == creator_id)
            .order_by(Wardrobe.name)
        )
    ).scalars()
    return [await _wardrobe_detail(session, w) for w in rows]


@router.post("/v1/wardrobes/{wardrobe_id}/versions", response_model=WardrobeVersionOut, status_code=201)
async def create_wardrobe_version(
    wardrobe_id: UUID, body: WardrobeVersionCreate, principal: Writer, session: DbSession
) -> WardrobeVersionOut:
    wardrobe = await lock_scoped(session, Wardrobe, principal.ctx, wardrobe_id, "wardrobe")
    base_id = body.from_version_id or wardrobe.current_version_id
    base = await get_scoped(session, WardrobeVersion, principal.ctx, base_id, "wardrobe version") if base_id else None
    if base is not None and base.wardrobe_id != wardrobe.id:
        raise NotFoundError("wardrobe version not found for this wardrobe")
    if body.spec is None and base is None:
        raise InvalidInputError("the first version needs a spec", issues=[Issue("spec", "required", path="/spec")])
    spec = body.spec or WardrobeSpec.model_validate(base.spec)  # type: ignore[union-attr]
    await _check_wardrobe_spec(session, principal.org_id, spec)
    row = WardrobeVersion(
        org_id=principal.org_id,
        wardrobe_id=wardrobe.id,
        number=await next_number(session, WardrobeVersion, WardrobeVersion.wardrobe_id, wardrobe.id),
        parent_version_id=base.id if base else None,
        spec=spec.model_dump(mode="json"),
        reference_asset_ids=list(spec.reference_asset_ids),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return WardrobeVersionOut.model_validate(row)


@router.get("/v1/wardrobe-versions/{wardrobe_version_id}", response_model=WardrobeVersionOut)
async def get_wardrobe_version(wardrobe_version_id: UUID, principal: Reader, session: DbSession) -> WardrobeVersionOut:
    return WardrobeVersionOut.model_validate(
        await get_scoped(session, WardrobeVersion, principal.ctx, wardrobe_version_id, "wardrobe version")
    )


@router.patch("/v1/wardrobe-versions/{wardrobe_version_id}", response_model=WardrobeVersionOut)
async def patch_wardrobe_version(
    wardrobe_version_id: UUID, body: WardrobeVersionPatch, principal: Writer, session: DbSession
) -> WardrobeVersionOut:
    row = await lock_scoped(session, WardrobeVersion, principal.ctx, wardrobe_version_id, "wardrobe version")
    require_draft(row, "wardrobe version")
    await _check_wardrobe_spec(session, principal.org_id, body.spec)
    row.spec = body.spec.model_dump(mode="json")
    row.reference_asset_ids = list(body.spec.reference_asset_ids)
    await session.flush()
    await session.refresh(row)
    return WardrobeVersionOut.model_validate(row)


@router.post("/v1/wardrobe-versions/{wardrobe_version_id}:approve", response_model=WardrobeVersionOut)
async def approve_wardrobe_version(
    wardrobe_version_id: UUID, principal: Approver, request: Request, session: DbSession
) -> WardrobeVersionOut:
    row = await lock_scoped(session, WardrobeVersion, principal.ctx, wardrobe_version_id, "wardrobe version")
    wardrobe = await lock_scoped(session, Wardrobe, principal.ctx, row.wardrobe_id, "wardrobe")
    require_draft(row, "wardrobe version")
    issues = await asset_issues(
        session, principal.org_id, row.reference_asset_ids, path="/reference_asset_ids", family="image"
    )
    if not row.reference_asset_ids:
        issues.append(
            Issue(
                "wardrobe_references",
                "a wardrobe needs at least one reference image (BuildWardrobeWorkflow)",
                path="/reference_asset_ids",
            )
        )
    if issues:
        raise ApprovalBlockedError("the wardrobe version cannot be approved yet", issues=issues)
    row.status = "approved"
    wardrobe.current_version_id = row.id
    await session.flush()
    await audit(session, principal, "wardrobe_version.approve", "wardrobe_version", row.id, request=request)
    await session.refresh(row)
    return WardrobeVersionOut.model_validate(row)


# ====================================================================== voices (design, test bench: studio.py)
class VoiceVersionOut(Out):
    id: UUID
    voice_id: UUID
    number: int
    status: str
    description: str
    references: list[Any] = Field(default_factory=list)
    wpm: dict[str, Any]
    lexicon: list[Any]
    default_prosody: dict[str, Any]
    created_at: datetime


class VoiceOut(Out):
    id: UUID
    creator_id: UUID | None
    name: str
    kind: str
    current_version_id: UUID | None
    created_at: datetime


class VoiceDetail(VoiceOut):
    versions: list[VersionRef]


@router.get("/v1/voices", response_model=list[VoiceOut])
async def list_voices(principal: Reader, session: DbSession, creator_id: UUID | None = None) -> list[VoiceOut]:
    query = sa.select(Voice).where(Voice.org_id == principal.org_id).order_by(Voice.name)
    if creator_id is not None:
        query = query.where(Voice.creator_id == creator_id)
    return [VoiceOut.model_validate(v) for v in (await session.execute(query)).scalars()]


@router.get("/v1/voices/{voice_id}", response_model=VoiceDetail)
async def get_voice(voice_id: UUID, principal: Reader, session: DbSession) -> VoiceDetail:
    voice = await get_scoped(session, Voice, principal.ctx, voice_id, "voice")
    versions = (
        await session.execute(
            sa.select(VoiceVersion).where(VoiceVersion.voice_id == voice.id).order_by(VoiceVersion.number)
        )
    ).scalars()
    return VoiceDetail(
        **VoiceOut.model_validate(voice).model_dump(), versions=[VersionRef.model_validate(v) for v in versions]
    )


@router.get("/v1/voice-versions/{voice_version_id}", response_model=VoiceVersionOut)
async def get_voice_version(voice_version_id: UUID, principal: Reader, session: DbSession) -> VoiceVersionOut:
    return VoiceVersionOut.model_validate(
        await get_scoped(session, VoiceVersion, principal.ctx, voice_version_id, "voice version")
    )
