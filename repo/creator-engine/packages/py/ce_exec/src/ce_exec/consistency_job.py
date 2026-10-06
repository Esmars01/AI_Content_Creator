"""`ConsistencyWorkflow` (§20, ADR 0056): after a version is built, each creator in its cast is
measured against their baselines and a `consistency_reports` row is written.

Stages (the studio loop, ADR 0055):
- `start`: the version's BuildManifest outputs; model calls for face identity (`face.embed` on the
  keyframes of the creator's shots and on the canonical face), voice identity (`voice.embed` on the
  creator's TTS segments and the voice references) and wardrobe (`image.embed` of each keyframe with
  the wardrobe references);
- `store`: features (`ce_qc.consistency.video_features`), the baseline (the Creator Test baseline
  and the rolling window of earlier reports, newest first), bands, verdict and intentional
  deviations (spec overrides); a `rolling` baseline row with this video's features; a sample of
  reports is queued for human "same person?" / "in character?" ratings (`human_rating_sample_rate`).

Mock analyses are labelled `mock` in every feature they feed.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import numpy as np
import sqlalchemy as sa
from ce_core.spec.videospec import VideoSpec
from ce_db.models.creators import AppearanceVersion, ConsistencyReport, CreatorBaseline, VoiceVersion
from ce_db.models.videos import VideoVersion
from ce_qc.consistency import compare, rolling_baseline, speech_features, video_features

from ce_exec.context import ExecServices
from ce_exec.creator_test import _outputs
from ce_exec.studio import ModelCall, StudioContext, StudioError, StudioStep, ref_of_asset, stage

__all__ = ["consistency_start", "consistency_store", "sampled_for_rating"]


def _cos(a: list[float], b: list[float]) -> float | None:
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if va.size == 0 or va.shape != vb.shape or not np.linalg.norm(va) or not np.linalg.norm(vb):
        return None
    return float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb)))


def sampled_for_rating(version_id: str, creator_id: str, rate: float) -> bool:
    """Deterministic sampling for the human evaluation queue (§20)."""
    digest = hashlib.sha256(f"rating:{version_id}:{creator_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 < rate


def _vector(call: dict[str, Any] | None, index: int = 0) -> list[float]:
    vectors = dict((call or {}).get("result") or {}).get("vectors") or []
    return list(vectors[index]) if len(vectors) > index else []


@stage("consistency", "start")
async def consistency_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    async with svc.db.session() as session:
        version = await session.get(VideoVersion, version_id)
        if version is None or version.org_id != org_id:
            raise StudioError("the version does not exist")
        if version.state not in ("ready", "needs_review", "approved", "partial"):
            raise StudioError(f"consistency runs on built versions (this one is {version.state})")
    vdata = await svc.version(org_id, version_id)
    spec = vdata.spec
    outputs = await _outputs(svc, org_id, version_id)
    keyframes = {k.split(":", 1)[1]: o for k, o in outputs.get("image.keyframe", []) if "image" in o.refs}
    tts = {k.split(":", 1)[1]: o for k, o in outputs.get("tts.segment", []) if "audio" in o.refs}
    calls: list[ModelCall] = []
    members: list[dict[str, Any]] = []
    async with svc.db.session() as session:
        for member in spec.cast:
            creator = vdata.refs.creator(member.creator_version_id)
            appearance_id = member.overrides.appearance_version_id or creator.appearance_version_id
            voice_id = member.overrides.voice_version_id or creator.voice_version_id
            shots = [s for sc in spec.scenes for s in sc.shots if s.character_key == member.key]
            segments = [s for s in spec.script.segments if s.speaker_key == member.key]
            face = None
            if appearance_id is not None:
                appearance = await session.get(AppearanceVersion, appearance_id)
                if appearance is not None and appearance.canonical_face_asset_id is not None:
                    face = await ref_of_asset(
                        svc, session, org_id, appearance.canonical_face_asset_id, kind="image", role="canonical_face"
                    )
            if face is not None:
                mock_face = bool(face.meta.get("mock"))
                labels = {"identity": face.sha256} if mock_face else {}
                calls.append(
                    ModelCall(
                        key=f"cons.face:{member.key}:canonical",
                        capability="face.embed",
                        prefer_mock=mock_face,
                        request={"media": face.model_dump(mode="json"), "sample_hz": 1.0, "labels": labels},
                    )
                )
                for shot in shots:
                    out = keyframes.get(shot.key)
                    if out is None:
                        continue
                    ref = out.refs["image"]
                    mock = bool(ref.meta.get("mock"))
                    calls.append(
                        ModelCall(
                            key=f"cons.face:{member.key}:{shot.key}",
                            capability="face.embed",
                            prefer_mock=mock,
                            request={
                                "media": ref.model_dump(mode="json"),
                                "sample_hz": 1.0,
                                "labels": {"identity": face.sha256} if mock else {},
                            },
                        )
                    )
            voice_refs = []
            if voice_id is not None:
                voice = await session.get(VoiceVersion, voice_id)
                for ref in (voice.references if voice else None) or []:
                    if ref.get("asset_id"):
                        voice_refs.append(
                            await ref_of_asset(
                                svc, session, org_id, UUID(str(ref["asset_id"])), kind="audio", role="voice_reference"
                            )
                        )
            if voice_refs:
                for seg in segments:
                    out = tts.get(seg.key)
                    if out is None:
                        continue
                    mock = bool(out.refs["audio"].meta.get("mock"))
                    calls.append(
                        ModelCall(
                            key=f"cons.voice:{member.key}:{seg.key}",
                            capability="voice.embed",
                            prefer_mock=mock,
                            request={
                                "audio": out.refs["audio"].model_dump(mode="json"),
                                "labels": {"identity": str(voice_id)} if mock else {},
                            },
                        )
                    )
                mock_tts = any(bool(o.refs["audio"].meta.get("mock")) for o in tts.values())
                for i, ref in enumerate(voice_refs[:3]):
                    calls.append(
                        ModelCall(
                            key=f"cons.voice_ref:{member.key}:{i}",
                            capability="voice.embed",
                            prefer_mock=mock_tts,
                            request={
                                "audio": ref.model_dump(mode="json"),
                                "labels": {"identity": str(voice_id)} if mock_tts else {},
                            },
                        )
                    )
            wardrobe = next(iter(vdata.refs.wardrobes.values()), None) if vdata.refs.wardrobes else None
            if wardrobe is not None and wardrobe.reference_assets:
                refs = [
                    (await ref_of_asset(svc, session, org_id, a.asset_id, kind="image", role="wardrobe_reference"))
                    for a in wardrobe.reference_assets[:2]
                ]
                for shot in shots:
                    out = keyframes.get(shot.key)
                    if out is None:
                        continue
                    ref = out.refs["image"]
                    calls.append(
                        ModelCall(
                            key=f"cons.wardrobe:{member.key}:{shot.key}",
                            capability="image.embed",
                            prefer_mock=bool(ref.meta.get("mock")),
                            request={
                                "images": [ref.model_dump(mode="json"), *(r.model_dump(mode="json") for r in refs)]
                            },
                        )
                    )
            members.append(
                {
                    "key": member.key,
                    "creator_id": str(creator.creator_id),
                    "creator_version_id": str(creator.version_id),
                    "signature_phrases": [
                        str(p.get("text", "")) if isinstance(p, dict) else str(p)
                        for p in dict(dict(creator.dna).get("speech") or {}).get("signature_phrases", [])
                    ],
                    "overrides": member.overrides.model_dump(mode="json", exclude_none=True),
                }
            )
    return StudioStep(calls=calls, next="store", progress=0.5, data={"members": members})


EMOTION_DIMENSIONS = ("emotion_visual", "emotion_vocal")


def _features_for(
    member: dict[str, Any], spec: VideoSpec, outputs: dict[str, list[tuple[str, Any]]], calls: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    key = member["key"]
    canonical = _vector(calls.get(f"cons.face:{key}:canonical"))
    face_calls = [c for k, c in calls.items() if k.startswith(f"cons.face:{key}:") and not k.endswith(":canonical")]
    face = [s for s in (_cos(canonical, _vector(c)) for c in face_calls) if s is not None]
    refs = [_vector(c) for k, c in calls.items() if k.startswith(f"cons.voice_ref:{key}:")]
    voice: list[float] = []
    for k, c in calls.items():
        if k.startswith(f"cons.voice:{key}:") and refs:
            scores = [s for s in (_cos(_vector(c), r) for r in refs) if s is not None]
            if scores:
                voice.append(sum(scores) / len(scores))
    wardrobe: list[float] = []
    for k, c in calls.items():
        if k.startswith(f"cons.wardrobe:{key}:"):
            vectors = dict(c.get("result") or {}).get("vectors") or []
            sims = [s for s in (_cos(vectors[0], v) for v in vectors[1:]) if s is not None] if vectors else []
            if sims:
                wardrobe.append(max(sims))
    durations = {
        k.split(":", 1)[1]: float(o.data.get("duration_s", 0.0) or 0.0) for k, o in outputs.get("tts.segment", [])
    }
    segments = [
        {
            "text": s.text,
            "duration_s": durations.get(s.key, 0.0),
            "pauses": sum(1 for a in s.annotations if "pause" in str(getattr(a, "type", "")).lower()),
        }
        for s in spec.script.segments
        if s.speaker_key == key
    ]
    tracks: list[dict[str, Any]] = []
    tracks_mock = False
    for _, out in outputs.get("behavior.observe", []):
        observed = dict(out.data.get("observed") or {})
        for analyzer in observed.get("analyzers", []):
            tracks_mock = tracks_mock or str(analyzer.get("adapter_id", "")).startswith("mock")
        tracks.extend(t for t in observed.get("tracks", []) if t.get("character_key") == key)
    # requested emotional states along the video: one per item, the visual dimension first (the CBS
    # carries `emotion_visual` and `emotion_vocal`; an item usually requests both with one state)
    states: list[str] = []
    for _, out in outputs.get("behavior.resolve", []):
        content = dict(out.data.get("content") or {})
        per_item: dict[str, str] = {}
        for control in content.get("requested_controls", []):
            dimension = control.get("dimension")
            if control.get("character_key") != key or dimension not in EMOTION_DIMENSIONS:
                continue
            item = str(control.get("item_ref"))
            if item not in per_item or dimension == "emotion_visual":
                per_item[item] = str(control.get("value"))
        states.extend(per_item.values())
    shots = {s.key for sc in spec.scenes for s in sc.shots if s.character_key == key}
    world = [
        float(o.data["environment_identity"])
        for k, o in outputs.get("qc.world", [])
        if k.split(":", 1)[1] in shots and o.data.get("environment_identity") is not None
    ]
    # the camera post's peak motion amplitude per shot (`post.camera` → `motion.peak_px`); face size
    # in frame is not measured by any analyzer yet, so `face_size_ratio` stays unmeasured
    camera = [
        float(peak)
        for k, o in outputs.get("post.camera", [])
        if k.split(":", 1)[1] in shots
        and isinstance(peak := dict(o.data.get("motion") or {}).get("peak_px"), (int, float))
    ]
    return video_features(
        face_similarity=face,
        face_mock=any(c.get("mock") for c in face_calls),
        voice_similarity=voice,
        voice_mock=any(c.get("mock") for k, c in calls.items() if k.startswith(f"cons.voice:{key}:")),
        speech=speech_features(segments, member.get("signature_phrases", [])),
        tracks=tracks,
        tracks_mock=tracks_mock,
        emotion_states=states,
        world_scores=world,
        wardrobe_similarity=wardrobe,
        wardrobe_mock=any(c.get("mock") for k, c in calls.items() if k.startswith(f"cons.wardrobe:{key}:")),
        camera_motion=camera,
    )


@stage("consistency", "store")
async def consistency_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    config = svc.bundle.qc_consistency
    if config is None:
        raise StudioError("config/qc/consistency.yaml is missing")
    vdata = await svc.version(org_id, version_id)
    outputs = await _outputs(svc, org_id, version_id)
    calls = {o["key"]: o for o in data.get("outputs", [])}
    reports: list[str] = []
    async with svc.db.transaction() as session:
        for member in data.get("members", []):
            creator_version_id = UUID(member["creator_version_id"])
            features = _features_for(member, vdata.spec, outputs, calls)
            rows = (
                (
                    await session.execute(
                        sa.select(CreatorBaseline)
                        .where(
                            CreatorBaseline.org_id == org_id, CreatorBaseline.creator_version_id == creator_version_id
                        )
                        .order_by(CreatorBaseline.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            snapshots = [
                dict(r.stats.get("features") or r.stats)
                for r in rows
                if str(r.stats.get("version_id", "")) != str(version_id)
            ]
            baseline = rolling_baseline(snapshots, config.baseline_window_videos)
            overrides = dict(member.get("overrides") or {})
            deviations = [
                {"dimension": dim, "reason": f"spec override: {field}"}
                for field, dim in (("voice_version_id", "voice_identity"), ("appearance_version_id", "face_identity"))
                if overrides.get(field)
            ]
            metrics, verdict = compare(features, baseline, config, deviations=deviations)
            rating = sampled_for_rating(str(version_id), member["creator_id"], config.human_rating_sample_rate)
            await session.execute(
                sa.delete(ConsistencyReport).where(
                    ConsistencyReport.org_id == org_id,
                    ConsistencyReport.version_id == version_id,
                    ConsistencyReport.creator_id == UUID(member["creator_id"]),
                )
            )
            report = ConsistencyReport(
                org_id=org_id,
                creator_id=UUID(member["creator_id"]),
                creator_version_id=creator_version_id,
                version_id=version_id,
                metrics={
                    "dimensions": metrics,
                    "features": features,
                    "baseline": baseline,
                    "baseline_videos": len(snapshots),
                    "character_key": member["key"],
                    "rating_requested": rating,
                    "job_id": ctx.job_id,
                },
                verdict=verdict,
                deviations=deviations,
            )
            session.add(report)
            await session.flush()
            reports.append(str(report.id))
            await session.execute(
                sa.delete(CreatorBaseline).where(
                    CreatorBaseline.org_id == org_id,
                    CreatorBaseline.creator_version_id == creator_version_id,
                    CreatorBaseline.source == "rolling",
                    CreatorBaseline.stats["version_id"].astext == str(version_id),
                )
            )
            session.add(
                CreatorBaseline(
                    org_id=org_id,
                    creator_version_id=creator_version_id,
                    source="rolling",
                    stats={"version_id": str(version_id), "features": features, "report_id": str(report.id)},
                    window_n=min(len(snapshots) + 1, config.baseline_window_videos),
                )
            )
    return StudioStep(done=True, result={"reports": reports, "version_id": ctx.target_id})


async def enqueue_after_build(svc: ExecServices, org_id: UUID, version_id: UUID, state: str) -> dict[str, Any] | None:
    """The `consistency` job a finished build starts (§20: "runs as a job after a version reaches
    ready"), as the `StudioContext` of its workflow; None when it does not apply: the setting is
    off, the build did not finish, the cast has no creator, or the video is a Creator Test (its
    own scorecard and baseline cover it)."""
    from ce_db.models.assets import GenerationJob
    from ce_db.models.videos import Project, Video

    if not svc.bundle.app.qc.consistency_after_build or state not in ("ready", "needs_review"):
        return None
    async with svc.db.transaction() as session:
        version = await session.get(VideoVersion, version_id)
        if version is None or not (version.spec or {}).get("cast"):
            return None
        video = await session.get(Video, version.video_id)
        project = await session.get(Project, video.project_id) if video is not None else None
        if project is not None and project.name == svc.bundle.app.studio.creator_test_project:
            return None
        job = GenerationJob(
            org_id=org_id,
            kind="consistency",
            status="queued",
            target_type="video_version",
            target_id=version_id,
            input={"after_build": True},
            video_version_id=version_id,
        )
        session.add(job)
        await session.flush()
        job.temporal_workflow_id = f"consistency-{job.id}"
        build = svc.bundle.app.build
        return StudioContext(
            kind="consistency",
            org_id=str(org_id),
            job_id=str(job.id),
            target_id=str(version_id),
            model_timeout_s=float(build.model_node_timeout_s),
            cpu_timeout_s=float(build.cpu_node_timeout_s),
        ).model_dump(mode="json")
