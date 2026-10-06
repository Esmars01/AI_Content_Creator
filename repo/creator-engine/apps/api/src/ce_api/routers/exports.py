"""Caption translation, packaging and exports (§30 "Rendering, packaging, export", Phase 12).

- `GET /v1/platforms`: what the export dialog offers per platform — render presets, the
  disclosure checklist, packaging sizes and the packaging limits with where each comes from
  (`platform` only for verified platform files; our `design_default` otherwise, D16).
- `POST /v1/versions/{id}/captions:translate {language}`: a `set_captions` operation adding the
  language, auto-applied (§30); the derived version builds a `captions.translate` node. Its caption
  files arrive `pending`. `POST /v1/captions/{id}:approve|reject` reviews a translation (all its
  formats); a re-translated file goes back to `pending` (`ce_db.execution.upsert_caption`).
- `POST /v1/versions/{id}:package {platforms?}` → `PackagingWorkflow` (Director stage 12);
  `GET /v1/versions/{id}/packaging`; `PATCH /v1/packaging/{id}` edits the texts or picks a
  thumbnail (validated against the stored limits; an edit withdraws an approval);
  `POST /v1/packaging/{id}:approve` (no open limit issues).
- `POST /v1/renders/{id}/exports {platform, packaging_id?, disclosure_checklist, caption_languages?}`
  creates a packaged render download. It is refused for a mock-provenance render (§32), a version
  that is not `ready`, a preset of another platform, an unapproved packaging (when required), an
  unchecked required disclosure item, unsupported claims without an override, and translations
  that are not approved. The export metadata (texts, provenance, captions, disclosures, license
  obligations of the engines used) is stored as an artifact; the Creator Memory `exported` write
  path runs as a `memory_update` job. Audit-logged.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.edit.ops import SetCaptions
from ce_core.enums import JobKind
from ce_core.errors import ConflictError, InvalidInputError, Issue
from ce_db import execution as rec
from ce_db.models.assets import Artifact, ExecutionNode
from ce_db.models.platform import Plugin
from ce_db.models.research import Claim
from ce_db.models.videos import Caption, Export, Packaging, Render, VideoVersion
from ce_director.packaging import PlatformPackagingOut, effective_limits, normalize_hashtags, problems
from ce_storage.content import content_key
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ce_api.common import audit, begin_idempotent, finish_idempotent
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.jobs import start_studio_job
from ce_api.routers.edits import DerivedAccepted, _derived
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["exports"])

RENDERED = ("ready", "needs_review", "approved", "partial")


# ====================================================================== platforms
class PresetOut(Out):
    id: str
    aspect: str
    width: int
    height: int
    fps: float


class ChecklistOut(Out):
    key: str
    label: str
    required: bool


class PlatformOut(Out):
    id: str
    label: str
    verified_at: str | None
    presets: list[PresetOut]
    checklist: list[ChecklistOut]
    limits: dict[str, Any] = Field(description="effective packaging limits; `sources` says platform or design_default")
    thumbnail: dict[str, int]


def _limits(services: Any, platform: Any) -> Any:
    config = services.config.packaging
    return effective_limits(
        platform, config.design_limits, thumbnail_text_max_chars=int(config.thumbnail_text_max_chars)
    )


@router.get("/v1/platforms", response_model=list[PlatformOut])
async def list_platforms(principal: Reader, services: ServicesDep) -> list[PlatformOut]:
    """The platforms of the configuration, as the export dialog shows them."""
    out = []
    for platform in services.effective.bundle.platforms.values():
        out.append(
            PlatformOut(
                id=platform.id,
                label=platform.label,
                verified_at=platform.verified_at.isoformat() if platform.verified_at else None,
                presets=[
                    PresetOut(id=p.id, aspect=p.aspect, width=p.width, height=p.height, fps=float(p.fps))
                    for p in platform.render_presets
                ],
                checklist=[
                    ChecklistOut(key=c.key, label=c.label, required=c.required) for c in platform.export_checklist
                ],
                limits=_limits(services, platform).as_dict(),
                thumbnail={"width": platform.packaging.thumbnail_width, "height": platform.packaging.thumbnail_height},
            )
        )
    return sorted(out, key=lambda p: p.id)


# ====================================================================== caption translation
class TranslateBody(Body):
    model_config = examples([{"language": "de"}])
    language: Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]


class ReviewBody(Body):
    model_config = examples([{"note": "Checked against the source script; names and numbers are right."}])
    note: Annotated[str, Field(max_length=2000)] = ""


class CaptionReviewOut(Out):
    version_id: UUID
    language: str
    review_state: str
    caption_ids: list[UUID]
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_note: str | None


@router.post("/v1/versions/{version_id}/captions:translate", status_code=202, response_model=DerivedAccepted)
async def translate_captions(
    version_id: UUID,
    body: TranslateBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Adds a caption language (auto-applied `set_captions`); the translation arrives `pending`."""
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    spec = version.spec or {}
    languages = services.effective.bundle.languages
    entry = languages.languages.get(body.language) if languages is not None else None
    if entry is None or str(entry.support) == "unsupported":
        raise InvalidInputError(
            f"captions cannot be translated into {body.language!r}",
            issues=[Issue("unsupported_language", body.language, path="/language")],
        )
    captions = dict(spec.get("captions") or {})
    if not captions.get("enabled", True):
        raise ConflictError("captions are off for this version")
    source = str(captions.get("language") or (spec.get("meta") or {}).get("language") or "").split("-")[0]
    if body.language == source:
        raise ConflictError(f"{body.language} is the spoken language of the video")
    existing = [t["language"] for t in captions.get("translations") or []]
    if body.language in existing:
        raise ConflictError(f"the version already has a {body.language} translation")
    op = SetCaptions(
        changes={"translations": [{"language": lang} for lang in [*existing, body.language]]},  # type: ignore[arg-type]
        reason=f"translate captions into {body.language}",
    )
    return await _derived(
        request,
        principal,
        session,
        services,
        background,
        version,
        kind="captions_translate",
        idempotency=key,
        operations=[op],
        instruction=f"Translate captions into {body.language}",
    )


async def _review(session: Any, principal: Any, services: Any, caption_id: UUID, state: str, note: str) -> Any:
    caption = await lock_scoped(session, Caption, principal.ctx, caption_id, "caption")
    if caption.review_state == "n/a":
        raise ConflictError("only translations are reviewed; the spoken-language captions need no review")
    rows = (
        (
            await session.execute(
                sa.select(Caption)
                .where(
                    Caption.org_id == principal.org_id,
                    Caption.version_id == caption.version_id,
                    Caption.language == caption.language,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = services.clock()
    for row in rows:
        row.review_state, row.reviewed_by, row.reviewed_at, row.review_note = (
            state,
            principal.user_id,
            now,
            note or None,
        )
    await session.flush()
    return CaptionReviewOut(
        version_id=caption.version_id,
        language=caption.language,
        review_state=state,
        caption_ids=[r.id for r in rows],
        reviewed_by=principal.user_id,
        reviewed_at=now,
        review_note=note or None,
    )


@router.post("/v1/captions/{caption_id}:approve", response_model=CaptionReviewOut)
async def approve_caption(
    caption_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    body: ReviewBody | None = None,
) -> Any:
    """Approves a translation (every format of its language in this version)."""
    out = await _review(session, principal, services, caption_id, "approved", body.note if body else "")
    await audit(
        session, principal, "caption.approve", "caption", caption_id, request=request, after={"language": out.language}
    )
    return out


@router.post("/v1/captions/{caption_id}:reject", response_model=CaptionReviewOut)
async def reject_caption(
    caption_id: UUID, body: ReviewBody, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> Any:
    """Rejects a translation; it is never exported. Translate again after editing the script."""
    out = await _review(session, principal, services, caption_id, "rejected", body.note)
    await audit(
        session, principal, "caption.reject", "caption", caption_id, request=request,
        after={"language": out.language, "note": body.note},
    )  # fmt: skip
    return out


# ====================================================================== packaging
class PackageBody(Body):
    model_config = examples([{"platforms": ["tiktok", "youtube_shorts"]}])
    platforms: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=10)


class PackageAccepted(Out):
    job_id: UUID
    platforms: list[str]


class PackagingOut(Out):
    id: UUID
    version_id: UUID
    platform: str
    title: str
    description: str
    hashtags: list[str]
    cta_text: str
    thumbnail_artifact_ids: list[UUID]
    thumbnail_candidates: list[dict[str, Any]]
    limits: dict[str, Any]
    issues: list[dict[str, Any]]
    generator: dict[str, Any]
    status: str
    approved_by: UUID | None
    approved_at: datetime | None
    job_id: UUID | None
    updated_at: datetime


class PackagingPatch(Body):
    model_config = examples([{"title": "Why most people get AI agents wrong", "hashtags": ["ai", "agents"]}])
    title: Annotated[str | None, Field(max_length=1000)] = None
    description: Annotated[str | None, Field(max_length=10000)] = None
    hashtags: list[Annotated[str, Field(max_length=200)]] | None = Field(default=None, max_length=50)
    cta_text: Annotated[str | None, Field(max_length=1000)] = None
    thumbnail_artifact_id: UUID | None = None


class Download(Out):
    url: str
    expires_at: datetime
    filename: str


@router.post("/v1/versions/{version_id}:package", status_code=202, response_model=PackageAccepted)
async def package_version(
    version_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
    body: PackageBody | None = None,
) -> Any:
    """Starts Director stage 12 for the version's platforms (default: its platform targets)."""
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if version.state not in RENDERED:
        raise ConflictError(f"packaging needs a rendered version (this one is {version.state})")
    known = services.effective.bundle.platforms
    platforms = list(dict.fromkeys((body.platforms if body and body.platforms else None) or []))
    if not platforms:
        platforms = [p for p in (version.spec or {}).get("meta", {}).get("platform_targets", []) if p in known]
    unknown = [p for p in platforms if p not in known]
    if unknown or not platforms:
        raise InvalidInputError(
            "name the platforms to package for",
            issues=[Issue("unknown_platform", ", ".join(unknown) or "none", path="/platforms")],
        )
    job = await start_studio_job(
        session,
        services,
        background,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kind=JobKind.PACKAGE,
        target_type="video_version",
        target_id=version.id,
        args={"platforms": platforms},
    )
    accepted = PackageAccepted(job_id=job.id, platforms=platforms)
    await finish_idempotent(session, principal, key, 202, accepted)
    return accepted


@router.get("/v1/versions/{version_id}/packaging", response_model=list[PackagingOut])
async def list_packaging(version_id: UUID, principal: Reader, session: DbSession) -> list[PackagingOut]:
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(Packaging)
            .where(Packaging.org_id == principal.org_id, Packaging.version_id == version_id)
            .order_by(Packaging.platform)
        )
    ).scalars()
    return [PackagingOut.model_validate(r) for r in rows]


def _packaging_issues(services: Any, row: Packaging) -> list[str]:
    platform = services.effective.bundle.platforms.get(row.platform)
    if platform is None:
        return [f"unknown platform {row.platform}"]
    limits = _limits(services, platform)
    texts = list((row.generator or {}).get("thumbnail_texts") or []) or ["-"]
    return problems(
        PlatformPackagingOut(
            title=row.title or " ",
            description=row.description,
            hashtags=list(row.hashtags or []),
            cta_text=row.cta_text,
            thumbnail_texts=texts,
        ),
        limits,
        thumbnails=len(texts),
    )


@router.patch("/v1/packaging/{packaging_id}", response_model=PackagingOut)
async def patch_packaging(
    packaging_id: UUID,
    body: PackagingPatch,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
) -> PackagingOut:
    """Edits the texts or picks a thumbnail candidate; validated against the limits. An edit of an
    approved packaging withdraws the approval."""
    row = await lock_scoped(session, Packaging, principal.ctx, packaging_id, "packaging")
    before = PackagingOut.model_validate(row).model_dump(mode="json")
    if body.title is not None:
        row.title = body.title.strip()
    if body.description is not None:
        row.description = body.description.strip()
    if body.hashtags is not None:
        row.hashtags = normalize_hashtags(body.hashtags)
    if body.cta_text is not None:
        row.cta_text = body.cta_text.strip()
    if body.thumbnail_artifact_id is not None:
        candidates = {str(c.get("artifact_id")) for c in row.thumbnail_candidates or []}
        if str(body.thumbnail_artifact_id) not in candidates:
            raise InvalidInputError(
                "pick one of the thumbnail candidates",
                issues=[Issue("unknown_thumbnail", str(body.thumbnail_artifact_id), path="/thumbnail_artifact_id")],
            )
        row.thumbnail_artifact_ids = [body.thumbnail_artifact_id]
    found = _packaging_issues(services, row)
    if found:
        raise InvalidInputError("the packaging breaks its limits", issues=[Issue("packaging_limit", m) for m in found])
    row.issues = [i for i in row.issues or [] if i.get("code") not in ("limit",)]
    row.generator = {**(row.generator or {}), "edited_by": str(principal.user_id)}
    row.status, row.approved_by, row.approved_at = "draft", None, None
    await session.flush()
    await session.refresh(row)
    after = PackagingOut.model_validate(row)
    await audit(
        session, principal, "packaging.update", "packaging", row.id, request=request, before=before,
        after=after.model_dump(mode="json"),
    )  # fmt: skip
    return after


@router.post("/v1/packaging/{packaging_id}:approve", response_model=PackagingOut)
async def approve_packaging(
    packaging_id: UUID, request: Request, principal: Writer, session: DbSession, services: ServicesDep
) -> PackagingOut:
    row = await lock_scoped(session, Packaging, principal.ctx, packaging_id, "packaging")
    found = _packaging_issues(services, row)
    if found:
        raise ConflictError("the packaging breaks its limits; edit it first", problems=found)
    if not row.thumbnail_artifact_ids:
        raise ConflictError("the packaging has no thumbnail; package the version again after it renders")
    row.status, row.approved_by, row.approved_at = "approved", principal.user_id, services.clock()
    await session.flush()
    await session.refresh(row)
    await audit(session, principal, "packaging.approve", "packaging", row.id, request=request)
    return PackagingOut.model_validate(row)


async def _download(services: Any, session: Any, principal: Any, artifact_id: UUID, filename: str) -> Download:
    artifact = await get_scoped(session, Artifact, principal.ctx, artifact_id, "artifact")
    signed = await services.storage.presign_get(
        services.settings.s3_bucket_artifacts,
        artifact.storage_key,
        ttl_s=services.config.storage.presign_ttl_s,
        download_filename=filename,
    )
    return Download(url=signed.url, expires_at=signed.expires_at, filename=filename)


@router.get("/v1/packaging/{packaging_id}/thumbnails/{artifact_id}", response_model=Download)
async def download_thumbnail(
    packaging_id: UUID, artifact_id: UUID, principal: Reader, session: DbSession, services: ServicesDep
) -> Download:
    row = await get_scoped(session, Packaging, principal.ctx, packaging_id, "packaging")
    if str(artifact_id) not in {str(c.get("artifact_id")) for c in row.thumbnail_candidates or []}:
        raise InvalidInputError(
            "not a thumbnail of this packaging", issues=[Issue("unknown_thumbnail", str(artifact_id))]
        )
    return await _download(services, session, principal, artifact_id, f"thumbnail.{row.platform}.png")


# ====================================================================== exports
class ExportCreate(Body):
    model_config = examples(
        [
            {
                "platform": "tiktok",
                "packaging_id": "0192f0a0-0000-7000-8000-0000000000e1",
                "disclosure_checklist": {
                    "ai_label_on": True,
                    "not_mass_produced": True,
                    "visible_label_reviewed": True,
                },
            }
        ]
    )
    platform: Annotated[str, Field(max_length=64)]
    packaging_id: UUID | None = None
    disclosure_checklist: dict[Annotated[str, Field(max_length=64)], bool] = Field(default_factory=dict)
    caption_languages: list[Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]] | None = Field(
        default=None, description="default: the spoken language and every approved translation"
    )


class ExportOut(Out):
    id: UUID
    render_id: UUID
    version_id: UUID | None
    packaging_id: UUID | None
    platform: str
    preset_id: str | None
    caption_ids: list[UUID]
    disclosure_checklist: dict[str, Any]
    metadata_doc: dict[str, Any]
    metadata_artifact_id: UUID | None
    created_by: UUID | None
    created_at: datetime


class ExportDownloads(ExportOut):
    downloads: dict[str, Download] = Field(default_factory=dict)


async def _obligations(session: Any, org_id: UUID, version_id: UUID) -> list[dict[str, Any]]:
    """License obligations for export metadata of the engines that built the version (§24)."""
    rows = (
        await session.execute(
            sa.select(ExecutionNode.route).where(
                ExecutionNode.org_id == org_id, ExecutionNode.version_id == version_id, ExecutionNode.route.is_not(None)
            )
        )
    ).scalars()
    adapters = sorted({str(r.get("adapter_id")) for r in rows if r and r.get("adapter_id")})
    if not adapters:
        return []
    plugins = (await session.execute(sa.select(Plugin).where(Plugin.plugin_key.in_(adapters)))).scalars()
    out: list[dict[str, Any]] = []
    for plugin in plugins:
        for obligation in (plugin.manifest or {}).get("obligations") or []:
            if "export_metadata" in (obligation.get("placement") or []):
                out.append({"adapter_id": plugin.plugin_key, "text": obligation.get("text", "")})
    return out


async def _blocking_claims(session: Any, org_id: UUID, version: VideoVersion) -> list[str]:
    keys = [c.get("key") for c in ((version.spec or {}).get("research") or {}).get("claims", [])]
    if not keys:
        return []
    rows = (
        await session.execute(
            sa.select(Claim).where(
                Claim.org_id == org_id,
                Claim.video_id == version.video_id,
                Claim.claim_key.in_(keys),
                Claim.blocking.is_(True),
                Claim.override_by.is_(None),
            )
        )
    ).scalars()
    return [f"{r.claim_key}: {r.text}" for r in rows]


@router.post("/v1/renders/{render_id}/exports", status_code=201, response_model=ExportDownloads)
async def create_export(
    render_id: UUID,
    body: ExportCreate,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """A packaged render download, after every export rule passes (see the module docstring)."""
    key, replay = await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    render = await get_scoped(session, Render, principal.ctx, render_id, "render")
    version = await get_scoped(session, VideoVersion, principal.ctx, render.version_id, "version")
    bundle, exports = services.effective.bundle, services.config.exports
    platform = bundle.platforms.get(body.platform)
    if platform is None:
        raise InvalidInputError("unknown platform", issues=[Issue("unknown_platform", body.platform, path="/platform")])
    if render.is_proxy or render.status != "ready" or render.artifact_id is None:
        raise ConflictError("only a finished final render can be exported")
    if render.provenance_mode != "real":
        raise ConflictError(
            "this render carries mock provenance (MOCK PROVENANCE — NOT FOR DISTRIBUTION) and cannot be exported",
            reason="mock_provenance",
        )
    if version.state != "ready":
        raise ConflictError(f"only a ready version can be exported (this one is {version.state})")
    if render.preset_id not in {p.id for p in platform.render_presets}:
        raise InvalidInputError(
            f"preset {render.preset_id} is not a {platform.label} preset",
            issues=[Issue("preset_platform", render.preset_id, path="/platform")],
        )
    checklist = {i.key: i for i in platform.export_checklist}
    unknown = sorted(set(body.disclosure_checklist) - set(checklist))
    unchecked = sorted(k for k, item in checklist.items() if item.required and not body.disclosure_checklist.get(k))
    if unknown or unchecked:
        raise InvalidInputError(
            "confirm the disclosure checklist",
            issues=[Issue("unknown_checklist_item", k, path="/disclosure_checklist") for k in unknown]
            + [Issue("checklist_unchecked", checklist[k].label, path=f"/disclosure_checklist/{k}") for k in unchecked],
        )
    packaging: Packaging | None = None
    if body.packaging_id is not None:
        packaging = await get_scoped(session, Packaging, principal.ctx, body.packaging_id, "packaging")
        if packaging.version_id != version.id or packaging.platform != platform.id:
            raise InvalidInputError(
                "the packaging is for another version or platform",
                issues=[Issue("packaging_mismatch", str(packaging.id))],
            )
    if exports.require_approved_packaging and (packaging is None or packaging.status != "approved"):
        raise ConflictError("approve the packaging for this platform before exporting")
    blocked = await _blocking_claims(session, principal.org_id, version)
    if blocked:
        raise ConflictError(
            "unsupported claims block the export: edit the script, add a source or override", claims=blocked
        )
    captions = (
        (
            await session.execute(
                sa.select(Caption).where(Caption.org_id == principal.org_id, Caption.version_id == version.id)
            )
        )
        .scalars()
        .all()
    )
    by_language: dict[str, list[Caption]] = {}
    for caption in captions:
        by_language.setdefault(caption.language, []).append(caption)
    wanted = body.caption_languages
    chosen: list[Caption] = []
    for language, rows in sorted(by_language.items()):
        state = rows[0].review_state
        if wanted is not None and language not in wanted:
            continue
        if state in ("pending", "rejected") and exports.require_approved_translations:
            if wanted is not None:
                raise ConflictError(f"the {language} translation is {state}; approve it before exporting it")
            continue
        chosen += [r for r in rows if r.artifact_id is not None]
    missing = sorted(set(wanted or []) - set(by_language))
    if missing:
        raise InvalidInputError(
            "no captions in these languages", issues=[Issue("unknown_caption_language", ", ".join(missing))]
        )
    hashtags = [f"{platform.packaging.hashtag_prefix}{t}" for t in (packaging.hashtags if packaging else [])]
    description = packaging.description if packaging else ""
    if packaging is not None and platform.packaging.description_hashtags and hashtags:
        description = f"{description}\n\n{' '.join(hashtags)}".strip()
    render_artifact = await get_scoped(session, Artifact, principal.ctx, render.artifact_id, "artifact")
    caption_artifacts = {
        c.id: await get_scoped(session, Artifact, principal.ctx, c.artifact_id, "artifact")
        for c in chosen
        if c.artifact_id is not None
    }
    thumbnail_id = packaging.thumbnail_artifact_ids[0] if packaging and packaging.thumbnail_artifact_ids else None
    thumbnail = await get_scoped(session, Artifact, principal.ctx, thumbnail_id, "artifact") if thumbnail_id else None
    row = Export(
        org_id=principal.org_id,
        render_id=render.id,
        packaging_id=packaging.id if packaging else None,
        platform=platform.id,
        disclosure_checklist={k: bool(v) for k, v in body.disclosure_checklist.items()},
        created_by=principal.user_id,
        version_id=version.id,
        preset_id=render.preset_id,
        caption_ids=[c.id for c in chosen],
    )
    session.add(row)
    await session.flush()
    metadata = {
        "schema": "creator-engine/export-metadata/v1",
        "export_id": str(row.id),
        "platform": {"id": platform.id, "label": platform.label, "rules_verified": platform.verified_at is not None},
        "render": {
            "id": str(render.id),
            "preset_id": render.preset_id,
            "aspect": render.aspect,
            "sha256": render_artifact.sha256,
        },
        "video": {"video_id": str(version.video_id), "version_id": str(version.id)},
        "packaging": (
            {
                "title": packaging.title,
                "description": description,
                "hashtags": hashtags,
                "cta_text": packaging.cta_text,
                "thumbnail_sha256": thumbnail.sha256 if thumbnail else None,
                "generator": packaging.generator.get("kind"),
            }
            if packaging
            else None
        ),
        "captions": [
            {
                "language": c.language,
                "format": c.format,
                "review_state": c.review_state,
                "sha256": caption_artifacts[c.id].sha256,
            }
            for c in chosen
        ],
        "provenance": {
            "mode": render.provenance_mode,
            "c2pa_manifest": bool(render.c2pa_manifest),
            "watermark_payload_id": render.watermark_payload_id,
            "consent_ids": [str(c) for c in render.consent_ids or []],
            "ai_generated": True,
        },
        "disclosure_checklist": row.disclosure_checklist,
        "license_obligations": await _obligations(session, principal.org_id, version.id),
        "created_at": services.clock().isoformat(),
    }
    raw = json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode("utf-8")
    sha = await _put_metadata(services, raw)
    row.metadata_doc = metadata
    row.metadata_artifact_id = await rec.register_artifact(
        session,
        principal.org_id,
        sha256=sha,
        kind="other",
        mime="application/json",
        size=len(raw),
        storage_key=content_key(sha),
        media={"role": "export_metadata", "export_id": str(row.id)},
    )
    await session.flush()
    await rec.add_artifact_refs(
        session,
        principal.org_id,
        [row.metadata_artifact_id, render.artifact_id, *[c.artifact_id for c in chosen if c.artifact_id]],
        ref_type="export",
        ref_id=str(row.id),
    )
    memory_job = await start_studio_job(  # §18.4: the exported write path (activations, usage event)
        session,
        services,
        background,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kind=JobKind.MEMORY_UPDATE,
        target_type="video_version",
        target_id=version.id,
        args={"trigger": "exported", "export_id": str(row.id)},
    )
    memory_job.video_version_id = version.id
    await audit(
        session,
        principal,
        "export.create",
        "export",
        row.id,
        request=request,
        after={"platform": platform.id, "render_id": str(render.id), "checklist": row.disclosure_checklist},
    )
    await session.refresh(row)
    out = await _with_downloads(services, session, principal, row)
    await finish_idempotent(session, principal, key, 201, out)
    return out


async def _put_metadata(services: Any, raw: bytes) -> str:
    from ce_storage.content import ContentStore

    store = ContentStore(services.storage, services.settings.s3_bucket_artifacts)
    return await store.put_bytes(raw, mime="application/json")


async def _with_downloads(services: Any, session: Any, principal: Any, row: Export) -> ExportDownloads:
    downloads: dict[str, Download] = {}
    render = await get_scoped(session, Render, principal.ctx, row.render_id, "render")
    if render.artifact_id is not None:
        downloads["video"] = await _download(
            services, session, principal, render.artifact_id, f"{row.platform}.{render.preset_id}.mp4"
        )
    if row.metadata_artifact_id is not None:
        downloads["metadata"] = await _download(
            services, session, principal, row.metadata_artifact_id, f"{row.platform}.metadata.json"
        )
    for caption_id in row.caption_ids or []:
        caption = await get_scoped(session, Caption, principal.ctx, caption_id, "caption")
        if caption.artifact_id is not None:
            downloads[f"captions.{caption.language}.{caption.format}"] = await _download(
                services, session, principal, caption.artifact_id, f"captions.{caption.language}.{caption.format}"
            )
    thumbnail_sha = ((row.metadata_doc or {}).get("packaging") or {}).get("thumbnail_sha256")
    if thumbnail_sha and row.packaging_id:
        packaging = await get_scoped(session, Packaging, principal.ctx, row.packaging_id, "packaging")
        if packaging.thumbnail_artifact_ids:
            downloads["thumbnail"] = await _download(
                services, session, principal, packaging.thumbnail_artifact_ids[0], f"{row.platform}.thumbnail.png"
            )
    return ExportDownloads(**ExportOut.model_validate(row).model_dump(), downloads=downloads)


@router.get("/v1/versions/{version_id}/exports", response_model=list[ExportOut])
async def list_exports(version_id: UUID, principal: Reader, session: DbSession) -> list[ExportOut]:
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(Export)
            .where(Export.org_id == principal.org_id, Export.version_id == version_id)
            .order_by(Export.created_at.desc())
        )
    ).scalars()
    return [ExportOut.model_validate(r) for r in rows]


@router.get("/v1/exports/{export_id}", response_model=ExportDownloads)
async def get_export(export_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> ExportDownloads:
    row = await get_scoped(session, Export, principal.ctx, export_id, "export")
    return await _with_downloads(services, session, principal, row)
