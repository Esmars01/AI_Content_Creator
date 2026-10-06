"""The stages of the studio jobs (Phase 10; `ce_exec.studio` explains the loop).

- `identity_pack`, mode `candidates` (draft appearance version): face candidates (`image.generate`)
  become assets in `identity_pack.candidates`.
- `identity_pack`, mode `expand` (a canonical face is chosen): the VLM apparent age (`vision.image`)
  and the angles and expressions (`image.edit`, always conditioned on the canonical face, never
  chained), then identity scores (`face.embed`) → `identity_pack.images`, `age_checks.vlm_estimate`.
- `wardrobe_refs` (draft wardrobe version): views (`image.edit` on the canonical face) and their
  scores (`face.embed`) → `reference_asset_ids`.
- `voice_design` (voice): `voice.design` → `voice_candidates`.
- `voice_test` (voice version): conditioning (`voice.clone_prepare`, with references) → `voice.tts`
  → ASR, speech quality, speaker embeddings → WER, WPM, quality, similarity; a draft records the
  WPM a real engine measured (WPM calibration, §21).
- `world_plates`, mode `generate` (draft world version): `image.generate` per permitted position ×
  time of day × weather → `plate_candidates`.
- `world_plates`, mode `fingerprint`: `image.embed` per chosen plate + colour and lighting
  statistics → the fingerprints document (`fingerprints_artifact_id`).
- `creator_test`: `ce_exec.creator_test`.

Mock engines are honest about being mocks: their outputs carry `mock` and the assets are tagged
`mock`; analyses of mock media are routed to mock analyzers (`prefer_mock`, D84), and the mock
face embedding of an image conditioned on the canonical face is computed for that identity.
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_contracts.common import ArtifactRef
from ce_core.enums import CameraPositionStatus
from ce_core.identity.creator import AppearanceDNA, WardrobeSpec
from ce_core.identity.world import WorldDNA
from ce_db.models.creators import (
    Appearance,
    AppearanceVersion,
    Creator,
    CreatorVersion,
    Voice,
    VoiceCandidate,
    VoiceVersion,
    Wardrobe,
    WardrobeVersion,
)
from ce_db.models.worlds import WorldVersion
from ce_world.plates import plate_prompt, plate_statistics

from ce_exec.context import ExecServices
from ce_exec.studio import (
    ModelCall,
    StudioContext,
    StudioError,
    StudioStep,
    asset_from_ref,
    ref_of_asset,
    stage,
)

__all__ = ["cosine"]

AGE_SCHEMA = {
    "type": "object",
    "properties": {"apparent_age": {"type": "number", "description": "apparent age in years"}},
    "required": ["apparent_age"],
}


def cosine(a: list[float], b: list[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return round(sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb), 4)


def _uuid(value: Any) -> UUID:
    return UUID(str(value))


def _studio(svc: ExecServices) -> Any:
    return svc.bundle.app.studio


def _is_mock(ref: dict[str, Any] | ArtifactRef) -> bool:
    meta = ref.meta if isinstance(ref, ArtifactRef) else dict(ref.get("meta") or {})
    return bool(meta.get("mock"))


async def _draft(session: Any, model: Any, org_id: UUID, row_id: UUID, what: str) -> Any:
    row = (
        await session.execute(sa.select(model).where(model.org_id == org_id, model.id == row_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise StudioError(f"{what} {row_id} not found")
    if row.status != "draft":
        raise StudioError(f"{what} {row_id} is {row.status}: studio jobs only change drafts (I3)")
    return row


def _by_key(outputs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {o["key"]: o for o in outputs}


# ====================================================================== identity pack
def _face_prompt(dna: AppearanceDNA) -> str:
    parts = [
        f"portrait photo of an adult, apparent age {dna.age_appearance}",
        dna.face_shape and f"{dna.face_shape} face",
        dna.skin and f"{dna.skin} skin",
        dna.hair and f"{dna.hair} hair",
        dna.eyes and f"{dna.eyes} eyes",
        ", ".join(dna.distinctive_features),
        dna.grooming_style,
        dna.body_type and f"{dna.body_type} build",
        "neutral background, soft even light, looking at the camera",
    ]
    return ", ".join(p for p in parts if p)


@stage("identity_pack", "start")
async def identity_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    cfg = _studio(svc)
    org_id = _uuid(ctx.org_id)
    async with svc.db.transaction() as session:
        row = await _draft(session, AppearanceVersion, org_id, _uuid(ctx.target_id), "appearance version")
        dna = AppearanceDNA.model_validate(row.dna)
        if ctx.args.get("mode", "candidates") == "candidates":
            count = int(ctx.args.get("candidates") or cfg.candidates_default)
            prompt = _face_prompt(dna)
            calls = [
                ModelCall(
                    key=f"identity.candidate:{i + 1}",
                    capability="image.generate",
                    seed=i + 1,
                    request={
                        "prompt": prompt,
                        "width": cfg.face_size,
                        "height": cfg.face_size,
                        "negative": "child, minor, teenager, text, watermark",
                        "labels": {"kind": "face_candidate", "candidate": str(i + 1)},
                    },
                )
                for i in range(count)
            ]
            return StudioStep(calls=calls, next="candidates_store", progress=0.1, data={"prompt": prompt})
        if row.canonical_face_asset_id is None:
            raise StudioError("choose a canonical face before expanding the identity pack")
        face = await ref_of_asset(
            svc, session, org_id, row.canonical_face_asset_id, kind="image", role="canonical_face"
        )
    mock = _is_mock(face)
    face_json = face.model_dump(mode="json")
    calls = [
        ModelCall(
            key="identity.age_check",
            capability="vision.image",
            prefer_mock=mock,
            request={
                "media": face_json,
                "json_schema": AGE_SCHEMA,
                "question": "Estimate the apparent age in years of the person in this image. Answer conservatively.",
            },
        )
    ]
    views = [("angle", a) for a in cfg.angles] + [("expression", e) for e in cfg.expressions]
    for index, (kind, label) in enumerate(views):
        calls.append(
            ModelCall(
                key=f"identity.{kind}:{label}",
                capability="image.edit",
                seed=101 + index,
                prefer_mock=mock,
                request={
                    "image": face_json,
                    "references": [face_json],
                    "width": cfg.face_size,
                    "height": cfg.face_size,
                    "prompt": f"the same person, {label.replace('_', ' ')} "
                    f"{'view' if kind == 'angle' else 'expression'}, identical face, same lighting and background",
                    "labels": {
                        "kind": "identity_expansion",
                        kind: label,
                        **({"expression": label} if kind == "expression" else {}),
                    },
                },
            )
        )
    return StudioStep(calls=calls, next="expand_score", progress=0.1, data={"face": face_json, "views": views})


@stage("identity_pack", "candidates_store")
async def identity_candidates_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = _uuid(ctx.org_id)
    candidates = []
    async with svc.db.transaction() as session:
        row = await _draft(session, AppearanceVersion, org_id, _uuid(ctx.target_id), "appearance version")
        for output in data["outputs"]:
            image = output["result"]["image"]
            asset = await asset_from_ref(
                svc,
                session,
                org_id,
                image,
                tags=["identity_pack", "face_candidate"],
                rights={"job_id": ctx.job_id, "adapter_id": output["adapter_id"]},
                user_id=_uuid(ctx.user_id) if ctx.user_id else None,
            )
            candidates.append(
                {
                    "asset_id": str(asset.id),
                    "sha256": asset.sha256,
                    "adapter_id": output["adapter_id"],
                    "mock": output["mock"],
                    "seed": int(output["key"].rsplit(":", 1)[1]),
                }
            )
        pack = dict(row.identity_pack or {})
        pack["candidates"] = candidates
        pack["candidates_job_id"] = ctx.job_id
        pack["prompt"] = data.get("prompt", "")
        row.identity_pack = pack
    return StudioStep(done=True, result={"candidates": candidates})


@stage("identity_pack", "expand_score")
async def identity_expand_score(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = _uuid(ctx.org_id)
    outputs = _by_key(data["outputs"])
    face = data["face"]
    mock_face = _is_mock(face)
    images = []
    async with svc.db.transaction() as session:
        await _draft(session, AppearanceVersion, org_id, _uuid(ctx.target_id), "appearance version")
        for kind, label in data["views"]:
            output = outputs[f"identity.{kind}:{label}"]
            asset = await asset_from_ref(
                svc,
                session,
                org_id,
                output["result"]["image"],
                tags=["identity_pack", f"identity_{kind}"],
                rights={"job_id": ctx.job_id, "adapter_id": output["adapter_id"], "conditioned_on": face["sha256"]},
                user_id=_uuid(ctx.user_id) if ctx.user_id else None,
            )
            images.append(
                {
                    "asset_id": str(asset.id),
                    "sha256": asset.sha256,
                    "kind": kind,
                    "label": label,
                    "adapter_id": output["adapter_id"],
                    "mock": output["mock"],
                    "decision": "pending",
                    "image": output["result"]["image"],
                }
            )

    def embed_call(key: str, image: dict[str, Any], mock: bool) -> ModelCall:
        labels = {"identity": face["sha256"]} if mock else {}
        return ModelCall(
            key=key,
            capability="face.embed",
            prefer_mock=mock,
            request={"media": image, "sample_hz": 1.0, "labels": labels},
        )

    calls = [embed_call("identity.embed:canonical", face, mock_face)]
    calls += [embed_call(f"identity.embed:{i['kind']}:{i['label']}", i["image"], i["mock"]) for i in images]
    age = outputs["identity.age_check"]
    return StudioStep(calls=calls, next="expand_store", progress=0.6, data={"images": images, "age": age})


@stage("identity_pack", "expand_store")
async def identity_expand_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = _uuid(ctx.org_id)
    outputs = _by_key(data["outputs"])
    canonical = (outputs["identity.embed:canonical"]["result"]["vectors"] or [[]])[0]
    images = []
    for image in data["images"]:
        vectors = outputs[f"identity.embed:{image['kind']}:{image['label']}"]["result"]["vectors"]
        similarity = cosine(canonical, vectors[0]) if vectors else None
        images.append({k: v for k, v in image.items() if k != "image"} | {"similarity": similarity})
    age = data["age"]
    answer = dict(age["result"].get("answer") or {})
    estimate = answer.get("apparent_age")
    async with svc.db.transaction() as session:
        row = await _draft(session, AppearanceVersion, org_id, _uuid(ctx.target_id), "appearance version")
        dna = AppearanceDNA.model_validate(row.dna)
        pack = dict(row.identity_pack or {})
        pack["images"] = images
        pack["expansion_job_id"] = ctx.job_id
        pack["canonical_asset_id"] = str(row.canonical_face_asset_id)
        row.identity_pack = pack
        checks = dict(row.age_checks or {})
        checks["dna_age_appearance"] = dna.age_appearance
        checks["vlm_estimate"] = float(estimate) if isinstance(estimate, int | float) else None
        checks["vlm_adapter"] = age["adapter_id"]
        checks["vlm_mock"] = bool(age["mock"])
        checks["vlm_confidence"] = age["result"].get("confidence")
        checks["vlm_job_id"] = ctx.job_id
        checks["threshold"] = float(_studio(svc).min_vlm_age_estimate)
        row.age_checks = checks
    scores = [i["similarity"] for i in images if i["similarity"] is not None]
    return StudioStep(
        done=True,
        result={
            "images": len(images),
            "vlm_estimate": checks["vlm_estimate"],
            "similarity_min": min(scores) if scores else None,
            "similarity_mean": round(sum(scores) / len(scores), 4) if scores else None,
        },
    )


# ====================================================================== wardrobe references
async def _canonical_face(svc: ExecServices, session: Any, org_id: UUID, creator_id: UUID) -> ArtifactRef:
    creator = (
        await session.execute(sa.select(Creator).where(Creator.org_id == org_id, Creator.id == creator_id))
    ).scalar_one()
    appearance_version_id = None
    if creator.current_version_id is not None:
        current = await session.get(CreatorVersion, creator.current_version_id)
        appearance_version_id = current.appearance_version_id if current else None
    if appearance_version_id is None:  # the latest approved appearance of the creator
        appearance_version_id = (
            await session.execute(
                sa.select(AppearanceVersion.id)
                .join(Appearance, Appearance.id == AppearanceVersion.appearance_id)
                .where(
                    Appearance.org_id == org_id,
                    Appearance.creator_id == creator_id,
                    AppearanceVersion.status == "approved",
                )
                .order_by(AppearanceVersion.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    appearance = await session.get(AppearanceVersion, appearance_version_id) if appearance_version_id else None
    if appearance is None or appearance.canonical_face_asset_id is None:
        raise StudioError("the creator has no approved appearance with a canonical face (§17.2)")
    return await ref_of_asset(
        svc, session, org_id, appearance.canonical_face_asset_id, kind="image", role="canonical_face"
    )


@stage("wardrobe_refs", "start")
async def wardrobe_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    cfg = _studio(svc)
    org_id = _uuid(ctx.org_id)
    async with svc.db.transaction() as session:
        row = await _draft(session, WardrobeVersion, org_id, _uuid(ctx.target_id), "wardrobe version")
        wardrobe = await session.get_one(Wardrobe, row.wardrobe_id)
        spec = WardrobeSpec.model_validate(row.spec)
        face = await _canonical_face(svc, session, org_id, wardrobe.creator_id)
    mock = _is_mock(face)
    face_json = face.model_dump(mode="json")
    outfit = ", ".join(p for p in (spec.name, spec.description, ", ".join(spec.style_tags)) if p)
    calls = [
        ModelCall(
            key=f"wardrobe.view:{view}",
            capability="image.edit",
            seed=201 + i,
            prefer_mock=mock,
            request={
                "image": face_json,
                "references": [face_json],
                "width": cfg.face_size,
                "height": cfg.face_size,
                "prompt": f"the same person wearing {outfit}, {view.replace('_', ' ')} view, identical face",
                "labels": {"kind": "wardrobe_reference", "view": view},
            },
        )
        for i, view in enumerate(cfg.wardrobe_views)
    ]
    return StudioStep(calls=calls, next="score", progress=0.1, data={"face": face_json})


@stage("wardrobe_refs", "score")
async def wardrobe_score(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    face = data["face"]
    refs = [
        {
            "view": o["key"].split(":", 1)[1],
            "image": o["result"]["image"],
            "adapter_id": o["adapter_id"],
            "mock": o["mock"],
        }
        for o in data["outputs"]
    ]
    labels = {"identity": face["sha256"]}
    calls = [
        ModelCall(
            key="wardrobe.embed:canonical",
            capability="face.embed",
            prefer_mock=_is_mock(face),
            request={"media": face, "sample_hz": 1.0, "labels": labels if _is_mock(face) else {}},
        )
    ]
    calls += [
        ModelCall(
            key=f"wardrobe.embed:{r['view']}",
            capability="face.embed",
            prefer_mock=r["mock"],
            request={"media": r["image"], "sample_hz": 1.0, "labels": labels if r["mock"] else {}},
        )
        for r in refs
    ]
    return StudioStep(calls=calls, next="store", progress=0.6, data={"refs": refs})


@stage("wardrobe_refs", "store")
async def wardrobe_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = _uuid(ctx.org_id)
    outputs = _by_key(data["outputs"])
    canonical = (outputs["wardrobe.embed:canonical"]["result"]["vectors"] or [[]])[0]
    stored = []
    async with svc.db.transaction() as session:
        row = await _draft(session, WardrobeVersion, org_id, _uuid(ctx.target_id), "wardrobe version")
        for ref in data["refs"]:
            asset = await asset_from_ref(
                svc,
                session,
                org_id,
                ref["image"],
                tags=["wardrobe_reference"],
                rights={"job_id": ctx.job_id, "adapter_id": ref["adapter_id"]},
                user_id=_uuid(ctx.user_id) if ctx.user_id else None,
            )
            vectors = outputs[f"wardrobe.embed:{ref['view']}"]["result"]["vectors"]
            stored.append(
                {
                    "asset_id": str(asset.id),
                    "view": ref["view"],
                    "mock": ref["mock"],
                    "similarity": cosine(canonical, vectors[0]) if vectors else None,
                }
            )
        ids = [_uuid(s["asset_id"]) for s in stored]
        row.reference_asset_ids = ids
        spec = dict(row.spec or {})
        spec["reference_asset_ids"] = [str(i) for i in ids]
        row.spec = spec
    return StudioStep(done=True, result={"references": stored})


# ====================================================================== voices
@stage("voice_design", "start")
async def voice_design_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    args = ctx.args
    call = ModelCall(
        key="voice.design",
        capability="voice.design",
        language=str(args["language"]),
        seed=1,
        request={
            "description": str(args["description"]),
            "language": str(args["language"]),
            "sample_text": str(args.get("sample_text") or _studio(svc).voice_test_text),
            "count": int(args.get("count") or 4),
        },
    )
    return StudioStep(calls=[call], next="store", progress=0.1)


@stage("voice_design", "store")
async def voice_design_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = _uuid(ctx.org_id)
    (output,) = data["outputs"]
    rows = []
    async with svc.db.transaction() as session:
        voice = (
            await session.execute(sa.select(Voice).where(Voice.org_id == org_id, Voice.id == _uuid(ctx.target_id)))
        ).scalar_one()
        for ref in output["result"]["candidates"]:
            candidate = VoiceCandidate(
                org_id=org_id,
                voice_id=voice.id,
                job_id=_uuid(ctx.job_id),
                artifact_id=_uuid(ref["artifact_id"]),
                description=str(ctx.args.get("description", "")),
                engine=output["adapter_id"],
            )
            session.add(candidate)
            await session.flush()
            asset = await asset_from_ref(
                svc,
                session,
                org_id,
                ref,
                kind="audio",
                tags=["voice_candidate"],
                rights={
                    "job_id": ctx.job_id,
                    "adapter_id": output["adapter_id"],
                    "voice_candidate_id": str(candidate.id),
                },
                user_id=_uuid(ctx.user_id) if ctx.user_id else None,
            )
            rows.append(
                {
                    "candidate_id": str(candidate.id),
                    "artifact_id": ref["artifact_id"],
                    "asset_id": str(asset.id),
                    "mock": output["mock"],
                }
            )
    return StudioStep(done=True, result={"candidates": rows})


@stage("voice_test", "start")
async def voice_test_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    from ce_router import RouteRequest, route

    org_id = _uuid(ctx.org_id)
    language = str(ctx.args.get("language") or "en")
    async with svc.db.transaction() as session:
        row = (
            await session.execute(
                sa.select(VoiceVersion).where(VoiceVersion.org_id == org_id, VoiceVersion.id == _uuid(ctx.target_id))
            )
        ).scalar_one()
        references = []
        for ref in row.references or []:
            if ref.get("asset_id"):
                audio = await ref_of_asset(
                    svc, session, org_id, _uuid(ref["asset_id"]), kind="audio", role="voice_reference"
                )
                references.append(
                    {
                        "audio": audio.model_dump(mode="json"),
                        "transcript": str(ref.get("transcript", "")),
                        "language": str(ref.get("language", language)),
                    }
                )
        lexicon = {str(e["term"]): str(e.get("respelling") or e["term"]) for e in row.lexicon or [] if e.get("term")}
        description = row.description
    hint = ctx.args.get("engine_hint")
    decision = route(RouteRequest(capability="voice.tts", language=language, engine_hint=hint), svc.catalog)
    data = {
        "language": language,
        "lexicon": lexicon,
        "description": description,
        "adapter_id": decision.adapter_id,
        "references": references,
    }
    if references:
        call = ModelCall(
            key="voice_test.prepare",
            capability="voice.clone_prepare",
            adapter_id=decision.adapter_id,
            request={"references": references, "description": description},
        )
        return StudioStep(calls=[call], next="synthesize", progress=0.1, data=data)
    return await voice_test_synthesize(svc, ctx, {**data, "outputs": []})


@stage("voice_test", "synthesize")
async def voice_test_synthesize(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    from ce_core.text.tokenize import tokenize

    text = str(ctx.args.get("text") or _studio(svc).voice_test_text)
    conditioning = data["outputs"][0]["result"]["conditioning"] if data.get("outputs") else None
    call = ModelCall(
        key="voice_test.tts",
        capability="voice.tts",
        adapter_id=data["adapter_id"],
        language=data["language"],
        seed=7,
        request={
            "text": text,
            "words": [t.text for t in tokenize(text)],
            "language": data["language"],
            "voice": {"conditioning": conditioning, "description": data["description"], "lexicon": data["lexicon"]},
            "labels": {"kind": "voice_test", **({"tags": ",".join(ctx.args["tags"])} if ctx.args.get("tags") else {})},
        },
    )
    return StudioStep(calls=[call], next="measure", progress=0.4, data={**data, "text": text})


@stage("voice_test", "measure")
async def voice_test_measure(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    (tts,) = data["outputs"]
    audio = tts["result"]["audio"]
    mock = bool(tts["mock"])
    calls = [
        ModelCall(
            key="voice_test.asr",
            capability="asr.transcribe",
            language=data["language"],
            prefer_mock=mock,
            request={"audio": audio, "language": data["language"], "hint_text": data["text"] if mock else None},
        ),
        ModelCall(
            key="voice_test.quality",
            capability="qc.speech_quality",
            prefer_mock=mock,
            request={"media": audio, "text": data["text"]},
        ),
        ModelCall(
            key="voice_test.embed",
            capability="voice.embed",
            prefer_mock=mock,
            request={"audio": audio, "labels": {"identity": ctx.target_id} if mock else {}},
        ),
    ]
    for i, ref in enumerate(data["references"][:3]):
        calls.append(
            ModelCall(
                key=f"voice_test.ref_embed:{i}",
                capability="voice.embed",
                prefer_mock=mock,
                request={"audio": ref["audio"], "labels": {"identity": ctx.target_id} if mock else {}},
            )
        )
    return StudioStep(calls=calls, next="store", progress=0.7, data={**data, "tts": tts})


@stage("voice_test", "store")
async def voice_test_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    from ce_core.text.tokenize import tokenize
    from ce_voice.metrics import script_error_rates

    outputs = _by_key(data["outputs"])
    tts = data["tts"]
    duration = float(tts["result"]["duration_s"])
    words = len(tokenize(data["text"]))
    wpm = round(words * 60.0 / duration, 1) if duration > 0 else None
    heard = str(outputs["voice_test.asr"]["result"]["text"])
    language = data["language"]
    rates = script_error_rates(data["text"], heard, language, lexicon=data.get("lexicon") or None)
    measured_wer, measured_cer = rates.wer, rates.cer
    quality = outputs["voice_test.quality"]["result"]
    test_vec = (outputs["voice_test.embed"]["result"]["vectors"] or [[]])[0]
    refs = [
        o["result"]["vectors"][0]
        for k, o in outputs.items()
        if k.startswith("voice_test.ref_embed:") and o["result"]["vectors"]
    ]
    similarity = None
    if refs:
        scores = [s for s in (cosine(test_vec, r) for r in refs) if s is not None]
        similarity = round(sum(scores) / len(scores), 4) if scores else None
    result = {
        "audio_artifact_id": tts["result"]["audio"].get("artifact_id"),
        "adapter_id": tts["adapter_id"],
        "mock": bool(tts["mock"]),
        "text": data["text"],
        "heard": heard,
        "wer": measured_wer,
        "cer": measured_cer,
        "speech_quality": quality.get("score"),
        "speech_quality_metric": quality.get("metric"),
        "wpm": wpm,
        "duration_s": duration,
        "speaker_similarity": similarity,
        "speaker_similarity_basis": "voice references" if refs else "no references: not measured",
        "language": language,
    }
    org_id = _uuid(ctx.org_id)
    async with svc.db.transaction() as session:
        row = (
            await session.execute(
                sa.select(VoiceVersion)
                .where(VoiceVersion.org_id == org_id, VoiceVersion.id == _uuid(ctx.target_id))
                .with_for_update()
            )
        ).scalar_one()
        if row.status == "draft" and wpm and not result["mock"]:
            # WPM calibration (§21): the measured rate of a real engine; mocks never calibrate.
            row.wpm = {**dict(row.wpm or {}), language: wpm}
            result["wpm_recorded"] = True
    return StudioStep(done=True, result=result)


# ====================================================================== world plates
@stage("world_plates", "start")
async def plates_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    cfg = _studio(svc)
    org_id = _uuid(ctx.org_id)
    async with svc.db.transaction() as session:
        row = await _draft(session, WorldVersion, org_id, _uuid(ctx.target_id), "world version")
        dna = WorldDNA.model_validate(row.dna)
        if ctx.args.get("mode") == "fingerprint":
            chosen = []
            for position, by_time in (row.plates or {}).items():
                for time_of_day, by_weather in by_time.items():
                    for weather, asset_id in by_weather.items():
                        ref = await ref_of_asset(svc, session, org_id, _uuid(asset_id), kind="image", role="plate")
                        chosen.append(
                            {
                                "position": position,
                                "time_of_day": time_of_day,
                                "weather": weather,
                                "asset_id": str(asset_id),
                                "ref": ref.model_dump(mode="json"),
                            }
                        )
            if not chosen:
                raise StudioError("no plates are chosen yet")
            calls = [
                ModelCall(
                    key=f"plate.embed:{c['position']}:{c['time_of_day']}:{c['weather']}",
                    capability="image.embed",
                    prefer_mock=_is_mock(c["ref"]),
                    request={"images": [c["ref"]]},
                )
                for c in chosen
            ]
            return StudioStep(
                calls=calls, next="fingerprint_store", progress=0.2, data={"chosen": chosen, "plates": row.plates}
            )
        references = []
        for reference in dna.references:
            ref = await ref_of_asset(svc, session, org_id, reference.asset_id, kind="image", role=reference.role)
            references.append((reference.role, ref.model_dump(mode="json")))
    positions = [c.key for c in dna.camera_positions if c.status == CameraPositionStatus.PERMITTED]
    wanted = ctx.args.get("camera_position_keys") or positions
    times = ctx.args.get("times_of_day") or [str(dna.time_and_weather.default_time_of_day)]
    weathers = ctx.args.get("weather") or [str(dna.time_and_weather.default_weather)]
    allowed_t = {str(t) for t in dna.time_and_weather.allowed_times}
    allowed_w = {str(w) for w in dna.time_and_weather.allowed_weather}
    bad = [p for p in wanted if p not in positions] + [t for t in times if t not in allowed_t]
    bad += [w for w in weathers if w not in allowed_w]
    if bad:
        raise StudioError(f"not permitted for this world: {bad}")
    count = int(ctx.args.get("candidates") or cfg.plate_candidates)
    calls = []
    for position in wanted:
        for time_of_day in times:
            for weather in weathers:
                prompt, elements = plate_prompt(dna, position, time_of_day, weather)
                refs = [r for role, r in references if role in ("establishing", f"position:{position}")]
                for i in range(count):
                    calls.append(
                        ModelCall(
                            key=f"plate:{position}:{time_of_day}:{weather}:{i + 1}",
                            capability="image.generate",
                            seed=i + 1,
                            height=cfg.plate_height,
                            width=cfg.plate_width,
                            request={
                                "prompt": prompt,
                                "negative": "people, person, text, watermark",
                                "references": refs,
                                "width": cfg.plate_width,
                                "height": cfg.plate_height,
                                "labels": {
                                    "kind": "plate",
                                    "camera_position": position,
                                    "time_of_day": time_of_day,
                                    "weather": weather,
                                    "elements": ",".join(elements),
                                },
                            },
                        )
                    )
    return StudioStep(calls=calls, next="generate_store", progress=0.1)


@stage("world_plates", "generate_store")
async def plates_generate_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = _uuid(ctx.org_id)
    added = 0
    async with svc.db.transaction() as session:
        row = await _draft(session, WorldVersion, org_id, _uuid(ctx.target_id), "world version")
        candidates = {
            p: {t: {w: list(ids) for w, ids in tw.items()} for t, tw in pt.items()}
            for p, pt in (row.plate_candidates or {}).items()
        }
        for output in data["outputs"]:
            _, position, time_of_day, weather, _ = output["key"].split(":")
            asset = await asset_from_ref(
                svc,
                session,
                org_id,
                output["result"]["image"],
                tags=["world_plate", f"world:{row.world_id}"],
                rights={"job_id": ctx.job_id, "adapter_id": output["adapter_id"]},
                user_id=_uuid(ctx.user_id) if ctx.user_id else None,
            )
            bucket = candidates.setdefault(position, {}).setdefault(time_of_day, {}).setdefault(weather, [])
            if str(asset.id) not in bucket:
                bucket.append(str(asset.id))
                added += 1
        row.plate_candidates = candidates
    return StudioStep(done=True, result={"candidates_added": added, "plate_candidates": candidates})


@stage("world_plates", "fingerprint_store")
async def plates_fingerprint_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    import tempfile
    from pathlib import Path

    from ce_db import execution as rec
    from ce_storage import content_key

    org_id = _uuid(ctx.org_id)
    outputs = _by_key(data["outputs"])
    entries = []
    with tempfile.TemporaryDirectory(prefix="ce-plates-") as tmp:
        for chosen in data["chosen"]:
            key = f"plate.embed:{chosen['position']}:{chosen['time_of_day']}:{chosen['weather']}"
            embed = outputs[key]
            path = await svc.content.fetch(chosen["ref"]["sha256"], Path(tmp) / chosen["ref"]["sha256"])
            entries.append(
                {
                    "camera_position_key": chosen["position"],
                    "time_of_day": chosen["time_of_day"],
                    "weather": chosen["weather"],
                    "asset_id": chosen["asset_id"],
                    "sha256": chosen["ref"]["sha256"],
                    "embedding": embed["result"]["vectors"][0],
                    "embedding_adapter": embed["adapter_id"],
                    "mock": bool(embed["mock"]),
                    **plate_statistics(path),
                }
            )
    import json

    document = {"kind": "world_fingerprints", "world_version_id": ctx.target_id, "plates": entries}
    raw = json.dumps(document, sort_keys=True).encode()
    sha = await svc.content.put_bytes(raw, mime="application/json")
    async with svc.db.transaction() as session:
        row = await _draft(session, WorldVersion, org_id, _uuid(ctx.target_id), "world version")
        if row.plates != data["plates"]:
            raise StudioError("the plate choices changed while fingerprinting; choose again to fingerprint them")
        artifact_id = await rec.register_artifact(
            session,
            org_id,
            sha256=sha,
            kind="world_fingerprints",
            mime="application/json",
            size=len(raw),
            storage_key=content_key(sha),
            media={"role": "world_fingerprints", "world_version_id": ctx.target_id},
            produced_by_node_id=None,
        )
        row.fingerprints_artifact_id = artifact_id
    return StudioStep(done=True, result={"fingerprints_artifact_id": str(artifact_id), "plates": len(entries)})
