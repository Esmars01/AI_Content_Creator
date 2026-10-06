"""Spec templates and brand kits (§30 "Templates and brand", Phase 12, ADR 0059).

Spec templates are partial specs made of portable slots (`ce_core.spec.templates`):

- `POST /v1/spec-templates` saves one — captured from a version (`from_version_id`, `kind`,
  `paths?`), given as a `body`, or composed only (`composes_from`). Every value is checked against
  the configuration (render presets, caption styles, camera profiles, vocabularies) and brand kits
  of the organization before it is stored.
- Templates are immutable: `PATCH` saves the next **version** (`parent_template_id`, `version + 1`);
  only the latest version can be patched; `DELETE` archives.
- `POST /v1/spec-templates:compose` previews a composition (merged body, conflicts, which template
  set each slot) without saving anything.
- `POST /v1/spec-templates/{id}:apply {version_id}` turns the template into edit operations and
  submits them as an ordinary edit proposal (`from_template`), validated, previewed and applied
  like any edit (I3); `preview: true` returns the operations and conflicts instead.

Brand kits: `GET|POST /v1/brand-kits`, `GET|PATCH|DELETE /v1/brand-kits/{id}` — name, logo (an
image asset of the organization), colors, font names and a caption style. The project's kit is the
default brand of new plans (`PATCH /v1/projects/{id} {brand_kit_id}`); the logo is burned by
`render.final` when `brand.logo_overlay` is on. The V1 fields (intro/outro, libraries, rules) are
refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import ConflictError, InvalidInputError, Issue
from ce_core.spec.templates import TemplateError, body_digest, capture, compose, operations, validate_body
from ce_db.models.assets import Asset
from ce_db.models.research import BrandKit, SpecTemplate
from ce_db.models.videos import VideoVersion
from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.common import Page, audit, begin_idempotent, finish_idempotent, page
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.routers.edits import EditAccepted, _operations, _propose, _start
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["templates"])

Kind = Literal["video", "scene", "creator_style", "camera", "caption", "brand"]
Name = Annotated[str, Field(min_length=1, max_length=200)]
MAX_COMPOSE = 10
MAX_DEPTH = 5


class TemplateCreate(Body):
    model_config = examples(
        [
            {"name": "Punchy captions", "kind": "caption", "from_version_id": "0192f0a0-0000-7000-8000-000000000002"},
            {
                "name": "Close and fast",
                "kind": "scene",
                "body": {"scene_defaults": {"pacing": {"cut_cadence": "fast"}}},
            },
            {"name": "House style", "kind": "video", "composes_from": ["0192f0a0-0000-7000-8000-0000000000a1"]},
        ]
    )
    name: Name
    kind: Kind
    description: Annotated[str, Field(max_length=2000)] = ""
    from_version_id: UUID | None = None
    paths: list[Annotated[str, Field(max_length=200)]] | None = Field(default=None, max_length=50)
    body: dict[str, Any] | None = None
    composes_from: list[UUID] = Field(default_factory=list, max_length=MAX_COMPOSE)

    @model_validator(mode="after")
    def _one_source(self) -> TemplateCreate:
        if self.from_version_id is not None and self.body is not None:
            raise ValueError("give from_version_id or body, not both")
        if self.from_version_id is None and self.body is None and not self.composes_from:
            raise ValueError("give from_version_id, body or composes_from")
        if self.paths and self.from_version_id is None:
            raise ValueError("paths narrow a capture from a version")
        return self


class TemplatePatch(Body):
    model_config = examples([{"body": {"captions": {"style_id": "minimal_lower"}}}])
    name: Name | None = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    body: dict[str, Any] | None = None
    composes_from: list[UUID] | None = Field(default=None, max_length=MAX_COMPOSE)


class ConflictOut(Out):
    path: str
    values: list[dict[str, Any]]
    winner: Any


class TemplateOut(Out):
    id: UUID
    kind: str
    name: str
    description: str
    version: int
    parent_template_id: UUID | None
    source_version_id: UUID | None
    paths: list[str]
    body: dict[str, Any]
    composes_from: list[UUID]
    body_digest: str
    created_by: UUID | None
    archived_at: datetime | None
    created_at: datetime


class TemplateDetail(TemplateOut):
    effective: dict[str, Any] = Field(description="the composed body that apply uses")
    conflicts: list[ConflictOut]
    sources: dict[str, str] = Field(description="slot path → id of the template that set it")
    latest: bool


class ComposeBody(Body):
    model_config = examples([{"template_ids": ["0192f0a0-0000-7000-8000-0000000000a1"]}])
    template_ids: list[UUID] = Field(min_length=1, max_length=MAX_COMPOSE)


class ComposeOut(Out):
    body: dict[str, Any]
    conflicts: list[ConflictOut]
    sources: dict[str, str]


class TemplateApplyBody(Body):
    model_config = examples([{"version_id": "0192f0a0-0000-7000-8000-000000000002"}])
    version_id: UUID
    preview: bool = False


class ApplyPreview(Out):
    operations: list[dict[str, Any]]
    conflicts: list[ConflictOut]
    body: dict[str, Any]


# ====================================================================== validation
def _template_error(exc: TemplateError, prefix: str = "/body") -> InvalidInputError:
    issues = [Issue("template_body", message, path=f"{prefix}{path}") for path, message in exc.problems] or [
        Issue("template_body", str(exc), path=prefix)
    ]
    return InvalidInputError(str(exc), issues=issues)


async def _reference_issues(
    session: AsyncSession, services: Any, org_id: UUID, body: dict[str, Any], prefix: str = "/body"
) -> list[Issue]:
    """Values that name something must name something that exists (config, vocabulary, org)."""
    bundle, vocab = services.effective.bundle, services.vocab
    issues: list[Issue] = []
    for target in (body.get("meta") or {}).get("platform_targets") or []:
        if target not in bundle.platforms:
            issues.append(Issue("unknown_platform", target, path=f"{prefix}/meta/platform_targets"))
    for output in (body.get("render") or {}).get("outputs") or []:
        if bundle.render_preset(output["preset_id"]) is None:
            issues.append(Issue("unknown_preset", output["preset_id"], path=f"{prefix}/render/outputs"))
    style = (body.get("captions") or {}).get("style_id")
    if style is not None and style not in bundle.caption_styles:
        issues.append(Issue("unknown_caption_style", style, path=f"{prefix}/captions/style_id"))
    camera = ((body.get("shot_defaults") or {}).get("camera")) or {}
    if camera.get("profile_id") is not None and camera["profile_id"] not in bundle.camera_profiles:
        issues.append(
            Issue("unknown_camera_profile", camera["profile_id"], path=f"{prefix}/shot_defaults/camera/profile_id")
        )
    for name in ("framing", "angle"):
        token = camera.get(name)
        if token is not None and not vocab.has(f"camera.{name}", token):
            issues.append(Issue("unknown_vocab", token, path=f"{prefix}/shot_defaults/camera/{name}"))
    kit = (body.get("brand") or {}).get("brand_kit_id")
    if kit is not None:
        found = (
            await session.execute(
                sa.select(BrandKit.id).where(
                    BrandKit.org_id == org_id, BrandKit.id == UUID(str(kit)), BrandKit.archived_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if found is None:
            issues.append(Issue("unknown_brand_kit", str(kit), path=f"{prefix}/brand/brand_kit_id"))
    return issues


async def _effective(
    session: AsyncSession, org_id: UUID, row: SpecTemplate, depth: int = 0, seen: frozenset[UUID] = frozenset()
) -> Any:
    """The template's composed body: its `composes_from` (each composed in turn), then its own."""
    if depth > MAX_DEPTH or row.id in seen:
        raise InvalidInputError(
            "template composition is too deep or circular", issues=[Issue("composition", str(row.id))]
        )
    layers: list[tuple[str, dict[str, Any]]] = []
    for child_id in row.composes_from or []:
        child = await _template(session, org_id, child_id)
        layers.append((str(child.id), (await _effective(session, org_id, child, depth + 1, seen | {row.id})).body))
    layers.append((str(row.id), dict(row.body or {})))
    try:
        return compose(layers)
    except TemplateError as exc:
        raise _template_error(exc) from exc


async def _template(session: AsyncSession, org_id: UUID, template_id: UUID) -> SpecTemplate:
    row = (
        await session.execute(
            sa.select(SpecTemplate).where(SpecTemplate.org_id == org_id, SpecTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise InvalidInputError("unknown template", issues=[Issue("unknown_template", str(template_id))])
    return row


async def _check_composes(session: AsyncSession, org_id: UUID, ids: list[UUID]) -> None:
    for template_id in ids:
        row = await _template(session, org_id, template_id)
        if row.archived_at is not None:
            raise InvalidInputError("an archived template cannot be composed", issues=[Issue("archived", str(row.id))])


async def _validated(
    session: AsyncSession, services: Any, org_id: UUID, kind: str, body: dict[str, Any], composes: list[UUID]
) -> tuple[dict[str, Any], Any]:
    """(clean body, composition) after every check; the composition is validated for the kind."""
    try:
        clean = validate_body(body, kind)
    except TemplateError as exc:
        raise _template_error(exc) from exc
    await _check_composes(session, org_id, composes)
    probe = SpecTemplate(id=UUID(int=0), org_id=org_id, kind=kind, name="", body=clean, composes_from=composes)
    composed = await _effective(session, org_id, probe)
    try:
        validate_body(composed.body, kind)
    except TemplateError as exc:
        raise _template_error(exc, "/composes_from") from exc
    issues = await _reference_issues(session, services, org_id, clean)
    if issues:
        raise InvalidInputError("the template names things that do not exist", issues=issues)
    return clean, composed


def _detail(row: SpecTemplate, composed: Any, latest: bool) -> TemplateDetail:
    return TemplateDetail(
        **TemplateOut.model_validate(row).model_dump(),
        effective=composed.body,
        conflicts=[ConflictOut(**c.as_dict()) for c in composed.conflicts],
        sources={k: v if v != str(UUID(int=0)) else str(row.id) for k, v in composed.sources.items()},
        latest=latest,
    )


async def _is_latest(session: AsyncSession, row: SpecTemplate) -> bool:
    child = await session.scalar(
        sa.select(sa.func.count())
        .select_from(SpecTemplate)
        .where(SpecTemplate.org_id == row.org_id, SpecTemplate.parent_template_id == row.id)
    )
    return not child


# ====================================================================== spec templates
@router.post("/v1/spec-templates", status_code=201, response_model=TemplateDetail)
async def create_template(
    body: TemplateCreate, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> TemplateDetail:
    """Saves a spec template: captured from a version, given as a body, or composed only."""
    source_version: UUID | None = None
    paths: list[str] = []
    raw = dict(body.body or {})
    if body.from_version_id is not None:
        version = await get_scoped(session, VideoVersion, principal.ctx, body.from_version_id, "version")
        try:
            raw = capture(version.spec or {}, body.kind, body.paths)
        except TemplateError as exc:
            raise InvalidInputError(str(exc), issues=[Issue("template_paths", str(exc), path="/paths")]) from exc
        if not raw and not body.composes_from:
            raise InvalidInputError(
                "the version has no value for these slots", issues=[Issue("empty_template", "nothing to save")]
            )
        source_version, paths = version.id, list(body.paths or [])
    clean, composed = await _validated(session, services, principal.org_id, body.kind, raw, list(body.composes_from))
    row = SpecTemplate(
        org_id=principal.org_id,
        kind=body.kind,
        name=body.name,
        description=body.description,
        body=clean,
        composes_from=list(body.composes_from),
        version=1,
        source_version_id=source_version,
        paths=paths,
        body_digest=body_digest(clean),
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit(session, principal, "spec_template.create", "spec_template", row.id, request=request)
    return _detail(row, composed, latest=True)


@router.get("/v1/spec-templates", response_model=Page[TemplateOut])
async def list_templates(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    kind: Kind | None = None,
    include_archived: bool = False,
    all_versions: bool = False,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[TemplateOut]:
    """The organization's templates (latest versions unless `all_versions`)."""
    query = sa.select(SpecTemplate).where(SpecTemplate.org_id == principal.org_id)
    if kind:
        query = query.where(SpecTemplate.kind == kind)
    if not include_archived:
        query = query.where(SpecTemplate.archived_at.is_(None))
    if not all_versions:
        child = sa.orm.aliased(SpecTemplate)
        query = query.where(
            ~sa.exists().where(child.org_id == SpecTemplate.org_id, child.parent_template_id == SpecTemplate.id)
        )
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, SpecTemplate.id, cursor=cursor, limit=size)
    return Page[TemplateOut](items=[TemplateOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/spec-templates/{spec_template_id}", response_model=TemplateDetail)
async def get_template(spec_template_id: UUID, principal: Reader, session: DbSession) -> TemplateDetail:
    row = await get_scoped(session, SpecTemplate, principal.ctx, spec_template_id, "spec template")
    return _detail(row, await _effective(session, principal.org_id, row), await _is_latest(session, row))


@router.patch("/v1/spec-templates/{spec_template_id}", status_code=201, response_model=TemplateDetail)
async def patch_template(
    spec_template_id: UUID,
    body: TemplatePatch,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
) -> TemplateDetail:
    """Saves the next version (templates are immutable; versions that used the old one keep it)."""
    row = await lock_scoped(session, SpecTemplate, principal.ctx, spec_template_id, "spec template")
    if row.archived_at is not None:
        raise ConflictError("an archived template cannot be changed")
    if not await _is_latest(session, row):
        raise ConflictError("only the latest version of a template can be changed")
    new_body = dict(body.body) if body.body is not None else dict(row.body or {})
    composes = list(body.composes_from) if body.composes_from is not None else list(row.composes_from or [])
    clean, composed = await _validated(session, services, principal.org_id, row.kind, new_body, composes)
    nxt = SpecTemplate(
        org_id=principal.org_id,
        kind=row.kind,
        name=body.name or row.name,
        description=body.description if body.description is not None else row.description,
        body=clean,
        composes_from=composes,
        version=row.version + 1,
        parent_template_id=row.id,
        source_version_id=row.source_version_id,
        paths=list(row.paths or []),
        body_digest=body_digest(clean),
        created_by=principal.user_id,
    )
    session.add(nxt)
    await session.flush()
    await session.refresh(nxt)
    await audit(
        session,
        principal,
        "spec_template.version",
        "spec_template",
        nxt.id,
        request=request,
        before={"id": str(row.id), "version": row.version, "body_digest": row.body_digest},
        after={"version": nxt.version, "body_digest": nxt.body_digest},
    )
    return _detail(nxt, composed, latest=True)


@router.delete("/v1/spec-templates/{spec_template_id}", status_code=204)
async def archive_template(
    spec_template_id: UUID, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> Response:
    row = await lock_scoped(session, SpecTemplate, principal.ctx, spec_template_id, "spec template")
    if row.archived_at is None:
        row.archived_at = services.clock()
        await audit(session, principal, "spec_template.archive", "spec_template", row.id, request=request)
    return Response(status_code=204)


@router.post("/v1/spec-templates:compose", response_model=ComposeOut)
async def compose_templates(body: ComposeBody, principal: Reader, session: DbSession) -> ComposeOut:
    """A composition preview: the templates (each with its own composition) merged in order."""
    layers: list[tuple[str, dict[str, Any]]] = []
    for template_id in body.template_ids:
        row = await get_scoped(session, SpecTemplate, principal.ctx, template_id, "spec template")
        layers.append((str(row.id), (await _effective(session, principal.org_id, row)).body))
    composed = compose(layers)
    return ComposeOut(
        body=composed.body,
        conflicts=[ConflictOut(**c.as_dict()) for c in composed.conflicts],
        sources=composed.sources,
    )


@router.post("/v1/spec-templates/{spec_template_id}:apply", response_model=EditAccepted | ApplyPreview)
async def apply_template(
    spec_template_id: UUID,
    body: TemplateApplyBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Submits the template as an edit proposal (`202`), or previews its operations (`200`)."""
    key: str | None = None
    if not body.preview:
        key, replay = await begin_idempotent(
            session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
        )
        if replay is not None:
            return JSONResponse(replay.body, status_code=replay.status)
    row = await get_scoped(session, SpecTemplate, principal.ctx, spec_template_id, "spec template")
    if row.archived_at is not None:
        raise ConflictError("an archived template cannot be applied")
    version = await get_scoped(session, VideoVersion, principal.ctx, body.version_id, "version")
    composed = await _effective(session, principal.org_id, row)
    issues = await _reference_issues(session, services, principal.org_id, composed.body, prefix="/template")
    if issues:  # a kit archived after the template was saved, a preset removed from config
        raise InvalidInputError("the template names things that no longer exist", issues=issues)
    reason = f"spec template {row.name!r} v{row.version}"
    raw_ops = operations(composed.body, version.spec or {}, reason=reason, template_ids=[str(row.id)])
    conflicts = [ConflictOut(**c.as_dict()) for c in composed.conflicts]
    if body.preview:
        return ApplyPreview(operations=raw_ops, conflicts=conflicts, body=composed.body)
    if not raw_ops:
        raise ConflictError("the version already has every value of this template", template_id=str(row.id))
    ops = _operations(raw_ops)
    proposal, job, _ = await _propose(
        session,
        principal,
        version,
        kind="from_template",
        instruction=f"Apply {reason}",
        operations=ops,
        editor={"template_id": str(row.id), "template_version": row.version},
    )
    await audit(
        session,
        principal,
        "spec_template.apply",
        "spec_template",
        row.id,
        request=request,
        after={"version_id": str(version.id), "edit_proposal_id": str(proposal.id), "operations": len(raw_ops)},
    )
    accepted = EditAccepted(edit_proposal_id=proposal.id, job_id=job.id)
    assert key is not None
    await finish_idempotent(session, principal, key, 202, accepted)
    _start(background, services, principal, job)
    return JSONResponse(accepted.model_dump(mode="json"), status_code=202)


# ====================================================================== brand kits
Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
ColorRole = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
FontName = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[\w .\-]+$")]
FontRole = Literal["heading", "body", "caption"]


class BrandKitCreate(Body):
    model_config = examples(
        [
            {
                "name": "Acme",
                "colors": {"primary": "#1A73E8", "accent": "#FBBC04"},
                "fonts": {"heading": "Inter"},
                "caption_style_id": "bold_pop_highlight",
            }
        ]
    )
    name: Name
    logo_asset_id: UUID | None = None
    colors: dict[ColorRole, Color] = Field(default_factory=dict, max_length=12)
    fonts: dict[FontRole, FontName] = Field(default_factory=dict)
    caption_style_id: Annotated[str | None, Field(max_length=100)] = None
    v1: dict[str, Any] = Field(default_factory=dict, description="V1 features; refused while unavailable")


class BrandKitPatch(Body):
    model_config = examples([{"colors": {"primary": "#0B57D0"}, "caption_style_id": "minimal_lower"}])
    name: Name | None = None
    logo_asset_id: UUID | None = None
    clear_logo: bool = False
    colors: dict[ColorRole, Color] | None = Field(default=None, max_length=12)
    fonts: dict[FontRole, FontName] | None = None
    caption_style_id: Annotated[str | None, Field(max_length=100)] = None
    v1: dict[str, Any] | None = None


class BrandKitOut(Out):
    id: UUID
    name: str
    logo_asset_id: UUID | None
    colors: dict[str, Any]
    fonts: dict[str, Any]
    caption_style_id: str | None
    created_by: UUID | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


async def _kit_issues(
    session: AsyncSession, services: Any, principal: Any, *, logo: UUID | None, style: str | None, v1: dict[str, Any]
) -> None:
    issues: list[Issue] = []
    if v1:
        issues.append(Issue("v1_feature", "intro/outro, libraries and rules are not available yet", path="/v1"))
    if style is not None and style not in services.effective.bundle.caption_styles:
        issues.append(Issue("unknown_caption_style", style, path="/caption_style_id"))
    if logo is not None:
        asset = (
            await session.execute(sa.select(Asset).where(Asset.org_id == principal.org_id, Asset.id == logo))
        ).scalar_one_or_none()
        if asset is None:
            issues.append(Issue("unknown_asset", str(logo), path="/logo_asset_id"))
        elif asset.status != "ready" or not str(asset.mime).startswith("image/"):
            issues.append(Issue("logo_not_image", f"{asset.mime} ({asset.status})", path="/logo_asset_id"))
    if issues:
        raise InvalidInputError("invalid brand kit", issues=issues)


@router.post("/v1/brand-kits", status_code=201, response_model=BrandKitOut)
async def create_brand_kit(
    body: BrandKitCreate, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> BrandKitOut:
    await _kit_issues(session, services, principal, logo=body.logo_asset_id, style=body.caption_style_id, v1=body.v1)
    row = BrandKit(
        org_id=principal.org_id,
        name=body.name,
        logo_asset_id=body.logo_asset_id,
        colors=dict(body.colors),
        fonts=dict(body.fonts),
        caption_style_id=body.caption_style_id,
        created_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit(session, principal, "brand_kit.create", "brand_kit", row.id, request=request)
    return BrandKitOut.model_validate(row)


@router.get("/v1/brand-kits", response_model=Page[BrandKitOut])
async def list_brand_kits(
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Page[BrandKitOut]:
    query = sa.select(BrandKit).where(BrandKit.org_id == principal.org_id)
    if not include_archived:
        query = query.where(BrandKit.archived_at.is_(None))
    size = min(limit or services.config.api.default_page_size, services.config.api.max_page_size)
    rows, next_cursor = await page(session, query, BrandKit.id, cursor=cursor, limit=size)
    return Page[BrandKitOut](items=[BrandKitOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/v1/brand-kits/{brand_kit_id}", response_model=BrandKitOut)
async def get_brand_kit(brand_kit_id: UUID, principal: Reader, session: DbSession) -> BrandKitOut:
    return BrandKitOut.model_validate(await get_scoped(session, BrandKit, principal.ctx, brand_kit_id, "brand kit"))


@router.patch("/v1/brand-kits/{brand_kit_id}", response_model=BrandKitOut)
async def patch_brand_kit(
    brand_kit_id: UUID,
    body: BrandKitPatch,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
) -> BrandKitOut:
    """Changes a kit. Versions already planned keep rendering with the kit's current content: the
    kit enters `render.final` by content digest, so a change re-renders only the final encode."""
    row = await lock_scoped(session, BrandKit, principal.ctx, brand_kit_id, "brand kit")
    if row.archived_at is not None:
        raise ConflictError("an archived brand kit cannot be changed")
    await _kit_issues(
        session, services, principal, logo=body.logo_asset_id, style=body.caption_style_id, v1=body.v1 or {}
    )
    before = BrandKitOut.model_validate(row).model_dump(mode="json")
    if body.name is not None:
        row.name = body.name
    if body.clear_logo:
        row.logo_asset_id = None
    if body.logo_asset_id is not None:
        row.logo_asset_id = body.logo_asset_id
    if body.colors is not None:
        row.colors = dict(body.colors)
    if body.fonts is not None:
        row.fonts = dict(body.fonts)
    if "caption_style_id" in body.model_fields_set:
        row.caption_style_id = body.caption_style_id
    await session.flush()
    await session.refresh(row)
    after = BrandKitOut.model_validate(row)
    await audit(
        session, principal, "brand_kit.update", "brand_kit", row.id, request=request, before=before,
        after=after.model_dump(mode="json"),
    )  # fmt: skip
    return after


@router.delete("/v1/brand-kits/{brand_kit_id}", status_code=204)
async def archive_brand_kit(
    brand_kit_id: UUID, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> Response:
    """Archives the kit: new plans and templates stop using it; existing versions still render."""
    row = await lock_scoped(session, BrandKit, principal.ctx, brand_kit_id, "brand kit")
    if row.archived_at is None:
        row.archived_at = services.clock()
        await audit(session, principal, "brand_kit.archive", "brand_kit", row.id, request=request)
    return Response(status_code=204)
