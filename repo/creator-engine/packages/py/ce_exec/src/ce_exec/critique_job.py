"""`CritiqueWorkflow` (§26, ADR 0056): the Creative Director's critique of a rendered version, and
the `AutonomousSuggestWorkflow` stub.

Stages (the studio loop, ADR 0055):
- `critique/start`: a VLM pass over the version's proxy render (or its final render) at
  `qc.critique_sampling_fps`, answering `ce_qc.critique.CRITIQUE_SCHEMA`;
- `critique/store`: `ce_qc.critique.critique` over the coverage report, the QC reports, world and
  render QC, consistency reports and the VLM answer → a `critiques` row. Findings carry typed edit
  operations; they are applied only when the user proposes them (`…/findings/{id}:propose`).
- `autonomous_suggest/start`: refuses while `features.autonomous_suggest_enabled` is off (V1).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_db.models.behavior import QCReport
from ce_db.models.creators import ConsistencyReport
from ce_db.models.videos import Critique, Render, VideoVersion
from ce_qc.critique import CRITIC_VERSION, CRITIQUE_SCHEMA, critique

from ce_exec.context import ExecServices
from ce_exec.creator_test import _outputs
from ce_exec.studio import ModelCall, StudioContext, StudioError, StudioStep, stage

__all__ = ["autonomous_start", "critique_start", "critique_store"]

QUESTION = (
    "You are the creative director reviewing a short AI-generated creator video, sampled at a few frames "
    "per second. Score realism (does it look like real footage of a real person?) and visual quality "
    "(sharpness, artifacts, stability) from 0 to 1, and list time-stamped problems a viewer would notice "
    "(warping, frozen faces, broken hands, flicker, lip-sync drift). Answer with JSON that matches the schema."
)


@stage("critique", "start")
async def critique_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    async with svc.db.session() as session:
        version = await session.get(VideoVersion, version_id)
        if version is None or version.org_id != org_id:
            raise StudioError("the version does not exist")
        if version.state not in ("ready", "needs_review", "approved", "partial"):
            raise StudioError(f"a critique needs a rendered version (this one is {version.state})")
    outputs = await _outputs(svc, org_id, version_id)
    video = None
    for kind in ("render.proxy", "render.final"):
        for _, out in outputs.get(kind, []):
            ref = out.refs.get("video") or out.refs.get("media")
            if ref is not None:
                video = ref
                break
        if video is not None:
            break
    if video is None:
        raise StudioError("the version has no rendered video to critique")
    mock = bool(video.meta.get("mock")) or any(
        bool(o.refs.get("video") and o.refs["video"].meta.get("mock")) for _, o in outputs.get("avatar.render", [])
    )
    call = ModelCall(
        key="critique.vlm",
        capability="vision.video",
        prefer_mock=mock,
        request={
            "media": video.model_dump(mode="json"),
            "question": QUESTION,
            "json_schema": CRITIQUE_SCHEMA,
            "sampling_fps": float(svc.bundle.app.qc.critique_sampling_fps),
        },
    )
    return StudioStep(calls=[call], next="store", progress=0.5, data={"mock_media": mock})


@stage("critique", "store")
async def critique_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    vdata = await svc.version(org_id, version_id)
    spec = vdata.spec
    outputs = await _outputs(svc, org_id, version_id)
    calls = {o["key"]: o for o in data.get("outputs", [])}
    vlm_call = calls.get("critique.vlm")
    vlm = dict(dict(vlm_call.get("result") or {}).get("answer") or {}) if vlm_call else {}
    if vlm_call and vlm_call.get("mock"):
        vlm["mock"] = True
    coverage = next((o for _, o in outputs.get("behavior.coverage", [])), None)
    final = next((o for _, o in outputs.get("render.final", [])), None)
    shot_times = {
        k: (float(v[0]), float(v[1])) for k, v in dict((final.data if final else {}).get("shots", {})).items()
    }
    mode = svc.bundle.modes.get(str(spec.meta.mode))
    async with svc.db.session() as session:
        reports = (
            (
                await session.execute(
                    sa.select(QCReport).where(QCReport.org_id == org_id, QCReport.version_id == version_id)
                )
            )
            .scalars()
            .all()
        )
        verdicts = (
            (
                await session.execute(
                    sa.select(ConsistencyReport.verdict).where(
                        ConsistencyReport.org_id == org_id, ConsistencyReport.version_id == version_id
                    )
                )
            )
            .scalars()
            .all()
        )
        render_id = (
            await session.execute(
                sa.select(Render.id)
                .where(Render.org_id == org_id, Render.version_id == version_id)
                .order_by(Render.is_proxy.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    render_qc = next((o for _, o in outputs.get("qc.render", [])), None)
    result = critique(
        spec=spec.model_dump(mode="json"),
        coverage_entries=list(dict((coverage.data if coverage else {}).get("report") or {}).get("entries", [])),
        coverage_targets=dict((coverage.data if coverage else {}).get("targets") or {}),
        shot_reports=[{"verdict": r.verdict, **r.checks} for r in reports if r.target_type == "shot"],
        take_reports=[r.checks for r in reports if r.target_type == "take"],
        vlm=vlm,
        world_scores=[
            float(o.data["environment_identity"])
            for _, o in outputs.get("qc.world", [])
            if o.data.get("environment_identity") is not None
        ],
        render_checks=dict(render_qc.data.get("checks") or {}) if render_qc else None,
        consistency_verdicts=list(verdicts),
        speech_passed=[bool(o.data.get("passed")) for _, o in outputs.get("asr.verify", [])],
        shot_times=shot_times,
        max_shot_s=float(mode.edit_grammar.max_shot_s) if mode else None,
    )
    meta: dict[str, Any] = {
        "critic": CRITIC_VERSION,
        "vlm_adapter": vlm_call.get("adapter_id") if vlm_call else None,
        "vlm_mock": bool(vlm.get("mock")),
        "applied": [],
    }
    async with svc.db.transaction() as session:
        row = Critique(
            org_id=org_id,
            version_id=version_id,
            render_id=render_id,
            job_id=UUID(ctx.job_id),
            scores={**result["scores"], "_meta": meta},
            findings=result["findings"],
        )
        session.add(row)
        await session.flush()
        critique_id = str(row.id)
    return StudioStep(done=True, result={"critique_id": critique_id, "findings": len(result["findings"])})


@stage("autonomous_suggest", "start")
async def autonomous_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    if not svc.bundle.app.features.autonomous_suggest_enabled:
        raise StudioError("autonomous suggestions are off (features.autonomous_suggest_enabled, V1)")
    # V1: periodic critiques of recent videos turned into suggestions; every suggestion still needs
    # the user's approval (§26). Nothing beyond the flag check is built in the MVP.
    raise StudioError("autonomous suggestions are not implemented in the MVP")
