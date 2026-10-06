"""Jobs, manifests and renders (§30): shapes, org scoping, render requests and cancellation."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant
from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_db.models.assets import Artifact, ExecutionNode, GenerationJob, JobAttempt
from ce_db.models.videos import BuildManifestEntry, Caption, Render, Video, VideoVersion
from ce_testing.seed import example_version_row

pytestmark = [pytest.mark.infra]


async def _version(harness: ApiHarness, tenant: ApiTenant, *, state: str = "ready") -> dict[str, Any]:
    project = (await tenant.client.post("/v1/projects", json={"name": "P"})).json()
    async with harness.services.db.transaction() as session:
        video = Video(org_id=tenant.org_id, project_id=uuid.UUID(project["id"]))
        session.add(video)
        await session.flush()
        row = example_version_row(tenant.org_id) | {"id": uuid.uuid4(), "video_id": video.id, "state": state}
        session.add(VideoVersion(**row))
        await session.flush()
        job = GenerationJob(
            org_id=tenant.org_id,
            kind="generate",
            status="succeeded",
            target_type="video_version",
            target_id=row["id"],
            video_version_id=row["id"],
            progress=1.0,
        )
        session.add(job)
        await session.flush()
        node = ExecutionNode(
            org_id=tenant.org_id,
            job_id=job.id,
            version_id=row["id"],
            node_key="tts.segment:seg_1",
            node_kind="tts.segment",
            status="succeeded",
            route={"adapter_id": "mock_voice", "model_id": "mock-voice", "revision": "1"},
        )
        session.add(node)
        await session.flush()
        session.add(
            JobAttempt(
                org_id=tenant.org_id, node_id=node.id, attempt_no=1, reason="initial", status="succeeded", seed=7
            )
        )
        artifact = Artifact(
            org_id=tenant.org_id,
            kind="video",
            storage_key="sha256/aa/bb/x",
            mime="video/mp4",
            bytes=10,
            sha256="ab" * 32,
        )
        session.add(artifact)
        await session.flush()
        session.add(
            BuildManifestEntry(
                org_id=tenant.org_id,
                version_id=row["id"],
                node_key="tts.segment:seg_1",
                route={"adapter_id": "mock_voice", "model_id": "mock-voice", "revision": "1"},
                effective_seed=7,
                artifact_id=artifact.id,
                config_digests={"default.yaml": "sha256:" + "0" * 64},
                impl_version="1",
            )
        )
        render = Render(
            org_id=tenant.org_id,
            version_id=row["id"],
            preset_id="tiktok_1080x1920_30",
            aspect="9:16",
            provenance_mode="mock_dev",
            artifact_id=artifact.id,
            status="ready",
        )
        session.add(render)
        await session.flush()
        return {"version_id": str(row["id"]), "job_id": str(job.id), "render_id": str(render.id)}


async def test_job_detail_lists_nodes_and_attempts(harness: ApiHarness, owner: ApiTenant) -> None:
    ids = await _version(harness, owner)
    detail = (await owner.client.get(f"/v1/jobs/{ids['job_id']}")).json()
    assert detail["kind"] == "generate" and detail["status"] == "succeeded"
    (node,) = detail["nodes"]
    assert node["node_key"] == "tts.segment:seg_1" and node["route"]["adapter_id"] == "mock_voice"
    assert node["attempt_history"][0]["seed"] == 7
    listed = (await owner.client.get("/v1/jobs", params={"kind": "generate"})).json()
    assert [j["id"] for j in listed["items"]] == [ids["job_id"]]
    assert (await owner.client.get("/v1/jobs", params={"status": "failed"})).json()["items"] == []


async def test_manifest_and_renders(harness: ApiHarness, owner: ApiTenant) -> None:
    ids = await _version(harness, owner)
    manifest = (await owner.client.get(f"/v1/versions/{ids['version_id']}/manifest")).json()
    assert manifest["routes"]["tts.segment:seg_1"]["adapter_id"] == "mock_voice"
    assert manifest["effective_seeds"] == {"tts.segment:seg_1": 7}
    assert set(manifest["config_digests"]) == {"default.yaml"}
    renders = (await owner.client.get(f"/v1/versions/{ids['version_id']}/renders")).json()
    assert [r["id"] for r in renders] == [ids["render_id"]] and renders[0]["provenance_mode"] == "mock_dev"
    detail = (await owner.client.get(f"/v1/renders/{ids['render_id']}")).json()
    assert detail["media"]["mime"] == "video/mp4"
    download = (await owner.client.get(f"/v1/renders/{ids['render_id']}/download")).json()
    assert download["url"] and download["exportable"] is False  # mock_dev renders cannot be exported


async def test_render_requests_need_a_ready_version_and_known_presets(
    harness: ApiHarness, owner: ApiTenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, Any, str]] = []

    async def fake_start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        started.append((workflow, arg, workflow_id))

    monkeypatch.setattr(harness.services.workflows, "start", fake_start)
    planned = await _version(harness, owner, state="planned")
    response = await owner.client.post(
        f"/v1/versions/{planned['version_id']}/renders", json={"preset_ids": ["tiktok_1080x1920_30"]}
    )
    assert response.status_code == 409
    ready = await _version(harness, owner)
    bad = await owner.client.post(f"/v1/versions/{ready['version_id']}/renders", json={"preset_ids": ["nope"]})
    assert bad.status_code == 422
    ok = await owner.client.post(
        f"/v1/versions/{ready['version_id']}/renders", json={"preset_ids": ["youtube_1920x1080_30"]}
    )
    assert ok.status_code == 202
    # Regression (audit A3): a second request while the first render job is queued would race it on
    # the version's render rows (duplicate rows break that preset for good)
    second = await owner.client.post(
        f"/v1/versions/{ready['version_id']}/renders", json={"preset_ids": ["youtube_1920x1080_30"]}
    )
    assert second.status_code == 409
    await harness.services.drain()
    (workflow, arg, workflow_id) = started[0]
    assert workflow == "RenderWorkflow" and arg["preset_ids"] == ["youtube_1920x1080_30"]
    assert workflow_id == f"render-{ok.json()['job_id']}"


async def test_cancel_only_running_jobs(harness: ApiHarness, owner: ApiTenant, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[str] = []

    async def fake_cancel(workflow_id: str) -> None:
        cancelled.append(workflow_id)

    monkeypatch.setattr(harness.services.workflows, "cancel", fake_cancel)
    ids = await _version(harness, owner)
    done = await owner.client.post(f"/v1/jobs/{ids['job_id']}:cancel")
    assert done.status_code == 409
    async with harness.services.db.transaction() as session:
        job = await session.get_one(GenerationJob, uuid.UUID(ids["job_id"]))
        job.status, job.temporal_workflow_id = "running", "generate-x"
    response = await owner.client.post(f"/v1/jobs/{ids['job_id']}:cancel")
    assert response.status_code == 202 and cancelled == ["generate-x"]


async def _signed_mp4(tmp: Path) -> tuple[bytes, str]:
    """A tiny clip signed by the C2PA signer (through the registry, as the build does)."""
    import subprocess

    src = tmp / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:d=1:r=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(src)],
        check=True,
    )  # fmt: skip
    ctx = LocalRunContext(tmp / "ctx")
    media = await ctx.write_artifact(src, "video", mime="video/mp4")
    signer = discover(app_env="test", include_mocks=True).get("c2pa_signer").adapter()
    await signer.load(LoadContext(model_cache_dir=str(tmp), scratch_dir=str(tmp / "scratch"), app_env="test"))
    signed = await signer.run(
        "provenance.sign",
        m.SignRequest(media=media, manifest={"watermark_payload_id": "wm_test", "routes": {}}, labels={"title": "t"}),
        ctx,
    )
    data = (await ctx.read_artifact(signed.media)).read_bytes()
    return data, signed.media.sha256


async def test_render_verification_reads_the_c2pa_manifest(
    harness: ApiHarness, owner: ApiTenant, tmp_path: Path
) -> None:
    ids = await _version(harness, owner)
    data, sha = await _signed_mp4(tmp_path)
    tampered = bytearray(data)
    tampered[len(tampered) // 2] ^= 0xFF  # inside mdat: the BMFF hash no longer matches
    bucket = harness.services.settings.s3_bucket_artifacts
    await harness.services.storage.ensure_bucket(bucket)
    async with harness.services.db.transaction() as session:
        render = await session.get(Render, uuid.UUID(ids["render_id"]))
        assert render is not None
        artifact = await session.get(Artifact, render.artifact_id)
        assert artifact is not None
        artifact.storage_key = f"test/{uuid.uuid4()}.mp4"
        artifact.sha256, artifact.bytes = sha, len(data)
        job = (
            await session.execute(sa.select(GenerationJob).where(GenerationJob.video_version_id == render.version_id))
        ).scalar_one()
        session.add(
            ExecutionNode(
                org_id=owner.org_id,
                job_id=job.id,
                version_id=render.version_id,
                node_key=f"provenance.sign:{render.preset_id}",
                node_kind="provenance.sign",
                status="succeeded",
                route={"adapter_id": "c2pa_signer", "model_id": "c2pa", "revision": "1"},
            )
        )
        key = artifact.storage_key
    await harness.services.storage.put(bucket, key, bytes(data), content_type="video/mp4")
    result = (await owner.client.get(f"/v1/renders/{ids['render_id']}/verify")).json()
    assert result["c2pa"]["present"] and result["c2pa"]["ok_untrusted_root"], result
    assert result["c2pa"]["failures"] == ["signingCredential.untrusted"]  # dev root, by design
    assert result["c2pa"]["payload_id"] == "wm_test" and result["c2pa"]["adapter_id"] == "c2pa_signer"
    assert result["verdict"] == "mock_dev"  # the watermark layers are mock_dev in dev
    assert result["watermarks"]["video"]["present"] is False

    await harness.services.storage.put(bucket, key, bytes(tampered), content_type="video/mp4")
    broken = (await owner.client.get(f"/v1/renders/{ids['render_id']}/verify")).json()
    assert not broken["c2pa"]["ok_untrusted_root"] and broken["verdict"] == "invalid", broken


async def test_render_verification_needs_a_ready_final_render(harness: ApiHarness, owner: ApiTenant) -> None:
    ids = await _version(harness, owner)
    async with harness.services.db.transaction() as session:
        render = await session.get(Render, uuid.UUID(ids["render_id"]))
        assert render is not None
        render.is_proxy = True
    response = await owner.client.get(f"/v1/renders/{ids['render_id']}/verify")
    assert response.status_code == 409


async def test_captions_list_and_download(harness: ApiHarness, owner: ApiTenant) -> None:
    ids = await _version(harness, owner)
    version_id = uuid.UUID(ids["version_id"])
    body = b"1\n00:00:00,200 --> 00:00:01,000\nEveryone thinks\n"
    sha = hashlib.sha256(body).hexdigest()
    bucket = harness.services.settings.s3_bucket_artifacts
    await harness.services.storage.ensure_bucket(bucket)
    key = f"test/{uuid.uuid4()}.srt"
    await harness.services.storage.put(bucket, key, body, content_type="application/x-subrip")
    async with harness.services.db.transaction() as session:
        artifact = Artifact(
            org_id=owner.org_id,
            kind="captions",
            storage_key=key,
            mime="application/x-subrip",
            bytes=len(body),
            sha256=sha,
        )
        session.add(artifact)
        await session.flush()
        session.add(
            Caption(
                org_id=owner.org_id,
                version_id=version_id,
                language="en",
                style_id="clean_subtitle",
                format="srt",
                artifact_id=artifact.id,
            )
        )
        session.add(
            Caption(
                org_id=owner.org_id,
                version_id=version_id,
                language="de",
                style_id="clean_subtitle",
                format="ass",
                review_state="pending",
            )
        )
    listed = (await owner.client.get(f"/v1/versions/{version_id}/captions")).json()
    assert [(c["language"], c["format"], c["review_state"]) for c in listed] == [
        ("de", "ass", "pending"),
        ("en", "srt", "n/a"),
    ]
    srt = next(c for c in listed if c["format"] == "srt")
    download = (await owner.client.get(f"/v1/captions/{srt['id']}/download")).json()
    assert download["filename"] == "captions.en.srt" and download["url"]
    assert (await owner.client.get(download["url"])).content == body
    pending = next(c for c in listed if c["format"] == "ass")
    assert (await owner.client.get(f"/v1/captions/{pending['id']}/download")).status_code == 409
