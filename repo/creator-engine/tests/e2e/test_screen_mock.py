"""Screen recordings end to end (§27, Phase 7) on the Compose infrastructure, mock engines:

upload → `AssetValidationWorkflow` → child `ScreenAnalysisWorkflow` (scene changes, keyframes,
OCR through the routed analyzer, pixel diffs, dead time, the mock VLM summary) → a
`screen_analysis` artifact → an edit adding a screen shot gets planned zooms, speed segments and a
webcam bubble → the derived version renders the zoomed recording with the bubble over it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import pytest
import sqlalchemy as sa
from ce_core.enums import JobKind
from ce_core.ids import new_id
from ce_db import execution as rec
from ce_db.models.assets import Artifact, Asset, GenerationJob
from ce_db.models.videos import Caption, EditProposal, Render
from ce_exec.parents import artifact_shas
from ce_orchestrator.models import AssetValidationInput
from ce_orchestrator.worker import queues
from ce_render.ffmpeg import run_ffmpeg
from ce_render.fonts import fonts_dir
from ce_render.screen import extract_frames
from ce_storage import asset_key, content_key
from ce_testing.fixtures import ALEX, two_scene_spec_dict
from ce_testing.stack import Stack

from tests.e2e.test_edit_mock import _job, _run, _statuses

pytestmark = [pytest.mark.infra, pytest.mark.slow]

TIMEOUT_S = 300


async def _recording(path: Path) -> Path:
    """8 s, 1280×720: a dashboard; a line appears bottom right at 1 s; then nothing changes."""
    font = fonts_dir() / "NotoSans-Regular.ttf"

    def text(t: str, x: int, y: int, start: float, size: int = 40) -> str:
        return (
            f"drawtext=fontfile={font}:text='{t}':x={x}:y={y}:fontsize={size}:fontcolor=white:enable='gte(t,{start})'"
        )

    vf = ",".join(
        [
            "drawbox=x=0:y=0:w=iw:h=60:color=0x333333:t=fill",
            text("Agents dashboard", 20, 10, 0),
            text("Smarter chatbots", 80, 200, 0),
            text("Agents plan and act", 700, 560, 1.0, 36),
        ]
    )
    await run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x101418:s=1280x720:r=30:d=8",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


async def _upload(stack: Stack, path: Path) -> tuple[UUID, dict[str, Any]]:
    """The API's upload flow without HTTP: the object, the row, then the validation workflow."""
    data = path.read_bytes()
    asset_id = new_id()
    key = asset_key(ALEX.ORG_ID, asset_id)
    await stack.exec.storage.put(stack.effective.settings.s3_bucket_assets, key, data, content_type="video/mp4")
    async with stack.exec.db.transaction() as session:
        session.add(
            Asset(
                id=asset_id, org_id=ALEX.ORG_ID, project_id=ALEX.PROJECT_ID, kind="screen_recording", storage_key=key,
                mime="video/mp4", bytes=len(data), sha256="0" * 64, status="uploading", probe={},
            )
        )  # fmt: skip
        await session.flush()
        job = GenerationJob(
            org_id=ALEX.ORG_ID,
            kind=JobKind.ASSET_VALIDATION.value,
            status="queued",
            target_type="asset",
            target_id=asset_id,
        )
        session.add(job)
        await session.flush()
        job.temporal_workflow_id = f"asset_validation-{job.id}"
        job_id = job.id
    handle = await stack.temporal.start_workflow(
        "AssetValidationWorkflow",
        AssetValidationInput(org_id=str(ALEX.ORG_ID), job_id=str(job_id), asset_id=str(asset_id)),
        id=f"asset_validation-{job_id}",
        task_queue=queues(stack.effective).orchestrator,
        result_type=dict,
    )
    result: dict[str, Any] = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    return asset_id, result


async def test_screen_recording_analysis_plan_and_render(stack: Stack, tmp_path: Path) -> None:
    recording = await _recording(tmp_path / "rec.mp4")
    asset_id, validated = await _upload(stack, recording)
    assert validated["status"] == "ready" and validated["kind"] == "screen_recording", validated
    assert validated["screen_analysis"]["status"] == "succeeded", validated

    # 1. the analysis: an artifact linked from the asset, routed through the (mock) analyzers
    async with stack.exec.db.session() as session:
        asset = await session.get_one(Asset, asset_id)
        meta = asset.probe["screen_analysis"]
        artifact = await session.get_one(Artifact, UUID(meta["artifact_id"]))
        job = await session.get_one(GenerationJob, UUID(validated["screen_analysis_job_id"]))
    assert asset.sha256 == hashlib.sha256(recording.read_bytes()).hexdigest() == meta["asset_sha256"]
    assert (artifact.kind, artifact.mime, job.kind, job.status) == (
        "screen_analysis",
        "application/json",
        "screen_analysis",
        "succeeded",
    )
    analysis = json.loads(
        await stack.exec.storage.get(stack.effective.settings.s3_bucket_artifacts, content_key(meta["sha256"]))
    )
    assert analysis["routes"]["vision.ocr"]["adapter_id"] == "mock_vision"  # CPU_REAL_ENGINES=off
    assert analysis["summary"]["status"] == "mock" and analysis["summary"]["events"]
    assert analysis["scenes"] == [{"index": 0, "start_s": 0.0, "end_s": 8.0}]
    changed = [k for k in analysis["keyframes"] if k["changed_regions"]]
    assert len(changed) == 1 and 1.0 < changed[0]["t_s"] < 2.5, analysis["keyframes"]
    x, y, _w, _h = changed[0]["changed_regions"][0]
    assert 0.5 < x < 0.6 and 0.7 < y < 0.85
    assert analysis["dead_time"] and analysis["dead_time"][-1]["end_s"] > 6.0
    assert {e["kind"] for e in analysis["events"]} >= {"region_changed", "dead_time", "vlm"}

    # 2. generate v1, then an edit adds a screen shot over the hook (no zooms given)
    spec = two_scene_spec_dict()
    spec["meta"]["mode"] = "educational"  # screen shots are allowed
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    v1, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    assert (await asyncio.wait_for(handle.result(), TIMEOUT_S)).state == "ready"
    shot = {
        "key": "sht_9",
        "type": "screen",
        "layer": "overlay",
        "span": {
            "kind": "words",
            "start": {"segment_key": "seg_1", "word": 1},
            "end": {"segment_key": "seg_1", "word": 7},
        },
        "camera": {"profile_id": "screen_webcam_bubble", "framing": "insert", "angle": "eye_level"},
        "screen": {"asset_id": str(asset_id)},
    }
    async with stack.exec.db.transaction() as session:
        proposal = EditProposal(
            org_id=ALEX.ORG_ID,
            version_id=v1.version_id,
            instruction="show the dashboard recording",
            selection={"kind": "edit"},
            ops=[{"op": "add_shot", "scene_key": "scn_hook", "shot": shot}],
            status="proposing",
        )
        session.add(proposal)
        await session.flush()
        proposal_id = proposal.id
    propose_job = await _job(
        stack, JobKind.EDIT_PROPOSE, v1.version_id, {"edit_proposal_id": str(proposal_id), "auto_apply": False}
    )
    proposed = await _run(stack, "ProposeEditWorkflow", propose_job, v1.version_id)
    async with stack.exec.db.session() as session:
        row = await session.get_one(EditProposal, proposal_id)
    assert proposed["proposal"]["status"] == "proposed", row.impact.get("issues")
    assert [op["op"] for op in row.ops] == ["add_shot", "set_shot"]
    planned = row.ops[1]["changes"]
    assert planned["webcam_bubble"]["enabled"] is True and planned["zooms"], planned
    assert any(e.get("kind") == "screen_plan" for e in row.impact["side_effects"])

    # 3. apply → the derived version renders the screen shot
    child_id = new_id()
    apply_job = await _job(
        stack,
        JobKind.EDIT_APPLY,
        v1.version_id,
        {"edit_proposal_id": str(proposal_id), "new_version_id": str(child_id), "alternative": None},
    )
    applied = await _run(stack, "ApplyEditWorkflow", apply_job, v1.version_id)
    assert applied["build"] == {"kind": "generate", "state": "ready", "failed": []}, applied
    statuses = await _statuses(stack, UUID(applied["apply"]["next_job_id"]))
    assert statuses["screen.prepare:sht_9"] == "succeeded"
    async with stack.exec.db.session() as session:
        rows = await rec.load_manifest_rows(session, ALEX.ORG_ID, child_id)
        wanted = {"screen.prepare:sht_9", "align.segment:seg_1"}
        shas = await artifact_shas(
            session, ALEX.ORG_ID, {r["node_key"]: r["artifact_id"] for r in rows if r["node_key"] in wanted}
        )
        render = (
            await session.execute(sa.select(Render).where(Render.version_id == child_id, Render.is_proxy.is_(False)))
        ).scalar_one()
        final = await session.get_one(Artifact, render.artifact_id)
    async with stack.exec.db.session() as session:
        captions = (await session.execute(sa.select(Caption).where(Caption.version_id == child_id))).scalars().all()
    assert sorted((c.language, c.format) for c in captions) == [("en", "ass"), ("en", "srt"), ("en", "vtt")]
    assert all(c.artifact_id is not None and c.review_state == "n/a" for c in captions)
    prepared = await stack.exec.docs.output(shas["screen.prepare:sht_9"])
    screen = prepared.data["screen"]
    assert screen["zooms"] and screen["bubble"] == {
        "enabled": True,
        "corner": planned["webcam_bubble"]["corner"],
        "size": 0.28,
    }
    assert screen["letterboxed"] is True and prepared.data["clip_duration_s"] > 1.0

    # the delivered render: the letterboxed screen with the creator in the bubble during the shot
    words = (await stack.exec.docs.output(shas["align.segment:seg_1"])).data["words"]
    lead = stack.effective.bundle.app.render.timeline.lead_s
    middle = lead + (float(words[1][0]) + float(words[7][1])) / 2
    path = tmp_path / "final.mp4"
    await stack.exec.content.fetch(final.sha256, path)
    frame = (await extract_frames(path, [middle], (1080, 1920)))[0]
    assert frame[:300].mean() < 40  # the top letterbox bar of the screen overlay
    assert _has_disc(frame, planned["webcam_bubble"]["corner"])


def _has_disc(frame: np.ndarray, corner: str) -> bool:
    """The bubble: a ring of light pixels in the chosen corner, over the dark letterbox."""
    h, w = frame.shape[:2]
    d = round(0.28 * min(w, h) / 2) * 2
    margin = round(0.04 * min(w, h))
    x = margin if "left" in corner else w - d - margin
    y = round(0.08 * h) if "top" in corner else h - d - round(0.08 * h)
    patch = frame[y : y + d, x : x + d].astype(int)
    ring = patch[d // 2, : max(4, d // 30)].mean()  # the left edge of the disc
    center = patch[d // 2, d // 2]
    corner_px = patch[2, 2]
    return bool(ring > 150 and corner_px.mean() < 40 and center.mean() > 0)
