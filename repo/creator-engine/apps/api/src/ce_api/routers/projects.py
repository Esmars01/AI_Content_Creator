"""Projects (§30). DELETE archives: videos and their versions are never deleted (§12.8)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import InvalidInputError, Issue
from ce_db.models.research import BrandKit
from ce_db.models.videos import Project
from ce_db.repository import TenantRepository
from fastapi import APIRouter, Query, Response
from pydantic import Field

from ce_api.common import Page, page
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.schemas import Body, Out, examples

router = APIRouter(tags=["projects"])

Name = Annotated[str, Field(min_length=1, max_length=200)]


class ProjectCreate(Body):
    name: Name
    description: Annotated[str, Field(max_length=5000)] = ""
    default_creator_id: UUID | None = None
    brand_kit_id: UUID | None = Field(default=None, description="the default brand of the project's new plans")
    budget_usd: Annotated[Decimal | None, Field(ge=0)] = None
    settings: dict[str, Any] = Field(default_factory=dict)

    model_config = examples([{"name": "Spring launch", "description": "Short-form series"}])


class ProjectPatch(Body):
    name: Name | None = None
    description: Annotated[str | None, Field(max_length=5000)] = None
    default_creator_id: UUID | None = None
    brand_kit_id: UUID | None = None
    budget_usd: Annotated[Decimal | None, Field(ge=0)] = None
    settings: dict[str, Any] | None = None

    model_config = examples([{"name": "Spring launch (EU)"}])


class ProjectOut(Out):
    id: UUID
    name: str
    description: str
    default_creator_id: UUID | None
    brand_kit_id: UUID | None
    budget_usd: Decimal | None
    settings: dict[str, Any]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


async def _check_brand_kit(session: DbSession, principal: Any, kit_id: UUID | None) -> None:
    if kit_id is None:
        return
    found = (
        await session.execute(
            sa.select(BrandKit.id).where(
                BrandKit.org_id == principal.org_id, BrandKit.id == kit_id, BrandKit.archived_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise InvalidInputError("unknown brand kit", issues=[Issue("unknown_brand_kit", str(kit_id), "/brand_kit_id")])


@router.post("/v1/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, principal: Writer, session: DbSession) -> ProjectOut:
    await _check_brand_kit(session, principal, body.brand_kit_id)
    repo = TenantRepository(Project, session, principal.ctx)
    row = await repo.add(Project(**body.model_dump()))
    await session.refresh(row)
    return ProjectOut.model_validate(row)


@router.get("/v1/projects", response_model=Page[ProjectOut])
async def list_projects(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    include_archived: bool = False,
) -> Page[ProjectOut]:
    query = sa.select(Project).where(Project.org_id == principal.org_id)
    if not include_archived:
        query = query.where(Project.archived_at.is_(None))
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, Project.id, cursor=cursor, limit=size)
    return Page[ProjectOut](items=[ProjectOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: UUID, principal: Reader, session: DbSession) -> ProjectOut:
    return ProjectOut.model_validate(await TenantRepository(Project, session, principal.ctx).get(project_id))


@router.patch("/v1/projects/{project_id}", response_model=ProjectOut)
async def patch_project(project_id: UUID, body: ProjectPatch, principal: Writer, session: DbSession) -> ProjectOut:
    values = body.model_dump(exclude_unset=True)
    await _check_brand_kit(session, principal, values.get("brand_kit_id"))
    row = await TenantRepository(Project, session, principal.ctx).update(project_id, **values)
    await session.refresh(row)
    return ProjectOut.model_validate(row)


@router.delete("/v1/projects/{project_id}", status_code=204)
async def archive_project(project_id: UUID, principal: Writer, session: DbSession, services: ServicesDep) -> Response:
    repo = TenantRepository(Project, session, principal.ctx)
    row = await repo.get(project_id)
    if row.archived_at is None:
        await repo.update(project_id, archived_at=services.clock())
    return Response(status_code=204)
