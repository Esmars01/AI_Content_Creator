"""Screen recordings (§27): the work of `ScreenAnalysisWorkflow`, the planning pass that gives new
screen shots their zooms, speed segments and webcam bubble, and the `screen.prepare` executor.

**Analysis** (on upload of a `screen_recording` asset): FFmpeg scene-change detection →
keyframes (after each cut, and periodically inside long scenes) → OCR with boxes through the
`vision.ocr` route (PP-OCR on CPU; the mock finds no text) on a 1 fps clip of the keyframes →
text diffs and changed-pixel regions between keyframes → dead time → a "what happens when" summary
through the `vision.video` route (the VLM is mocked until a vLLM worker is validated, §39.2) →
one `screen_analysis` artifact, linked from the asset's probe (`probe.screen_analysis`). Routes
that need a GPU are not called in-process: the summary then says it is unavailable.

**Planning**: an edit adding a screen shot (or pointing one at another recording) without zooms or
speed segments gets them from `ce_director.screen.plan_screen` — added to the proposal as a
`set_shot`, so they are typed, reviewable spec data.

**`screen.prepare`**: the recording on the shot's output clock — speed segments, animated zoom
windows, letterboxed at the generation size — and the webcam bubble for render.final to place.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_config.schemas import ScreenConfig
from ce_contracts import models as m
from ce_contracts.common import ArtifactRef
from ce_core.build import RouteDecision
from ce_core.edit.ops import EditOperation, SetShot, ShotChanges
from ce_core.enums import ShotType
from ce_core.errors import CEError
from ce_core.spec.anchors import estimate_segment_timings
from ce_core.spec.videospec import Shot, VideoSpec
from ce_core.text import tokenize
from ce_db import execution as rec
from ce_db.models.assets import Artifact, Asset
from ce_director.screen import ShotWords, plan_screen
from ce_obs import bound_context, get_logger
from ce_obs.events import EventType
from ce_render.ffmpeg import probe
from ce_render.screen import (
    ANALYSIS_VERSION,
    Keyframe,
    ZoomPlan,
    dead_time,
    extract_frames,
    frame_size,
    keyframe_times,
    pixel_changes,
    render_screen,
    scene_changes,
    text_diff,
    write_montage,
)
from ce_render.timeline import SegmentAudio, build_timeline, scene_clock
from ce_router import RouteRequest, route
from ce_storage.content import StorageRunContext

from ce_exec.context import ExecServices
from ce_exec.jobs import job_event
from ce_exec.noderun import NodeRun
from ce_exec.outputs import NodeOutput

__all__ = [
    "SUMMARY_QUESTION",
    "analyze_recording",
    "analyze_screen",
    "load_screen_analyses",
    "screen_operations",
    "screen_prepare_node",
]

_log = get_logger("ce.exec.screen")

SUMMARY_QUESTION = (
    "This is a screen recording. List what happens on screen, in order, as short events with the time "
    "(seconds from the start) each begins. Describe only what is visible."
)
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t_s": {"type": "number"}, "description": {"type": "string"}},
                "required": ["t_s", "description"],
            },
        },
    },
    "required": ["events"],
}


class ScreenAnalysisError(CEError):
    code = "screen_analysis_failed"


# ---------------------------------------------------------------------- routing


def _route(svc: ExecServices, capability: str, profile: str = "draft") -> tuple[RouteDecision | None, str]:
    """An in-process route for `capability`, or None with the reason."""
    try:
        decision = route(RouteRequest(capability=capability, routing_profile=profile), svc.catalog)
    except LookupError as exc:
        return None, f"no route: {str(exc)[:200]}"
    manifest = svc.catalog.manifests[decision.adapter_id]
    if manifest.runtime.requires_gpu:
        return None, f"{decision.adapter_id} runs on a GPU worker; screen analysis calls in-process routes only"
    return decision, ""


# ---------------------------------------------------------------------- analysis


def _events(answer: dict[str, Any], duration_s: float) -> list[dict[str, Any]]:
    """The VLM's events, kept only when well-formed (data, never instructions: I10)."""
    out: list[dict[str, Any]] = []
    for event in answer.get("events") or []:
        if not isinstance(event, dict):
            continue
        try:
            t = float(event.get("t_s", -1))
        except (TypeError, ValueError):
            continue
        text = str(event.get("description") or "").strip()
        if 0.0 <= t <= duration_s + 0.5 and text:
            out.append({"t_s": round(t, 3), "description": text[:300]})
    return sorted(out, key=lambda e: e["t_s"])[:200]


async def analyze_recording(
    svc: ExecServices, ref: ArtifactRef, path: Path, scratch: Path, cfg: ScreenConfig
) -> tuple[dict[str, Any], ArtifactRef | None]:
    """The analysis document of one recording, and the keyframe clip it OCR'd (see the module docstring)."""
    info = await probe(path)
    if not info.has_video or not info.width or not info.height or info.duration_s <= 0:
        raise ScreenAnalysisError("the recording has no video stream")
    duration = float(info.duration_s)
    changes = await scene_changes(path, threshold=cfg.scene_threshold)
    every = min(cfg.keyframe_every_s, max(1.0, duration / 12))  # short clips: about a dozen looks
    times = keyframe_times(
        changes, duration, settle_s=cfg.keyframe_settle_s, every_s=every, max_count=cfg.max_keyframes
    )
    size = frame_size(info.width, info.height, cfg.max_side)
    frames = await extract_frames(path, times, size)
    ctx = StorageRunContext(svc.content, scratch / "ctx")
    routes: dict[str, Any] = {}
    unavailable: dict[str, str] = {}

    montage_ref: ArtifactRef | None = None
    ocr: list[list[dict[str, Any]]] = [[] for _ in times]
    if frames:
        montage = await write_montage(frames, scratch / "keyframes.mp4")
        montage_ref = await ctx.write_artifact(
            montage, "video", {"screen_keyframes": True}, role="screen_keyframes", mime="video/mp4"
        )
    decision, reason = _route(svc, "vision.ocr")
    if decision is not None and montage_ref is not None:
        adapter = await svc.adapter(decision.adapter_id)
        result = await adapter.run("vision.ocr", m.OcrRequest(media=montage_ref, sampling_fps=1.0), ctx)
        for frame in result.frames:
            index = round(frame.t_s)
            if 0 <= index < len(times):
                ocr[index] = [b.model_dump(mode="json") for b in frame.boxes]
        routes["vision.ocr"] = decision.identity()
    elif decision is None:
        unavailable["vision.ocr"] = reason

    scene_starts = [0.0, *[c for c in changes if 0 < c < duration]]
    keyframes: list[Keyframe] = []
    for i, t in enumerate(times):
        frame = Keyframe(t_s=t, scene=sum(1 for c in scene_starts[1:] if c <= t), ocr=ocr[i])
        if i:
            frame.added, frame.removed, _ = text_diff(ocr[i - 1], ocr[i])
            frame.changed_regions, frame.changed_fraction = pixel_changes(frames[i - 1], frames[i])
        keyframes.append(frame)
    dead = dead_time(keyframes, changes, duration, min_s=cfg.dead_time_min_s)

    summary: dict[str, Any] = {"status": "unavailable", "events": []}
    decision, reason = _route(svc, "vision.video")
    if decision is not None:
        adapter = await svc.adapter(decision.adapter_id)
        request = m.VisionRequest(
            media=ref, question=SUMMARY_QUESTION, json_schema=SUMMARY_SCHEMA, sampling_fps=cfg.vlm_sampling_fps
        )
        answer = await adapter.run("vision.video", request, ctx)
        mock = bool(svc.catalog.manifests[decision.adapter_id].mock)
        summary = {
            "status": "mock" if mock else "ok",
            "adapter_id": decision.adapter_id,
            "confidence": round(float(answer.confidence), 3),
            "summary": str(answer.answer.get("summary") or "")[:1000],
            "events": _events(answer.answer, duration),
        }
        routes["vision.video"] = decision.identity()
    else:
        summary["reason"] = reason
        unavailable["vision.video"] = reason

    events: list[dict[str, Any]] = [{"t_s": c, "kind": "scene_change"} for c in scene_starts[1:]]
    events += [
        {"t_s": k.t_s, "kind": "text_added", "text": k.added[:10]} for k in keyframes if k.added and k.t_s > times[0]
    ]
    events += [
        {"t_s": k.t_s, "kind": "region_changed", "regions": len(k.changed_regions)}
        for k in keyframes
        if k.changed_regions
    ]
    events += [{"t_s": d["start_s"], "kind": "dead_time", "end_s": d["end_s"]} for d in dead]
    events += [{"t_s": e["t_s"], "kind": "vlm", "description": e["description"]} for e in summary["events"]]
    scenes = [
        {"index": i, "start_s": round(a, 3), "end_s": round(b, 3)}
        for i, (a, b) in enumerate(zip(scene_starts, [*scene_starts[1:], duration], strict=False))
    ]
    document = {
        "version": ANALYSIS_VERSION,
        "asset_sha256": ref.sha256,
        "duration_s": round(duration, 3),
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "scene_threshold": cfg.scene_threshold,
        "scenes": scenes,
        "keyframes": [k.as_dict() for k in keyframes],
        "keyframes_clip_sha256": montage_ref.sha256 if montage_ref else None,
        "dead_time": dead,
        "summary": summary,
        "events": sorted(events, key=lambda e: (e["t_s"], e["kind"])),
        "routes": routes,
        "unavailable": unavailable,
        "ocr_text_is_data": True,
    }
    return document, montage_ref


async def _set(
    svc: ExecServices, org_id: UUID, job_id: UUID, extra: dict[str, Any] | None = None, **values: Any
) -> None:
    async with svc.db.transaction() as session:
        job = await rec.set_job(session, org_id, job_id, **values)
        event = {**job_event(job), **(extra or {})}
    await svc.publish(org_id, EventType.JOB_UPDATED, event)


async def analyze_screen(svc: ExecServices, org_id: UUID, job_id: UUID, asset_id: UUID) -> dict[str, Any]:
    """The `screen_analysis` job: analyzes a ready `screen_recording` asset once per content and
    analysis version (a retried activity finds the stored result)."""
    cfg = svc.bundle.app.render.screen
    with bound_context(org_id=org_id, job_id=job_id, asset_id=asset_id):
        await _set(svc, org_id, job_id, status="running", progress=0.05)
        try:
            async with svc.db.session() as session:
                asset = (
                    await session.execute(sa.select(Asset).where(Asset.org_id == org_id, Asset.id == asset_id))
                ).scalar_one_or_none()
            if asset is None:
                raise ScreenAnalysisError("the asset does not exist")
            if asset.kind != "screen_recording" or asset.status != "ready" or not asset.sha256:
                raise ScreenAnalysisError(
                    f"only ready screen recordings are analyzed (this is a {asset.status} {asset.kind})"
                )
            done = dict((asset.probe or {}).get("screen_analysis") or {})
            if (
                done.get("asset_sha256") == asset.sha256
                and done.get("version") == ANALYSIS_VERSION
                and done.get("artifact_id")
            ):
                await _set(svc, org_id, job_id, {"result": done}, status="succeeded", progress=1.0)
                return {"status": "succeeded", **done}
            await svc.content.adopt(svc.settings.s3_bucket_assets, asset.storage_key, asset.sha256, mime=asset.mime)
            ref = ArtifactRef(
                sha256=asset.sha256, kind="video", mime=asset.mime, bytes=asset.bytes, role="screen_recording"
            )
            scratch = svc.scratch("screen")
            try:
                path = await svc.content.fetch(asset.sha256, scratch / "recording.mp4")
                document, montage = await analyze_recording(svc, ref, path, scratch, cfg)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            await _set(svc, org_id, job_id, status="running", progress=0.9)
            data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            sha = await svc.content.put_bytes(data, mime="application/json")
            async with svc.db.transaction() as session:
                keyframes_id = None
                if montage is not None:
                    clip = Artifact(
                        org_id=org_id,
                        kind="video",
                        storage_key=svc.content.key(montage.sha256),
                        mime="video/mp4",
                        bytes=montage.bytes or 0,
                        sha256=montage.sha256,
                        media={"role": "screen_keyframes", "asset_id": str(asset_id)},
                    )
                    session.add(clip)
                    await session.flush()
                    keyframes_id = str(clip.id)
                artifact = Artifact(
                    org_id=org_id, kind="screen_analysis", storage_key=svc.content.key(sha), mime="application/json",
                    bytes=len(data), sha256=sha, media={"asset_id": str(asset_id), "version": ANALYSIS_VERSION},
                )  # fmt: skip
                session.add(artifact)
                await session.flush()
                row = (
                    await session.execute(
                        sa.select(Asset).where(Asset.org_id == org_id, Asset.id == asset_id).with_for_update()
                    )
                ).scalar_one()
                result = {
                    "artifact_id": str(artifact.id),
                    "sha256": sha,
                    "version": ANALYSIS_VERSION,
                    "asset_sha256": asset.sha256,
                    "keyframes_artifact_id": keyframes_id,
                    "job_id": str(job_id),
                    "analyzed_at": datetime.now(UTC).isoformat(),
                    "scenes": len(document["scenes"]),
                    "keyframes": len(document["keyframes"]),
                    "summary": document["summary"]["status"],
                    "routes": {k: v.get("adapter_id") for k, v in document["routes"].items()},
                }
                row.probe = {**(row.probe or {}), "screen_analysis": result}
                kept = [artifact.id, *([UUID(keyframes_id)] if keyframes_id else [])]
                await rec.add_artifact_refs(session, org_id, kept, ref_type="asset", ref_id=str(asset_id))  # §12.10 GC
        except CEError as exc:
            _log.warning("screen analysis failed", code=exc.code, error=exc.message)
            await _set(svc, org_id, job_id, status="failed", error={"code": exc.code, "message": exc.message})
            return {"status": "failed", "code": exc.code}
        except Exception:
            _log.exception("screen analysis crashed")
            await _set(
                svc, org_id, job_id, status="failed", error={"code": "internal", "message": "screen analysis crashed"}
            )
            raise
        await _set(svc, org_id, job_id, {"result": result}, status="succeeded", progress=1.0)
        return {"status": "succeeded", **result}


async def load_screen_analyses(
    svc: ExecServices, org_id: UUID, asset_ids: Sequence[UUID]
) -> dict[UUID, dict[str, Any]]:
    """The current analysis document of each analyzed asset (missing ones are left out)."""
    if not asset_ids:
        return {}
    async with svc.db.session() as session:
        rows = (
            (await session.execute(sa.select(Asset).where(Asset.org_id == org_id, Asset.id.in_(list(asset_ids)))))
            .scalars()
            .all()
        )
    out: dict[UUID, dict[str, Any]] = {}
    for asset in rows:
        meta = (asset.probe or {}).get("screen_analysis") or {}
        if meta.get("sha256") and meta.get("asset_sha256") == asset.sha256:
            out[asset.id] = json.loads(await svc.content.read_bytes(str(meta["sha256"])))
    return out


# ---------------------------------------------------------------------- planning (edits)


def _shot_words(svc: ExecServices, spec: VideoSpec, shot: Shot, wpm: float) -> ShotWords:
    """The shot's words at estimated times, relative to its start (output seconds)."""
    cfg = svc.bundle.app.render.timeline
    order = [
        (k, spec.script.segment(k).text) for s in sorted(spec.scenes, key=lambda s: s.order) for k in s.segment_keys
    ]
    timings = estimate_segment_timings(order, wpm, start_s=cfg.lead_s, gap_s=cfg.segment_gap_s)
    segments = {}
    for key, t in timings.items():
        first = t.words[0].start_s
        segments[key] = SegmentAudio(
            t.words[-1].end_s - first, tuple((w.start_s - first, w.end_s - first) for w in t.words)
        )
    timeline = build_timeline(spec, segments, lead_s=cfg.lead_s, gap_s=cfg.segment_gap_s, tail_s=cfg.tail_s)
    slot = timeline.shots[shot.key]
    start, end = slot.span_start_s, slot.span_end_s
    words: list[tuple[str, int, str]] = []
    times: list[tuple[float, float]] = []
    for key, text in order:
        for token, (a, b) in zip(tokenize(text), timeline.word_times[key], strict=True):
            if start - 1e-6 <= a and b <= end + 1e-6:  # the words spoken over the shot
                words.append((key, token.index, token.text))
                times.append((round(a - start, 3), round(b - start, 3)))
    return ShotWords(words, times, round(end - start, 3))


def _needs_plan(shot: Shot, parent: VideoSpec) -> bool:
    if shot.screen is None or shot.screen.zooms or shot.screen.speed_segments:
        return False
    before = next((s for _, s in parent.shots() if s.key == shot.key), None)
    return before is None or before.screen is None or before.screen.asset_id != shot.screen.asset_id


async def screen_operations(
    svc: ExecServices,
    org_id: UUID,
    parent: VideoSpec,
    proposed: VideoSpec,
    wpm: float,
    operations: Sequence[EditOperation] = (),
) -> tuple[list[EditOperation], list[str]]:
    """`set_shot` operations giving each new (or re-pointed) screen shot without zooms or speed
    segments its planned zooms, speed segments and webcam bubble; and notes for the proposal.
    Shots whose zooms or speed segments an operation sets (even to none) are left as set."""
    explicit = {
        op.shot_key
        for op in operations
        if isinstance(op, SetShot) and (op.changes.zooms is not None or op.changes.speed_segments is not None)
    }
    shots = [
        (scene, shot)
        for scene, shot in proposed.shots()
        if shot.type == ShotType.SCREEN and shot.key not in explicit and _needs_plan(shot, parent)
    ]
    if not shots:
        return [], []
    analyses = await load_screen_analyses(svc, org_id, [s.screen.asset_id for _, s in shots if s.screen])
    cfg = svc.bundle.app.render.screen
    keys = set(proposed.iter_keys())
    ops: list[EditOperation] = []
    notes: list[str] = []
    for scene, shot in shots:
        assert shot.screen is not None
        analysis = analyses.get(shot.screen.asset_id)
        if analysis is None:
            notes.append(f"{shot.key}: the recording has no screen analysis yet; it plays unzoomed at 1×")
            continue
        plan = plan_screen(shot, analysis, _shot_words(svc, proposed, shot, wpm), cfg, existing_keys=keys)
        keys |= {z.key for z in plan.screen.zooms}
        ops.append(
            SetShot(
                scene_key=scene.key,
                shot_key=shot.key,
                changes=ShotChanges(
                    zooms=plan.screen.zooms,
                    speed_segments=plan.screen.speed_segments,
                    webcam_bubble=plan.screen.webcam_bubble,
                ),
            )
        )
        notes += [f"{shot.key}: {n}" for n in plan.notes]
    return ops, notes


# ---------------------------------------------------------------------- screen.prepare


async def screen_prepare_node(run: NodeRun) -> NodeOutput:
    """The screen shot on its output clock (see the module docstring). Reaction clips reuse the asset."""
    shot = run.shot
    asset = run.asset("source")
    ref = await run.adopt(asset, kind="video", role="video")
    source = await run.fetch(ref)
    info = await probe(source)
    if shot.screen is None:  # reaction clip: the source as is (its range and layout: roadmap)
        return NodeOutput(
            node_kind=run.node.kind,
            data={"reused_asset": True, "span_offset_s": 0.0, "clip_duration_s": info.duration_s, "shot_words": []},
            refs={"video": ref},
        )
    timeline_cfg = run.svc.bundle.app.render.timeline
    resolver, spans = scene_clock(run.spec, run.scene.key, run.segment_audio(), gap_s=timeline_cfg.segment_gap_s)
    start, end = spans[shot.key]
    duration = max(0.1, round(end - start, 3))

    def local(span: Any) -> tuple[float, float]:
        a, b = resolver.span(span)
        return round(max(0.0, a - start), 3), round(min(duration, b - start), 3)

    zooms = []
    for zoom in shot.screen.zooms:
        a, b = local(zoom.span)
        if b - a > 0.05:
            zooms.append(ZoomPlan(a, b, tuple(float(v) for v in zoom.rect), str(zoom.ease)))  # type: ignore[arg-type]
    speed: list[tuple[float, float, float]] = []
    for segment in sorted(shot.screen.speed_segments, key=lambda s: local(s.span)[0]):
        a, b = local(segment.span)
        if speed and a < speed[-1][1]:
            a = speed[-1][1]  # overlapping segments: the earlier one wins
        if b - a > 0.05:
            speed.append((a, b, float(segment.speed)))
    params = run.node.params
    width, height, fps = int(params["width"]), int(params["height"]), float(params["fps"])
    out = await render_screen(
        source,
        run.scratch / "screen.mp4",
        src_size=(int(info.width or width), int(info.height or height)),
        width=width,
        height=height,
        fps=fps,
        duration_s=duration,
        zooms=zooms,
        speed=speed,
    )
    video = await run.write(out, "video", role="video", mime="video/mp4")
    used = sum((b - a) * s for a, b, s in speed) + (duration - sum(b - a for a, b, _ in speed))
    bubble = shot.screen.webcam_bubble
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "span_offset_s": 0.0,
            "clip_duration_s": duration,
            "shot_words": [],
            "screen": {
                "recording_s": info.duration_s,
                "recording_used_s": round(min(used, float(info.duration_s)), 3),
                "held_s": round(max(0.0, used - float(info.duration_s)), 3),
                "zooms": [
                    {"start_s": z.start_s, "end_s": z.end_s, "rect": list(z.rect), "ease": z.ease} for z in zooms
                ],
                "speed": [{"start_s": a, "end_s": b, "speed": s} for a, b, s in speed],
                "bubble": {"enabled": bubble.enabled, "corner": str(bubble.corner), "size": float(bubble.size)}
                if bubble.enabled
                else None,
                "frame": [width, height],
                "letterboxed": not math.isclose((info.width or 1) / (info.height or 1), width / height, rel_tol=0.02),
            },
        },
        refs={"video": video},
    )
