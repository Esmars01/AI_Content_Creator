"""The Creator Test (§17.4) as studio stages: the fixed test video (`ce_creator.test_script`) is
built by the regular `GenerateVersionWorkflow` (a child of `CreatorTestWorkflow`) in the org's
"Creator tests" project; the scoring calls (identity and speaker embeddings, speech quality, a VLM
critique) then run like any studio call, and `ce_creator.scorecard` turns everything into the
scorecard stored in `creator_tests` and a `creator_baselines` row (§20). Drafts can be tested: the
row records exactly which creator, appearance, voice and world versions were tested.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.enums import CameraPositionStatus
from ce_core.identity.world import WorldDNA
from ce_core.text.tokenize import tokenize
from ce_creator.scorecard import baseline_stats, cosine, scorecard
from ce_creator.test_script import SEGMENTS, TEST_SCRIPT_VERSION, creator_test_spec
from ce_db.models.assets import Artifact
from ce_db.models.creators import (
    AppearanceVersion,
    CreatorBaseline,
    CreatorTest,
    CreatorVersion,
    VoiceVersion,
)
from ce_db.models.videos import BuildManifestEntry, Project
from ce_db.models.worlds import World, WorldVersion

from ce_exec.context import ExecServices
from ce_exec.studio import ModelCall, StudioContext, StudioError, StudioStep, ref_of_asset, stage
from ce_exec.submit import submit_spec

__all__ = ["creator_test_world_binding"]

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "eyes": {"type": "number", "description": "0–1, natural eyes and gaze"},
        "teeth": {"type": "number", "description": "0–1, natural teeth and mouth"},
        "hands": {"type": "number", "description": "0–1, natural hands (1 if none visible)"},
        "naturalness": {"type": "number", "description": "0–1, overall naturalness"},
        "notes": {"type": "string"},
    },
}


def creator_test_world_binding(dna: WorldDNA, world_version_id: UUID) -> dict[str, Any]:
    position = next(c for c in dna.camera_positions if c.status == CameraPositionStatus.PERMITTED)
    return {
        "world_version_id": str(world_version_id),
        "camera_position_key": position.key,
        "time_of_day": str(dna.time_and_weather.default_time_of_day),
        "weather": str(dna.time_and_weather.default_weather),
        "overrides": {
            "element_states": {},
            "hide_elements": [],
            "add_elements": [],
            "move_elements": [],
            "lighting": None,
            "acoustics": None,
        },
        "continuity_ref": {"kind": "world_plate", "camera_position_key": position.key},
    }


async def _test_project(session: Any, org_id: UUID, name: str) -> UUID:
    project_id = (
        await session.execute(sa.select(Project.id).where(Project.org_id == org_id, Project.name == name).limit(1))
    ).scalar_one_or_none()
    if project_id is None:
        project = Project(org_id=org_id, name=name)
        session.add(project)
        await session.flush()
        project_id = project.id
    return UUID(str(project_id))


@stage("creator_test", "start")
async def creator_test_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = UUID(ctx.org_id)
    async with svc.db.transaction() as session:
        test = await session.get_one(CreatorTest, UUID(ctx.target_id))
        version = await session.get_one(CreatorVersion, test.creator_version_id)
        if version.org_id != org_id:
            raise StudioError("creator version not found")
        world_version_id = test.world_version_id
        if world_version_id is None:
            for world_id in version.default_world_ids or []:
                world = await session.get(World, world_id)
                if world is not None and world.current_version_id is not None:
                    world_version_id = world.current_version_id
                    break
        if world_version_id is None:
            raise StudioError("the creator has no default world with an approved version; pick a world for the test")
        world_version = await session.get_one(WorldVersion, world_version_id)
        binding = creator_test_world_binding(WorldDNA.model_validate(world_version.dna), world_version_id)
        project_id = await _test_project(session, org_id, svc.bundle.app.studio.creator_test_project)
        test.world_version_id = world_version_id
        test.appearance_version_id = test.appearance_version_id or version.appearance_version_id
        test.voice_version_id = test.voice_version_id or version.voice_version_id
        wardrobes = list(version.default_wardrobe_version_ids or [])
        wardrobe = wardrobes[0] if wardrobes else None
        language = str(ctx.args.get("language") or "en-US")
    spec = creator_test_spec(
        video_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        creator_version_id=str(version.id),
        world=binding,
        wardrobe_version_id=str(wardrobe) if wardrobe else None,
        vocab_version=svc.bundle.vocab.version,
        language=language,
        seed_namespace=str(uuid.uuid5(uuid.NAMESPACE_URL, f"creator-test:{ctx.target_id}")),
    )
    submitted = await submit_spec(
        svc,
        org_id=org_id,
        project_id=project_id,
        spec_data=spec,
        user_id=UUID(ctx.user_id) if ctx.user_id else None,
    )
    async with svc.db.transaction() as session:
        test = await session.get_one(CreatorTest, UUID(ctx.target_id))
        test.scorecard = {
            "status": "building",
            "video_id": str(submitted.video_id),
            "version_id": str(submitted.version_id),
            "script": TEST_SCRIPT_VERSION,
        }
    return StudioStep(
        build={"version_id": str(submitted.version_id), "job_id": str(submitted.job_id)},
        next="score",
        progress=0.15,
        data={"version_id": str(submitted.version_id), "video_id": str(submitted.video_id), "language": language},
    )


async def _outputs(svc: ExecServices, org_id: UUID, version_id: UUID) -> dict[str, list[tuple[str, Any]]]:
    """Node kind → [(node key, NodeOutput)] from the version's BuildManifest."""
    async with svc.db.session() as session:
        rows = (
            await session.execute(
                sa.select(BuildManifestEntry.node_key, Artifact.sha256)
                .join(Artifact, Artifact.id == BuildManifestEntry.artifact_id)
                .where(BuildManifestEntry.org_id == org_id, BuildManifestEntry.version_id == version_id)
            )
        ).all()
    out: dict[str, list[tuple[str, Any]]] = {}
    for key, sha in rows:
        kind = str(key).split(":", 1)[0]
        out.setdefault(kind, []).append((str(key), await svc.docs.output(sha)))
    return out


@stage("creator_test", "score")
async def creator_test_score(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = UUID(ctx.org_id)
    build = dict(data.get("build") or {})
    outputs = await _outputs(svc, org_id, UUID(data["version_id"]))
    async with svc.db.transaction() as session:
        test = await session.get_one(CreatorTest, UUID(ctx.target_id))
        face = None
        if test.appearance_version_id is not None:
            appearance = await session.get(AppearanceVersion, test.appearance_version_id)
            if appearance is not None and appearance.canonical_face_asset_id is not None:
                face = await ref_of_asset(
                    svc, session, org_id, appearance.canonical_face_asset_id, kind="image", role="canonical_face"
                )
        voice_refs = []
        if test.voice_version_id is not None:
            voice = await session.get(VoiceVersion, test.voice_version_id)
            for ref in (voice.references if voice else None) or []:
                if ref.get("asset_id"):
                    reference = await ref_of_asset(
                        svc, session, org_id, UUID(str(ref["asset_id"])), kind="audio", role="voice_reference"
                    )
                    voice_refs.append(reference.model_dump(mode="json"))
    calls: list[ModelCall] = []
    keyframes = [(k, o.refs["image"]) for k, o in outputs.get("image.keyframe", []) if "image" in o.refs]
    audio = [(k, o.refs["audio"]) for k, o in outputs.get("tts.segment", []) if "audio" in o.refs]
    if face is not None:
        face_json = face.model_dump(mode="json")
        mock_face = bool(face.meta.get("mock"))
        calls.append(
            ModelCall(
                key="test.face:canonical",
                capability="face.embed",
                prefer_mock=mock_face,
                request={
                    "media": face_json,
                    "sample_hz": 1.0,
                    "labels": {"identity": face.sha256} if mock_face else {},
                },
            )
        )
        for key, ref in keyframes:
            mock = bool(ref.meta.get("mock"))
            calls.append(
                ModelCall(
                    key=f"test.face:{key}",
                    capability="face.embed",
                    prefer_mock=mock,
                    request={
                        "media": ref.model_dump(mode="json"),
                        "sample_hz": 1.0,
                        "labels": {"identity": face.sha256} if mock else {},
                    },
                )
            )
    for key, ref in audio:
        mock = bool(ref.meta.get("mock"))
        calls.append(
            ModelCall(
                key=f"test.quality:{key}",
                capability="qc.speech_quality",
                prefer_mock=mock,
                request={"media": ref.model_dump(mode="json")},
            )
        )
        if voice_refs:
            calls.append(
                ModelCall(
                    key=f"test.voice:{key}",
                    capability="voice.embed",
                    prefer_mock=mock,
                    request={
                        "audio": ref.model_dump(mode="json"),
                        "labels": {"identity": str(test.voice_version_id)} if mock else {},
                    },
                )
            )
    for i, ref_json in enumerate(voice_refs[:3]):
        calls.append(
            ModelCall(
                key=f"test.voice_ref:{i}",
                capability="voice.embed",
                prefer_mock=bool(audio and audio[0][1].meta.get("mock")),
                request={
                    "audio": ref_json,
                    "labels": {"identity": str(test.voice_version_id)}
                    if audio and audio[0][1].meta.get("mock")
                    else {},
                },
            )
        )
    if keyframes:
        key, ref = keyframes[0]
        calls.append(
            ModelCall(
                key="test.critique",
                capability="vision.image",
                prefer_mock=bool(ref.meta.get("mock")),
                request={
                    "media": ref.model_dump(mode="json"),
                    "json_schema": CRITIQUE_SCHEMA,
                    "question": "Critique this frame of a talking-head video: eyes, teeth, hands "
                    "and overall naturalness, each 0–1.",
                },
            )
        )
    return StudioStep(
        calls=calls,
        next="store",
        progress=0.8,
        data={
            **{k: v for k, v in data.items() if k != "outputs"},
            "build": build,
            "has_face": face is not None,
            "has_voice_refs": bool(voice_refs),
        },
    )


@stage("creator_test", "store")
async def creator_test_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = UUID(ctx.org_id)
    build = dict(data.get("build") or {})
    outputs = await _outputs(svc, org_id, UUID(data["version_id"]))
    calls = {o["key"]: o for o in data["outputs"]}
    canonical = (calls.get("test.face:canonical", {}).get("result", {}).get("vectors") or [[]])[0]
    face_sim = [
        cosine(canonical, (o["result"]["vectors"] or [[]])[0])
        for k, o in calls.items()
        if k.startswith("test.face:") and k != "test.face:canonical"
    ]
    refs = [(o["result"]["vectors"] or [[]])[0] for k, o in calls.items() if k.startswith("test.voice_ref:")]
    voice_sim = []
    for k, o in calls.items():
        if k.startswith("test.voice:") and refs:
            vector = (o["result"]["vectors"] or [[]])[0]
            scores = [s for s in (cosine(vector, r) for r in refs) if s is not None]
            voice_sim.append(round(sum(scores) / len(scores), 4) if scores else None)
    quality = [{**o["result"], "mock": o["mock"]} for k, o in calls.items() if k.startswith("test.quality:")]
    critique_call = calls.get("test.critique")
    critique = (
        {
            **dict(critique_call["result"].get("answer") or {}),
            "confidence": critique_call["result"].get("confidence"),
            "adapter_id": critique_call["adapter_id"],
            "mock": critique_call["mock"],
        }
        if critique_call
        else None
    )
    texts = dict(SEGMENTS)
    tts = [
        {
            "words": len(tokenize(texts.get(key.split(":", 1)[1], ""))),
            "duration_s": float(o.data.get("duration_s", 0.0)),
        }
        for key, o in outputs.get("tts.segment", [])
    ]
    coverage = outputs.get("behavior.coverage", [(None, None)])[0][1]
    card = scorecard(
        build_state=str(build.get("state", "unknown")),
        verify=[o.data for _, o in outputs.get("asr.verify", [])],
        tts=tts,
        qc_shots=[o.data for _, o in outputs.get("qc.shot", [])],
        observations=[o.data for _, o in outputs.get("behavior.observe", [])],
        qc_world=[o.data for _, o in outputs.get("qc.world", [])],
        coverage=coverage.data if coverage is not None else None,
        face_similarity=face_sim if data.get("has_face") else [],
        face_mock=any(o["mock"] for k, o in calls.items() if k.startswith("test.face:")),
        voice_similarity=voice_sim,
        voice_mock=any(o["mock"] for k, o in calls.items() if k.startswith("test.voice")),
        voice_basis="voice.embed cosine vs the voice references"
        if data.get("has_voice_refs")
        else "the voice version has no reference audio to compare with",
        speech_quality=quality,
        critique=critique,
    )
    card.update(
        {
            "status": "scored",
            "script": TEST_SCRIPT_VERSION,
            "video_id": data["video_id"],
            "version_id": data["version_id"],
        }
    )
    render = outputs.get("render.final", [(None, None)])[0][1]
    async with svc.db.transaction() as session:
        test = await session.get_one(CreatorTest, UUID(ctx.target_id))
        test.scorecard = card
        test.coverage_report = coverage.data.get("report", {}) if coverage is not None else {}
        if render is not None and "video" in render.refs:
            artifact = (
                (
                    await session.execute(
                        sa.select(Artifact.id).where(
                            Artifact.org_id == org_id, Artifact.sha256 == render.refs["video"].sha256
                        )
                    )
                )
                .scalars()
                .first()
            )
            test.render_artifact_id = artifact
        baseline = CreatorBaseline(
            org_id=org_id,
            creator_version_id=test.creator_version_id,
            source="creator_test",
            stats={**baseline_stats(card), "creator_test_id": str(test.id), "script": TEST_SCRIPT_VERSION},
            window_n=1,
        )
        session.add(baseline)
        await session.flush()
        baseline_id = baseline.id
    if build.get("state") not in ("ready", "needs_review"):  # flagged videos are still scored (§26)
        raise StudioError(f"the test video did not build ({build.get('state')}); the partial scorecard is stored")
    return StudioStep(
        done=True,
        result={"creator_test_id": ctx.target_id, "baseline_id": str(baseline_id), "build_state": build.get("state")},
    )
