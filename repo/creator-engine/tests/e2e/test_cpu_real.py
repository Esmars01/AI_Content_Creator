"""Phase 7 DoD with the real CPU engines on the Compose infrastructure (`CPU_REAL_ENGINES=auto`):

- the exact-script loop passes with CPU TTS (Kokoro) verified by CPU ASR (faster-whisper), aligned
  coarsely by the same ASR;
- with real CPU analyzers (MediaPipe) on mock video, the triad reports `NOT_MEASURABLE` honestly
  where no face is detected — never a pass;
- the render's C2PA signature and hashes verify with the untrusted dev root.

Skips with the reason when `make fetch-cpu-assets` has not run.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.provenance import verify_render_file
from ce_contracts.manifest import missing_assets
from ce_contracts.plugins import discover
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_db.models.assets import Artifact, ExecutionNode
from ce_db.models.videos import Render
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.seed import placeholder_objects
from ce_testing.stack import REAL_MODEL_CACHE, Stack, real_engine_env, stack_env

pytestmark = [pytest.mark.infra, pytest.mark.slow]

NEEDED = ("kokoro_cpu", "faster_whisper_cpu", "mediapipe_face", "mediapipe_body", "dinov2_embed")


def _missing() -> list[str]:
    registry = discover(app_env="test", include_mocks=True)
    return [
        f"{p}: {', '.join(missing_assets(registry.get(p).manifest, REAL_MODEL_CACHE))}"
        for p in NEEDED
        if missing_assets(registry.get(p).manifest, REAL_MODEL_CACHE)
    ]


if _missing():
    pytest.skip(
        f"real CPU engine assets are not fetched ({'; '.join(_missing())}); run `make fetch-cpu-assets`",
        allow_module_level=True,
    )


@pytest_asyncio.fixture
async def real_stack(e2e_db: TestDatabase) -> AsyncIterator[Stack]:
    async with Stack.run(stack_env(e2e_db.url, **real_engine_env())) as s:
        bucket = s.effective.settings.s3_bucket_assets
        for item in placeholder_objects():
            await s.exec.storage.put(bucket, item.key, item.data, content_type=item.mime)
        yield s


async def _routes(stack: Stack, version_id: object) -> dict[str, str]:
    async with stack.exec.db.session() as session:
        rows = (
            await session.execute(
                sa.select(ExecutionNode.node_key, ExecutionNode.route).where(ExecutionNode.version_id == version_id)
            )
        ).all()
    return {k: r["adapter_id"] for k, r in rows if r}


async def test_real_cpu_engines_end_to_end(real_stack: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stack = real_stack
    # The mock keyframes draw a cartoon face that MediaPipe does detect; this run injects faceless
    # takes so the honesty rule is exercised: no face → NOT_MEASURABLE, never a pass.
    monkeypatch.setenv("MOCK_IMAGE_FACELESS_RATE", "1.0")
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), 900)
    assert result.state in ("ready", "needs_review"), (result.failed, result.statuses)
    routes = await _routes(stack, submitted.version_id)
    assert routes["tts.segment:seg_1"] == "kokoro_cpu"
    assert routes["asr.verify:seg_1"] == routes["align.segment:seg_1"] == "faster_whisper_cpu"
    assert routes["behavior.observe:sht_1:t1"] == "mediapipe_face"

    # the exact-script loop with CPU TTS
    for segment in ("seg_1", "seg_2"):
        verify = await stack.exec.docs.output(result.outputs[f"asr.verify:{segment}"])
        assert verify.data["passed"] is True, verify.data
        assert verify.data["wer"] <= verify.data["max_wer"] or verify.data["cer"] <= verify.data["max_cer"]
        align = await stack.exec.docs.output(result.outputs[f"align.segment:{segment}"])
        assert align.data["precision"] == "coarse"
        times = align.data["words"]
        assert all(a <= b for a, b in times) and all(times[i][0] <= times[i + 1][0] for i in range(len(times) - 1))
    assert result.state == "ready"

    # real analyzers on mock video: no face, so nothing visual is measurable — and nothing passes
    observed = await stack.exec.docs.output(result.outputs["behavior.observe:sht_1:t1"])
    assert observed.data["summary"]["face_detected_ratio"] == 0.0
    coverage = await stack.exec.docs.output(result.outputs["behavior.coverage:video"])
    report = BehaviorCoverageReport.model_validate(coverage.data["report"])
    visual = [
        e for e in report.entries if e.dimension in ("gaze", "facial_expression", "emotion_visual", "head_motion")
    ]
    face_judged = [
        e
        for e in visual
        if e.observed is not None and e.compiled.method not in ("editorial_cutaway", "editorial_punch_in")
    ]
    assert face_judged, "the fixture requests visual behavior"
    for entry in face_judged:
        assert entry.observed is not None
        assert entry.observed.verdict in ("NOT_MEASURABLE", "NOT_APPLICABLE"), (entry.item_ref, entry.observed)
    # the take-level observations name why (the coverage entry only carries the verdict)
    shot_qc = await stack.exec.docs.output(result.outputs["qc.shot:sht_1:t1"])
    take_items = shot_qc.data["behavior"]["item_observations"]
    face_items = [
        o for o in take_items if o["dimension"] in ("gaze", "facial_expression", "emotion_visual", "head_motion")
    ]
    assert face_items and all(o["verdict"] in ("NOT_MEASURABLE", "NOT_APPLICABLE") for o in face_items), face_items
    assert any("face not detected" in o["method"] for o in face_items), face_items

    # provenance: the C2PA signature and hashes verify with the untrusted dev root
    async with stack.exec.db.session() as session:
        render = (
            await session.execute(
                sa.select(Render).where(Render.version_id == submitted.version_id, Render.is_proxy.is_(False))
            )
        ).scalar_one()
    assert render.c2pa_manifest is not None and render.provenance_mode == "mock_dev"
    routes_sign = {k: v for k, v in routes.items() if k.startswith("provenance.sign:")}
    assert set(routes_sign.values()) == {"c2pa_signer"}, routes_sign
    async with stack.exec.db.session() as session:
        artifact = (await session.execute(sa.select(Artifact).where(Artifact.id == render.artifact_id))).scalar_one()
    path = tmp_path / "render.mp4"
    await stack.exec.storage.download(stack.effective.settings.s3_bucket_artifacts, artifact.storage_key, path)
    verdict = await verify_render_file(
        path,
        sha256=artifact.sha256,
        mime=artifact.mime,
        routes={"c2pa": "c2pa_signer"},
        payload_id=render.watermark_payload_id,
        provenance_mode=render.provenance_mode,
        settings=stack.effective.settings,
    )
    assert verdict["c2pa"]["present"] and verdict["c2pa"]["ok_untrusted_root"], verdict
    assert verdict["c2pa"]["failures"] == ["signingCredential.untrusted"], verdict
    assert verdict["verdict"] == "mock_dev"  # watermarks stay mock_dev in dev (ADR 0047)
