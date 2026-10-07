"""`PackagingWorkflow` — Director stage 12 (§13, Phase 12): per-platform packaging and thumbnails
for a rendered version.

One stage, `package/start`, per requested platform:
1. **limits**: the platform's verified rules, else our labelled design defaults
   (`ce_director.packaging.effective_limits`);
2. **texts**: the configured LLM writes title, description, hashtags, CTA and thumbnail texts from
   the spec (data, I10), validated and repaired (`ce_llm.structured`); with no usable LLM (fixture
   miss, outage) or an answer still invalid, the labelled template packaging is used (rule 5) and
   the reason recorded in `generator` and `issues`. Each LLM call is a `director_runs` row
   (`stage = packaging`);
3. **thumbnails**: `packaging.thumbnail_candidates` frames of the version's final render (the hook,
   then talking-shot midpoints), each with one of the texts, at the platform's thumbnail size;
4. one `packaging` row per (version, platform), `draft` until a person approves it (a new run
   resets an approval: the texts changed).

A render made by mock engines is packaged too (its thumbnails show mock frames); the export rules,
not packaging, decide what may leave the system.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.canonical import content_digest
from ce_db import execution as rec
from ce_db.models.videos import DirectorRun, Packaging, VideoVersion
from ce_director.packaging import (
    PackagingDraft,
    PlatformPackagingOut,
    effective_limits,
    problems,
    spec_summary,
    template_packaging,
)
from ce_llm import FixtureMiss, LLMError, PromptLibrary, StructuredOutputError, prompts_root, provider_from_settings
from ce_llm import structured as llm_structured
from ce_obs import get_logger
from ce_render.video import make_thumbnail
from ce_storage.content import StorageRunContext, content_key

from ce_exec.context import ExecServices
from ce_exec.creator_test import _outputs
from ce_exec.studio import StudioContext, StudioError, StudioStep, stage

__all__ = ["package_start", "thumbnail_times"]

_log = get_logger("ce.exec.packaging")
RENDERED = ("ready", "needs_review", "approved", "partial")


_FALLBACK_REASONS: dict[type[Exception], str] = {
    FixtureMiss: "no recorded LLM answer for this platform (fixture mode)",
    LLMError: "the LLM did not answer",
    StructuredOutputError: "the LLM's answer did not meet this platform's limits",
}


def thumbnail_times(shots: dict[str, list[float]], duration_s: float, n: int) -> list[float]:
    """Frame times for `n` candidates: just after the start (the hook), then the midpoints of the
    longest shots in timeline order, never at the very end."""
    first = min(0.5, max(0.0, duration_s * 0.1))
    spans = sorted(((float(a), float(b)) for a, b in shots.values() if b > a), key=lambda s: -(s[1] - s[0]))
    mids = sorted({round((a + b) / 2, 3) for a, b in spans[: max(0, n - 1)]})
    times = [first, *[t for t in mids if abs(t - first) > 0.25]]
    while len(times) < n and duration_s > 0:  # short videos: spread evenly
        times.append(round(duration_s * len(times) / (n + 1), 3))
    return [min(t, max(0.0, duration_s - 0.05)) for t in times[:n]]


def _final_render(outputs: dict[str, list[tuple[str, Any]]], preset_ids: set[str]) -> tuple[str, Any] | None:
    finals = outputs.get("render.final", [])
    for key, out in finals:
        if key.split(":", 1)[1] in preset_ids:
            return key, out
    return finals[0] if finals else None


async def _texts(
    svc: ExecServices, org_id: UUID, version_id: UUID, platform: Any, summary: dict[str, Any], limits: Any, n: int
) -> PackagingDraft:
    """The LLM's packaging (validated, repaired), else the labelled template."""
    config = svc.bundle.app.packaging
    template = template_packaging(summary, platform.id, limits, thumbnails=n)
    try:
        provider = provider_from_settings(svc.settings)
        prompts = PromptLibrary(prompts_root())
        prompt = prompts.render(
            "packaging", platform_label=platform.label, summary=summary, limits=limits.as_dict(), thumbnails=n
        )
    except Exception as exc:  # no provider configured: the template, labelled
        template.generator["fallback"] = "no LLM is configured"
        template.issues.append(
            {"code": "llm_unavailable", "message": template.generator["fallback"], "detail": str(exc)[:200]}
        )
        return template
    run = DirectorRun(
        org_id=org_id,
        version_id=version_id,
        stage="packaging",
        template_version=prompt.template_version,
        provider=getattr(provider, "key", "llm"),
        model=getattr(provider, "model", ""),
        input={"platform": platform.id, "limits": limits.as_dict(), "summary_digest": content_digest(summary)},
        status="failed",
    )
    try:
        result = await llm_structured(
            provider,
            stage="packaging",
            output_type=PlatformPackagingOut,
            messages=prompt.messages(),
            scenario_id=f"packaging:{platform.id}",
            check=lambda out: problems(out, limits, thumbnails=n),
            max_repairs=int(config.max_repairs),
        )
    except (FixtureMiss, LLMError, StructuredOutputError) as exc:
        # the Packaging card shows the reason in words; the exception (with server paths) stays in the
        # run record and the issue detail (audit PKG-MSG)
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        template.generator["fallback"] = _FALLBACK_REASONS.get(type(exc), "the LLM's answer could not be used")
        template.issues.append({"code": "llm_fallback", "message": template.generator["fallback"], "detail": detail})
        run.output = {"error": detail}
        async with svc.db.transaction() as session:
            session.add(run)
        return template
    finally:
        await provider.aclose()
    run.status, run.output = result.status, result.value.model_dump(mode="json")
    run.tokens_in = sum(a.tokens_in for a in result.attempts)
    run.tokens_out = sum(a.tokens_out for a in result.attempts)
    async with svc.db.transaction() as session:
        session.add(run)
    draft = PackagingDraft.from_out(
        platform.id,
        result.value,
        limits,
        generator={
            "kind": "llm",
            "provider": result.provider,
            "model": result.model,
            "template_version": prompt.template_version,
            "attempts": len(result.attempts),
        },
    )
    if len(draft.thumbnail_texts) < n:  # fewer texts than candidates: the template fills in
        draft.thumbnail_texts += [t for t in template.thumbnail_texts if t not in draft.thumbnail_texts]
    draft.thumbnail_texts = draft.thumbnail_texts[:n]
    return draft


@stage("package", "start")
async def package_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    bundle = svc.bundle
    config = bundle.app.packaging
    async with svc.db.session() as session:
        version = (
            await session.execute(
                sa.select(VideoVersion).where(VideoVersion.org_id == org_id, VideoVersion.id == version_id)
            )
        ).scalar_one_or_none()
    if version is None:
        raise StudioError("the version does not exist")
    if version.state not in RENDERED:
        raise StudioError(f"packaging needs a rendered version (this one is {version.state})")
    platforms = [str(p) for p in (ctx.args.get("platforms") or [])]
    unknown = [p for p in platforms if p not in bundle.platforms]
    if not platforms or unknown:
        raise StudioError(f"unknown or missing platforms: {unknown or platforms}")
    outputs = await _outputs(svc, org_id, version_id)
    summary = spec_summary(version.spec or {})
    n = int(config.thumbnail_candidates)
    run_ctx = StorageRunContext(svc.content, svc.scratch("package"))
    results: list[dict[str, Any]] = []
    try:
        for platform_id in platforms:
            platform = bundle.platforms[platform_id]
            limits = effective_limits(
                platform, config.design_limits, thumbnail_text_max_chars=int(config.thumbnail_text_max_chars)
            )
            draft = await _texts(svc, org_id, version_id, platform, summary, limits, n)
            issues = list(draft.issues) + [
                {"code": "limit", "message": m}
                for m in problems(
                    PlatformPackagingOut(
                        title=draft.title or " ",
                        description=draft.description,
                        hashtags=draft.hashtags,
                        cta_text=draft.cta_text,
                        thumbnail_texts=draft.thumbnail_texts,
                    ),
                    limits,
                    thumbnails=n,
                )
            ]
            final = _final_render(outputs, {p.id for p in platform.render_presets})
            candidates: list[dict[str, Any]] = []
            if final is None:
                issues.append({"code": "no_render", "message": "the version has no final render to take frames from"})
            else:
                key, out = final
                video = out.refs.get("video") or out.refs.get("media")
                if video is None:
                    issues.append({"code": "no_render", "message": f"{key} has no video"})
                else:
                    path = await run_ctx.read_artifact(video)
                    duration = float(out.data.get("duration_s") or 0.0)
                    times = thumbnail_times(dict(out.data.get("shots") or {}), duration, n)
                    size = platform.packaging
                    for index, at in enumerate(times):
                        text = (
                            draft.thumbnail_texts[index % len(draft.thumbnail_texts)] if draft.thumbnail_texts else ""
                        )
                        png = await make_thumbnail(
                            path,
                            at,
                            run_ctx.scratch_dir / f"thumb-{platform_id}-{index}.png",
                            width=size.thumbnail_width,
                            height=size.thumbnail_height,
                            text=text,
                        )
                        ref = await run_ctx.write_artifact(png, "image", role="thumbnail", mime="image/png")
                        candidates.append(
                            {"sha256": ref.sha256, "bytes": ref.bytes, "at_s": at, "text": text, "render": key}
                        )
            async with svc.db.transaction() as session:
                for candidate in candidates:
                    candidate["artifact_id"] = str(
                        await rec.register_artifact(
                            session,
                            org_id,
                            sha256=candidate["sha256"],
                            kind="image",
                            mime="image/png",
                            size=int(candidate["bytes"]),
                            storage_key=content_key(candidate["sha256"]),
                            media={"role": "thumbnail", "version_id": str(version_id), "platform": platform_id},
                        )
                    )
                row = (
                    await session.execute(
                        sa.select(Packaging).where(
                            Packaging.org_id == org_id,
                            Packaging.version_id == version_id,
                            Packaging.platform == platform_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = Packaging(org_id=org_id, version_id=version_id, platform=platform_id)
                    session.add(row)
                row.title, row.description = draft.title, draft.description
                row.hashtags, row.cta_text = list(draft.hashtags), draft.cta_text
                row.thumbnail_candidates = candidates
                row.thumbnail_artifact_ids = [UUID(candidates[0]["artifact_id"])] if candidates else []
                row.limits, row.issues = draft.limits.as_dict(), issues
                row.generator = {**draft.generator, "thumbnail_texts": draft.thumbnail_texts}
                row.status, row.approved_by, row.approved_at = "draft", None, None
                row.job_id = UUID(ctx.job_id)
                await session.flush()
                await rec.add_artifact_refs(
                    session,
                    org_id,
                    [UUID(c["artifact_id"]) for c in candidates],
                    ref_type="packaging",
                    ref_id=str(row.id),
                )
                results.append(
                    {
                        "packaging_id": str(row.id),
                        "platform": platform_id,
                        "generator": draft.generator.get("kind"),
                        "issues": len(issues),
                        "thumbnails": len(candidates),
                    }
                )
            _log.info(
                "packaged", version_id=str(version_id), platform=platform_id, generator=draft.generator.get("kind")
            )
    finally:
        run_ctx.cleanup()
    return StudioStep(done=True, result={"version_id": str(version_id), "packaging": results})
