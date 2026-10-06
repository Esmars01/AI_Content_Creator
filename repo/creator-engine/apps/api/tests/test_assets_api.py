"""Assets (§30, §33): multipart presigned upload, validation job, idempotency, read, delete.

The S3 case is the Phase 1 DoD item "an upload round-trips through SeaweedFS".
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services, upload_asset
from ce_db.models.assets import GenerationJob
from ce_storage import MIN_PART_BYTES
from ce_testing.database import TestDatabase
from ce_testing.placeholders import placeholder_png, placeholder_wav

pytestmark = pytest.mark.infra


async def test_image_upload_round_trip(harness: ApiHarness, owner: ApiTenant) -> None:
    png = placeholder_png("desk", 32, 24)
    asset = await upload_asset(harness, owner, png, mime="image/png", kind="image", filename="desk.png")
    assert asset["status"] == "ready", asset
    assert asset["sha256"] == hashlib.sha256(png).hexdigest() and asset["bytes"] == len(png)
    assert asset["filename"] == "desk.png"
    assert asset["probe"]["format"]["format_name"] == "png_pipe"
    assert asset["probe"]["streams"][0]["width"] == 32
    downloaded = await owner.client.get(asset["download_url"])
    assert downloaded.status_code == 200 and downloaded.content == png
    assert 'filename="desk.png"' in downloaded.headers["content-disposition"]
    async with harness.services.db.session() as session:
        job = await session.get_one(GenerationJob, uuid.UUID(asset["job_id"]))
    assert (job.kind, job.status, float(job.progress), job.target_id) == (
        "asset_validation",
        "succeeded",
        1.0,
        uuid.UUID(asset["id"]),
    )


async def test_multipart_audio_upload(harness: ApiHarness, owner: ApiTenant) -> None:
    wav = placeholder_wav(seconds=120.0)  # 5.76 MB → two parts
    assert len(wav) > MIN_PART_BYTES
    asset = await upload_asset(harness, owner, wav, mime="audio/wav", kind="audio", filename="voice.wav")
    assert asset["status"] == "ready", asset
    assert asset["probe"]["streams"][0]["codec_type"] == "audio"


@pytest.mark.parametrize(
    ("data", "mime", "kind", "code"),
    [
        (b"<html>not an image</html>", "image/png", "image", "upload_sniff"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png", "image", "upload_probe"),
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100, "video/mp4", "video", "upload_probe"),
    ],
)
async def test_invalid_uploads_are_rejected(
    harness: ApiHarness, owner: ApiTenant, data: bytes, mime: str, kind: str, code: str
) -> None:
    asset = await upload_asset(harness, owner, data, mime=mime, kind=kind)
    assert asset["status"] == "rejected"
    assert asset["download_url"] is None
    assert [r["code"] for r in asset["probe"]["rejected"]] == [code]


async def test_declared_size_must_match(harness: ApiHarness, owner: ApiTenant) -> None:
    png = placeholder_png("size")
    asset = await upload_asset(harness, owner, png, mime="image/png", kind="image", declared_bytes=len(png) + 10)
    assert asset["status"] == "rejected" and asset["probe"]["rejected"][0]["code"] == "upload_size"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"filename": "x.exe", "mime": "application/x-msdownload", "bytes": 10, "kind": "document"},
            "upload_media_type",
        ),
        ({"filename": "big.mp4", "mime": "video/mp4", "bytes": 3 * 1024**3, "kind": "video"}, "upload_too_large"),
        ({"filename": "a.png", "mime": "image/png", "bytes": 10, "kind": "audio"}, "asset_kind_media"),
        ({"filename": "a.png", "mime": "image/png", "bytes": 10, "kind": "hologram"}, "asset_kind"),
        ({"filename": "../a.png", "mime": "image/png", "bytes": 10, "kind": "image"}, "filename"),
    ],
)
async def test_initiate_rejects_bad_requests(owner: ApiTenant, body: dict[str, object], code: str) -> None:
    response = await owner.client.post("/v1/assets:initiate-upload", json=body)
    assert response.status_code == 422
    assert code in [i["code"] for i in response.json()["issues"]]


async def test_complete_is_idempotent(harness: ApiHarness, owner: ApiTenant) -> None:
    png = placeholder_png("idem")
    plan = (
        await owner.client.post(
            "/v1/assets:initiate-upload",
            json={"filename": "i.png", "mime": "image/png", "bytes": len(png), "kind": "image"},
        )
    ).json()
    part = plan["upload"]["parts"][0]
    assert (await owner.client.put(part["url"], content=png, headers=part["headers"])).status_code == 200
    complete = f"/v1/assets/{plan['asset_id']}:complete"
    missing = await owner.client.post(complete, json={})
    assert missing.status_code == 422 and missing.json()["issues"][0]["code"] == "idempotency_key_missing"
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await owner.client.post(complete, json={}, headers=key)
    second = await owner.client.post(complete, json={}, headers=key)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()  # replayed, one job
    reused = await owner.client.post(complete, json={"parts": []}, headers=key)
    assert reused.status_code == 422 and reused.json()["issues"][0]["code"] == "idempotency_key_reused"
    await harness.services.drain()
    again = await owner.client.post(complete, json={}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert again.status_code == 409  # already ready
    async with harness.services.db.session() as session:
        jobs = (
            await session.execute(
                sa.select(sa.func.count()).where(GenerationJob.target_id == uuid.UUID(plan["asset_id"]))
            )
        ).scalar_one()
    assert jobs == 1


async def test_presigned_part_urls_are_bound(harness: ApiHarness, owner: ApiTenant) -> None:
    plan = (
        await owner.client.post(
            "/v1/assets:initiate-upload", json={"filename": "b.png", "mime": "image/png", "bytes": 100, "kind": "image"}
        )
    ).json()
    url = plan["upload"]["parts"][0]["url"]
    tampered = url.replace(plan["asset_id"], str(uuid.uuid4()))
    assert (await owner.client.put(tampered, content=b"x" * 100)).status_code == 403
    assert (await owner.client.get(url)).status_code == 403  # PUT URL used for GET


async def test_delete_asset(harness: ApiHarness, owner: ApiTenant) -> None:
    asset = await upload_asset(harness, owner, placeholder_png("del"), mime="image/png", kind="image")
    url = asset["download_url"]
    assert (await owner.client.delete(f"/v1/assets/{asset['id']}")).status_code == 204
    assert (await owner.client.get(f"/v1/assets/{asset['id']}")).status_code == 404
    assert (await owner.client.get(url)).status_code == 404  # the object is gone too


async def test_list_assets_filters(harness: ApiHarness, owner: ApiTenant) -> None:
    await upload_asset(harness, owner, placeholder_png("l1"), mime="image/png", kind="image")
    await upload_asset(harness, owner, placeholder_wav(0.5), mime="audio/wav", kind="audio")
    kinds = [a["kind"] for a in (await owner.client.get("/v1/assets")).json()["items"]]
    assert sorted(kinds) == ["audio", "image"]
    audio = (await owner.client.get("/v1/assets", params={"kind": "audio"})).json()["items"]
    assert [a["kind"] for a in audio] == ["audio"]


# ---------------------------------------------------------------------- SeaweedFS (DoD)
@pytest_asyncio.fixture
async def s3_harness(migrated_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(migrated_db.url, storage="s3")
    await services.storage.ensure_bucket(services.settings.s3_bucket_assets)
    h = ApiHarness(services)
    yield h
    await h.aclose()


async def test_upload_round_trips_through_seaweedfs(s3_harness: ApiHarness) -> None:
    tenant = await s3_harness.new_tenant()
    wav = placeholder_wav(seconds=110.0)  # two parts through presigned S3 URLs
    asset = await upload_asset(s3_harness, tenant, wav, mime="audio/wav", kind="audio", filename="take.wav")
    assert asset["status"] == "ready", asset
    assert asset["download_url"].startswith("http://localhost:8333/")
    async with httpx.AsyncClient(timeout=60) as network:
        downloaded = await network.get(asset["download_url"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == asset["sha256"] == hashlib.sha256(wav).hexdigest()
    head = await s3_harness.services.storage.head(
        s3_harness.services.settings.s3_bucket_assets, f"orgs/{tenant.org_id}/assets/{asset['id']}/original"
    )
    assert head.size == len(wav) and head.etag.endswith("-2")


async def _screen_recording(tmp: Path) -> bytes:
    from ce_render.ffmpeg import run_ffmpeg
    from ce_render.fonts import fonts_dir

    font = fonts_dir() / "NotoSans-Regular.ttf"
    vf = (
        f"drawtext=fontfile={font}:text='Billing overview':x=60:y=120:fontsize=44:fontcolor=white,"
        f"drawtext=fontfile={font}:text='Invoice paid':x=700:y=520:fontsize=40:fontcolor=white:enable='gte(t,1.5)'"
    )
    out = tmp / "screen.mp4"
    await run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=0x101418:s=1280x720:r=30:d=4",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )
    return out.read_bytes()


async def test_screen_recordings_are_analyzed_after_validation(
    harness: ApiHarness, owner: ApiTenant, tmp_path: Path
) -> None:
    data = await _screen_recording(tmp_path)
    asset = await upload_asset(harness, owner, data, mime="video/mp4", kind="screen_recording", filename="demo.mp4")
    assert asset["status"] == "ready", asset
    assert asset["probe"]["screen_analysis"]["summary"] == "mock"  # the VLM is mocked (§39.2)
    result = (await owner.client.get(f"/v1/assets/{asset['id']}/screen-analysis")).json()
    assert result["status"] == "ready" and result["keyframes_url"], result
    analysis = result["analysis"]
    assert analysis["asset_sha256"] == asset["sha256"] and analysis["duration_s"] == pytest.approx(4.0, abs=0.1)
    changed = [k for k in analysis["keyframes"] if k["changed_regions"]]
    assert changed and changed[0]["changed_regions"][0][0] > 0.5  # the invoice line, bottom right
    ocr = analysis["routes"].get("vision.ocr", {}).get("adapter_id")
    if ocr == "ppocr":  # CPU_REAL_ENGINES=auto: PP-OCR reads the text and the diff names the new line
        texts = {b["text"] for k in analysis["keyframes"] for b in k["ocr"]}
        assert "Billing overview" in texts and "Invoice paid" in texts, texts
        assert any("Invoice paid" in k["added"] for k in analysis["keyframes"][1:])
    else:
        assert ocr == "mock_vision"
    async with harness.services.db.session() as session:
        jobs = (
            (
                await session.execute(
                    sa.select(GenerationJob)
                    .where(GenerationJob.target_id == uuid.UUID(asset["id"]))
                    .order_by(GenerationJob.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [(j.kind, j.status) for j in jobs] == [("asset_validation", "succeeded"), ("screen_analysis", "succeeded")]
    assert jobs[1].parent_job_id == jobs[0].id


async def test_only_screen_recordings_have_a_screen_analysis(harness: ApiHarness, owner: ApiTenant) -> None:
    png = placeholder_png("not-a-screen")
    asset = await upload_asset(harness, owner, png, mime="image/png", kind="image", filename="x.png")
    response = await owner.client.get(f"/v1/assets/{asset['id']}/screen-analysis")
    assert response.status_code == 409
