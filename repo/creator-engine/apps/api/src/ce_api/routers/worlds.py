"""Worlds (§30, §19): World DNA versions, plate choices, approval, diffs.

Plate *generation* and fingerprinting run in `BuildWorldPlatesWorkflow` (World Studio, Phase 10).
Until then the API records plate choices on drafts and `:approve` verifies that the required plates
and the fingerprints exist (§19.2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import CameraPositionStatus, JobKind
from ce_core.errors import InvalidInputError, Issue, NotFoundError
from ce_core.identity.world import WorldDNA, validate_world_dna
from ce_db.models.assets import Artifact
from ce_db.models.creators import Creator
from ce_db.models.worlds import World, WorldVersion
from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import Field, ValidationError

from ce_api.common import Page, audit, page
from ce_api.deps import Approver, DbSession, Reader, ServicesDep, Writer
from ce_api.errors import ApprovalBlockedError
from ce_api.jobs import start_studio_job
from ce_api.references import asset_issues
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped, next_number, require_draft

router = APIRouter(tags=["worlds"])


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 JSON Merge Patch."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def json_diff(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
    """Differences from `a` to `b` as JSON-Pointer-addressed entries (lists compared whole, keyed lists by `key`)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[dict[str, Any]] = []
        for key in sorted(set(a) | set(b)):
            pointer = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key not in a:
                out.append({"op": "add", "path": pointer, "value": b[key]})
            elif key not in b:
                out.append({"op": "remove", "path": pointer, "old": a[key]})
            else:
                out += json_diff(a[key], b[key], pointer)
        return out
    keyed = isinstance(a, list) and isinstance(b, list) and all(isinstance(x, dict) and "key" in x for x in [*a, *b])
    if keyed:
        return json_diff({x["key"]: x for x in a}, {x["key"]: x for x in b}, path)
    return [] if a == b else [{"op": "replace", "path": path or "/", "old": a, "value": b}]


class WorldCreate(Body):
    name: Annotated[str | None, Field(max_length=200)] = Field(default=None, description="default: dna.name")
    kind: str | None = Field(default=None, description="default: dna.kind")
    owner_creator_id: UUID | None = None
    dna: WorldDNA

    model_config = examples(
        [
            {
                "dna": {
                    "vocab_version": "2026.10.1",
                    "name": "Alex's home office",
                    "kind": "home_office",
                    "geometry": {"dimensions_m": [3.5, 4.0, 2.6], "layout": "desk facing the window"},
                    "lighting": {
                        "key": {"azimuth_deg": 45, "elevation_deg": 30, "intensity": 0.8, "color_temp_k": 5200}
                    },
                    "time_and_weather": {
                        "default_time_of_day": "late_afternoon",
                        "allowed_times": ["late_afternoon", "evening"],
                    },
                    "acoustics": {"room_profile": "small_office", "rt60_s": 0.3},
                    "camera_positions": [
                        {
                            "key": "cam_desk_front",
                            "height_m": 1.2,
                            "distance_m": 0.8,
                            "lens_equiv_mm": 26,
                            "default_framing": "medium_close_up",
                            "allowed_camera_profiles": ["webcam"],
                        }
                    ],
                }
            }
        ]
    )


class WorldVersionCreate(Body):
    from_version_id: UUID | None = None
    patch: dict[str, Any] | None = Field(default=None, description="RFC 7396 merge patch applied to the base DNA")

    model_config = examples([{"patch": {"time_and_weather": {"default_time_of_day": "evening"}}}])


class WorldVersionPatch(Body):
    dna: WorldDNA | None = None
    patch: dict[str, Any] | None = Field(default=None, description="RFC 7396 merge patch applied to the DNA")

    model_config = examples([{"patch": {"style_tags": ["cozy", "warm"]}}])


class PlateChoice(Body):
    camera_position_key: str
    time_of_day: str
    weather: str
    asset_id: UUID

    model_config = examples(
        [
            {
                "camera_position_key": "cam_desk_front",
                "time_of_day": "late_afternoon",
                "weather": "clear",
                "asset_id": "0192f0a0-0000-7000-8000-0000000000c3",
            }
        ]
    )


class WorldVersionOut(Out):
    id: UUID
    world_id: UUID
    number: int
    parent_version_id: UUID | None
    status: str
    dna: dict[str, Any]
    plates: dict[str, Any]
    plate_candidates: dict[str, Any] = Field(default_factory=dict)
    fingerprints_artifact_id: UUID | None
    fingerprint_job_id: UUID | None = Field(default=None, description="set by plates:choose (the fingerprinting job)")
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VersionRef(Out):
    id: UUID
    number: int
    status: str
    created_at: datetime


class WorldOut(Out):
    id: UUID
    name: str
    kind: str
    owner_creator_id: UUID | None
    current_version_id: UUID | None
    status: str
    created_at: datetime


class WorldDetail(WorldOut):
    versions: list[VersionRef]


class DiffEntry(Out):
    op: str
    path: str
    old: Any = None
    value: Any = None


class WorldDiff(Out):
    world_version_id: UUID
    against: UUID
    changes: list[DiffEntry]


def _validated(dna: dict[str, Any] | WorldDNA, services: ServicesDep) -> WorldDNA:
    try:
        model = dna if isinstance(dna, WorldDNA) else WorldDNA.model_validate(dna)
    except ValidationError as exc:
        issues = [
            Issue(str(e["type"]), str(e["msg"]), path="/" + "/".join(str(p) for p in e["loc"])) for e in exc.errors()
        ]
        raise InvalidInputError("the World DNA is invalid", issues=issues) from exc
    if issues := [i for i in validate_world_dna(model, services.vocab) if i.severity == "error"]:
        raise InvalidInputError("the World DNA is invalid", issues=issues)
    return model


async def _detail(session: DbSession, world: World) -> WorldDetail:
    versions = (
        await session.execute(
            sa.select(WorldVersion).where(WorldVersion.world_id == world.id).order_by(WorldVersion.number)
        )
    ).scalars()
    return WorldDetail(
        **WorldOut.model_validate(world).model_dump(), versions=[VersionRef.model_validate(v) for v in versions]
    )


@router.post("/v1/worlds", response_model=WorldDetail, status_code=201)
async def create_world(body: WorldCreate, principal: Writer, session: DbSession, services: ServicesDep) -> WorldDetail:
    dna = _validated(body.dna, services)
    issues = []
    if body.kind is not None and body.kind != dna.kind:
        issues.append(Issue("world_kind", "kind differs from dna.kind", path="/kind"))
    if issues:
        raise InvalidInputError("inconsistent world", issues=issues)
    if body.owner_creator_id is not None:
        await get_scoped(session, Creator, principal.ctx, body.owner_creator_id, "creator")
    world = World(
        org_id=principal.org_id, name=body.name or dna.name, kind=dna.kind, owner_creator_id=body.owner_creator_id
    )
    session.add(world)
    await session.flush()
    session.add(WorldVersion(org_id=principal.org_id, world_id=world.id, number=1, dna=dna.model_dump(mode="json")))
    await session.flush()
    await session.refresh(world)
    return await _detail(session, world)


@router.get("/v1/worlds", response_model=Page[WorldOut])
async def list_worlds(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[WorldOut]:
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(
        session, sa.select(World).where(World.org_id == principal.org_id), World.id, cursor=cursor, limit=size
    )
    return Page[WorldOut](items=[WorldOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/worlds/{world_id}", response_model=WorldDetail)
async def get_world(world_id: UUID, principal: Reader, session: DbSession) -> WorldDetail:
    return await _detail(session, await get_scoped(session, World, principal.ctx, world_id, "world"))


@router.post("/v1/worlds/{world_id}/versions", response_model=WorldVersionOut, status_code=201)
async def create_world_version(
    world_id: UUID, body: WorldVersionCreate, principal: Writer, session: DbSession, services: ServicesDep
) -> WorldVersionOut:
    world = await lock_scoped(session, World, principal.ctx, world_id, "world")
    base_id = body.from_version_id or world.current_version_id
    if base_id is None:  # no approved version yet: derive from the latest draft
        base_id = (
            await session.execute(
                sa.select(WorldVersion.id)
                .where(WorldVersion.world_id == world.id)
                .order_by(WorldVersion.number.desc())
                .limit(1)
            )
        ).scalar_one()
    base = await get_scoped(session, WorldVersion, principal.ctx, base_id, "world version")
    if base.world_id != world.id:
        raise NotFoundError("world version not found for this world")
    dna = _validated(merge_patch(base.dna, body.patch or {}), services)
    if dna.kind != world.kind:
        raise InvalidInputError(
            "a world keeps its kind", issues=[Issue("world_kind", "dna.kind cannot change", path="/patch/kind")]
        )
    row = WorldVersion(
        org_id=principal.org_id,
        world_id=world.id,
        number=await next_number(session, WorldVersion, WorldVersion.world_id, world.id),
        parent_version_id=base.id,
        dna=dna.model_dump(mode="json"),
        plates=dict(base.plates),  # chosen plates carry over; fingerprints are recomputed (§19.2)
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return WorldVersionOut.model_validate(row)


@router.get("/v1/world-versions/{world_version_id}", response_model=WorldVersionOut)
async def get_world_version(world_version_id: UUID, principal: Reader, session: DbSession) -> WorldVersionOut:
    return WorldVersionOut.model_validate(
        await get_scoped(session, WorldVersion, principal.ctx, world_version_id, "world version")
    )


@router.patch("/v1/world-versions/{world_version_id}", response_model=WorldVersionOut)
async def patch_world_version(
    world_version_id: UUID, body: WorldVersionPatch, principal: Writer, session: DbSession, services: ServicesDep
) -> WorldVersionOut:
    row = await lock_scoped(session, WorldVersion, principal.ctx, world_version_id, "world version")
    require_draft(row, "world version")
    world = await get_scoped(session, World, principal.ctx, row.world_id, "world")
    if body.dna is None and body.patch is None:
        raise InvalidInputError("send dna or patch")
    source: Any = body.dna.model_dump(mode="json") if body.dna is not None else row.dna
    dna = _validated(merge_patch(source, body.patch or {}), services)
    if dna.kind != world.kind:
        raise InvalidInputError(
            "a world keeps its kind", issues=[Issue("world_kind", "dna.kind cannot change", path="/dna/kind")]
        )
    row.dna = dna.model_dump(mode="json")
    row.fingerprints_artifact_id = None  # plates must be re-fingerprinted against the new DNA
    await session.flush()
    await session.refresh(row)
    return WorldVersionOut.model_validate(row)


@router.post("/v1/world-versions/{world_version_id}/plates:choose", response_model=WorldVersionOut)
async def choose_plate(
    world_version_id: UUID,
    body: PlateChoice,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> WorldVersionOut:
    """Records the canonical plate for a position × time × weather and enqueues the fingerprinting
    of the chosen plates (`BuildWorldPlatesWorkflow`, fingerprint mode; `fingerprint_job_id`)."""
    row = await lock_scoped(session, WorldVersion, principal.ctx, world_version_id, "world version")
    require_draft(row, "world version")
    dna = WorldDNA.model_validate(row.dna)
    issues: list[Issue] = []
    position = dna.camera_position(body.camera_position_key)
    if position is None or position.status != CameraPositionStatus.PERMITTED:
        issues.append(
            Issue("camera_position", "not a permitted camera position of this world", path="/camera_position_key")
        )
    if body.time_of_day not in dna.time_and_weather.allowed_times:
        issues.append(Issue("time_of_day", "not an allowed time of day", path="/time_of_day"))
    if body.weather not in dna.time_and_weather.allowed_weather:
        issues.append(Issue("weather", "not an allowed weather", path="/weather"))
    issues += await asset_issues(session, principal.org_id, [body.asset_id], path="/asset_id", family="image")
    if issues:
        raise InvalidInputError("invalid plate choice", issues=issues)
    plates = {cam: {tod: dict(w) for tod, w in by_tod.items()} for cam, by_tod in (row.plates or {}).items()}
    plates.setdefault(body.camera_position_key, {}).setdefault(body.time_of_day, {})[body.weather] = str(body.asset_id)
    row.plates = plates
    row.fingerprints_artifact_id = None  # recomputed for the new choice set
    await session.flush()
    job = await start_studio_job(
        session, services, background, org_id=principal.org_id, user_id=principal.user_id,
        kind=JobKind.WORLD_PLATES, target_type="world_version", target_id=row.id, args={"mode": "fingerprint"},
    )  # fmt: skip
    await session.refresh(row)
    return WorldVersionOut.model_validate(row).model_copy(update={"fingerprint_job_id": job.id})


async def world_approval_issues(
    session: DbSession, services: ServicesDep, org_id: UUID, row: WorldVersion
) -> list[Issue]:
    dna = _validated(row.dna, services)
    issues: list[Issue] = []
    tod, weather = dna.time_and_weather.default_time_of_day, dna.time_and_weather.default_weather
    plate_ids: list[UUID] = []
    for position in dna.camera_positions:
        if position.status != CameraPositionStatus.PERMITTED:
            continue
        chosen = (row.plates or {}).get(position.key, {}).get(tod, {}).get(weather)
        if chosen is None:
            issues.append(
                Issue(
                    "plate_missing",
                    f"no canonical plate for {position.key} at {tod}/{weather}",
                    path=f"/plates/{position.key}/{tod}/{weather}",
                )
            )
        else:
            plate_ids.append(UUID(str(chosen)))
    issues += await asset_issues(session, org_id, plate_ids, path="/plates", family="image")
    if row.fingerprints_artifact_id is None:
        issues.append(
            Issue(
                "fingerprints_missing",
                "plate fingerprints have not been computed (BuildWorldPlatesWorkflow)",
                path="/fingerprints_artifact_id",
            )
        )
    else:
        exists = (
            await session.execute(
                sa.select(Artifact.id).where(Artifact.org_id == org_id, Artifact.id == row.fingerprints_artifact_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            issues.append(
                Issue(
                    "fingerprints_missing", "the fingerprints artifact does not exist", path="/fingerprints_artifact_id"
                )
            )
    return issues


@router.post("/v1/world-versions/{world_version_id}:approve", response_model=WorldVersionOut)
async def approve_world_version(
    world_version_id: UUID, principal: Approver, request: Request, session: DbSession, services: ServicesDep
) -> WorldVersionOut:
    row = await lock_scoped(session, WorldVersion, principal.ctx, world_version_id, "world version")
    world = await lock_scoped(session, World, principal.ctx, row.world_id, "world")
    require_draft(row, "world version")
    if issues := await world_approval_issues(session, services, principal.org_id, row):
        raise ApprovalBlockedError("the world version cannot be approved yet", issues=issues)
    row.status = "approved"
    row.approved_at = services.clock()
    world.current_version_id = row.id
    await session.flush()
    await audit(session, principal, "world_version.approve", "world_version", row.id, request=request)
    await session.refresh(row)
    return WorldVersionOut.model_validate(row)


@router.get("/v1/world-versions/{world_version_id}/diff", response_model=WorldDiff)
async def diff_world_version(
    world_version_id: UUID, principal: Reader, session: DbSession, against: UUID | None = None
) -> WorldDiff:
    row = await get_scoped(session, WorldVersion, principal.ctx, world_version_id, "world version")
    other_id = against or row.parent_version_id
    if other_id is None:
        raise InvalidInputError("this is the first version; pass ?against=", issues=[Issue("against", "required")])
    other = await get_scoped(session, WorldVersion, principal.ctx, other_id, "world version")
    if other.world_id != row.world_id:
        raise InvalidInputError("both versions must belong to the same world")
    changes = json_diff({"dna": other.dna, "plates": other.plates}, {"dna": row.dna, "plates": row.plates})
    return WorldDiff(world_version_id=row.id, against=other.id, changes=[DiffEntry(**c) for c in changes])
