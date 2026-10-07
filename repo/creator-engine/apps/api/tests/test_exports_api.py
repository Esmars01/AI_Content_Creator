"""Caption translation, packaging and exports over HTTP (Phase 12): the platforms the export dialog
offers (limits labelled by source), caption translation as an auto-applied edit with review,
packaging edits validated against limits, and every export rule — mock provenance, version state,
platform preset, disclosure checklist, approved packaging, unsupported claims, translation review —
then the export with its metadata artifact and the memory `exported` job. Org-scoped (I12).

Builds do not run here: renders, captions and packaging rows are written directly. A render with
`provenance_mode=real` stands in for a production render (dev renders are mock_dev and are never
exported); the end-to-end packaging run is `tests/e2e/test_packaging_mock.py`."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services
from ce_db import execution as rec
from ce_db.models.assets import GenerationJob
from ce_db.models.platform import AuditLog
from ce_db.models.research import Claim
from ce_db.models.videos import Caption, Packaging, Project, Render, VideoVersion
from ce_exec.context import build_services as build_exec_services
from ce_exec.editing import propose_edit
from ce_exec.submit import Submitted, submit_spec
from ce_storage.content import content_key
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, two_scene_spec_dict
from ce_testing.seed import example_version_row

pytestmark = [pytest.mark.infra]

CHECKLIST = {"ai_generated_label": True, "ai_ad_tag": True, "not_mass_produced": True, "visible_label_reviewed": True}


@pytest_asyncio.fixture
async def harness(seeded_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(
        seeded_db.url, storage="local_fs", storage_root=tmp_path / "storage", extra_env={"MOCK_GPU": "true"}
    )
    for bucket in (services.settings.s3_bucket_assets, services.settings.s3_bucket_artifacts):
        await services.storage.ensure_bucket(bucket)
    h = ApiHarness(services)
    h.exec = build_exec_services(services.effective, pool_size=2)  # type: ignore[attr-defined]
    h.started = []  # type: ignore[attr-defined]

    async def start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        h.started.append((workflow, arg))  # type: ignore[attr-defined]

    services.workflows.start = start  # type: ignore[method-assign]
    yield h
    await h.exec.close()  # type: ignore[attr-defined]
    await h.aclose()


@pytest_asyncio.fixture
async def editor(harness: ApiHarness) -> ApiTenant:
    return await harness.add_member(ALEX.ORG_ID, "editor")


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def submitted(harness: ApiHarness, state: str = "ready", research: dict[str, Any] | None = None) -> Submitted:
    spec = two_scene_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    spec["meta"]["platform_targets"] = ["tiktok"]
    if research is not None:
        spec["research"] = research
    async with harness.services.db.transaction() as session:
        project = Project(org_id=ALEX.ORG_ID, name=f"exports {uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        project_id = project.id
    sub = await submit_spec(harness.exec, org_id=ALEX.ORG_ID, project_id=project_id, spec_data=spec)  # type: ignore[attr-defined]
    async with harness.services.db.transaction() as session:
        await session.execute(sa.update(VideoVersion).where(VideoVersion.id == sub.version_id).values(state=state))
    return sub


async def artifact(harness: ApiHarness, session: Any, data: bytes, kind: str, mime: str) -> UUID:
    sha = hashlib.sha256(data).hexdigest()
    await harness.services.storage.put(
        harness.services.settings.s3_bucket_artifacts, content_key(sha), data, content_type=mime
    )
    return await rec.register_artifact(
        session, ALEX.ORG_ID, sha256=sha, kind=kind, mime=mime, size=len(data), storage_key=content_key(sha)
    )


async def rendered(
    harness: ApiHarness, sub: Submitted, *, provenance: str = "real", preset: str = "tiktok_1080x1920_30"
) -> dict[str, Any]:
    """A final render, captions (spoken language and a pending German translation) and a draft
    packaging with two thumbnail candidates."""
    async with harness.services.db.transaction() as session:
        video = await artifact(harness, session, f"video {uuid.uuid4()}".encode(), "video", "video/mp4")
        render = Render(
            org_id=ALEX.ORG_ID,
            version_id=sub.version_id,
            preset_id=preset,
            aspect="9:16",
            artifact_id=video,
            provenance_mode=provenance,
            c2pa_manifest={"claim": "test"} if provenance == "real" else None,
            status="ready",
        )
        session.add(render)
        captions: dict[str, UUID] = {}
        for language, state in (("en", "n/a"), ("de", "pending")):
            for fmt in ("ass", "srt"):
                file = await artifact(
                    harness, session, f"{language}.{fmt}.{uuid.uuid4()}".encode(), "captions", "text/plain"
                )
                row = await rec.upsert_caption(
                    session, ALEX.ORG_ID, version_id=sub.version_id, language=language, format=fmt,
                    style_id="bold_pop_highlight", artifact_id=file, review_state=state,
                )  # fmt: skip
                captions[f"{language}.{fmt}"] = row.id
        thumbs = [
            await artifact(harness, session, f"png {i} {uuid.uuid4()}".encode(), "image", "image/png") for i in range(2)
        ]
        packaging = Packaging(
            org_id=ALEX.ORG_ID,
            version_id=sub.version_id,
            platform="tiktok",
            title="Why agents are not chatbots",
            description="Agents plan, act and check their work.",
            hashtags=["ai", "agents"],
            thumbnail_candidates=[{"artifact_id": str(t), "text": "Agents ≠ chatbots"} for t in thumbs],
            thumbnail_artifact_ids=[thumbs[0]],
            limits={},
            generator={"kind": "template", "thumbnail_texts": ["Agents ≠ chatbots"]},
        )
        session.add(packaging)
        await session.flush()
        return {"render": render.id, "captions": captions, "packaging": packaging.id, "thumbs": thumbs}


async def test_platforms_label_where_each_limit_comes_from(harness: ApiHarness, editor: ApiTenant) -> None:
    platforms = {p["id"]: p for p in (await editor.client.get("/v1/platforms")).json()}
    tiktok = platforms["tiktok"]
    assert tiktok["verified_at"] is None  # platform rules stay unverified (D16)
    assert set(tiktok["limits"]["sources"].values()) == {"design_default"}
    assert tiktok["limits"]["title_max_chars"] == harness.services.config.packaging.design_limits.title_max_chars
    assert any(p["id"] == "tiktok_1080x1920_30" for p in tiktok["presets"])
    assert {c["key"] for c in tiktok["checklist"]} >= {"not_mass_produced", "visible_label_reviewed"}


async def test_translation_is_an_auto_applied_caption_edit(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    url = f"/v1/versions/{sub.version_id}/captions:translate"
    assert (await editor.client.post(url, json={"language": "az"}, headers=key())).status_code == 422
    assert (await editor.client.post(url, json={"language": "en"}, headers=key())).status_code == 409
    accepted = await editor.client.post(url, json={"language": "de"}, headers=key())
    assert accepted.status_code == 202, accepted.text
    ids = accepted.json()
    await harness.services.drain()
    proposed = await propose_edit(harness.exec, ALEX.ORG_ID, UUID(ids["job_id"]))  # type: ignore[attr-defined]
    assert proposed["status"] in ("applying", "applied", "proposed"), proposed
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    (op,) = edit["ops"]
    assert op["op"] == "set_captions" and op["changes"]["translations"] == [{"language": "de"}]
    assert {k for k, v in op["changes"].items() if v is not None} == {"translations"}  # nothing else changes
    assert edit["selection"]["kind"] == "captions_translate"


async def test_translation_review_and_its_carry_over(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    made = await rendered(harness, sub)
    spoken = await editor.client.post(f"/v1/captions/{made['captions']['en.ass']}:approve")
    assert spoken.status_code == 409
    approved = await editor.client.post(f"/v1/captions/{made['captions']['de.srt']}:approve", json={"note": "ok"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["review_state"] == "approved" and len(approved.json()["caption_ids"]) == 2
    listed = (await editor.client.get(f"/v1/versions/{sub.version_id}/captions")).json()
    assert {c["language"]: c["review_state"] for c in listed} == {"en": "n/a", "de": "approved"}
    async with harness.services.db.transaction() as session:  # another version of the video, same file
        old = await session.get_one(Caption, made["captions"]["de.ass"])
        other_id = uuid.uuid4()
        session.add(
            VideoVersion(
                **(
                    example_version_row(ALEX.ORG_ID)
                    | {"id": other_id, "video_id": sub.video_id, "number": 2, "state": "ready"}
                )
            )
        )
        await session.flush()
        carried = await rec.upsert_caption(
            session, ALEX.ORG_ID, version_id=other_id, language="de", format="ass",
            style_id="bold_pop_highlight", artifact_id=old.artifact_id, review_state="pending",
        )  # fmt: skip
        assert carried.review_state == "approved" and carried.reviewed_by == editor.user_id
        changed_file = await artifact(harness, session, b"retranslated", "captions", "text/plain")
        changed = await rec.upsert_caption(
            session, ALEX.ORG_ID, version_id=other_id, language="de", format="ass",
            style_id="bold_pop_highlight", artifact_id=changed_file, review_state="pending",
        )  # fmt: skip
        assert changed.review_state == "pending" and changed.reviewed_by is None  # new text, new review


async def test_packaging_edits_are_validated_and_withdraw_approval(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    made = await rendered(harness, sub)
    url = f"/v1/packaging/{made['packaging']}"
    limit = harness.services.config.packaging.design_limits.title_max_chars
    too_long = await editor.client.patch(url, json={"title": "x" * (limit + 1)})
    assert too_long.status_code == 422 and too_long.json()["issues"][0]["code"] == "packaging_limit"
    bad_tag = await editor.client.patch(url, json={"hashtags": ["two words"]})
    assert bad_tag.status_code == 422
    foreign = await editor.client.patch(url, json={"thumbnail_artifact_id": str(uuid.uuid4())})
    assert foreign.status_code == 422
    picked = await editor.client.patch(
        url, json={"thumbnail_artifact_id": str(made["thumbs"][1]), "hashtags": ["#AI", "ai", "Agents"]}
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["hashtags"] == ["AI", "Agents"] and picked.json()["thumbnail_artifact_ids"] == [
        str(made["thumbs"][1])
    ]
    approved = await editor.client.post(f"{url}:approve")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    edited = await editor.client.patch(url, json={"cta_text": "Follow for part two"})
    assert edited.json()["status"] == "draft" and edited.json()["approved_by"] is None
    listed = (await editor.client.get(f"/v1/versions/{sub.version_id}/packaging")).json()
    assert [p["id"] for p in listed] == [str(made["packaging"])]
    thumb = await editor.client.get(f"{url}/thumbnails/{made['thumbs'][0]}")
    assert thumb.status_code == 200 and thumb.json()["filename"] == "thumbnail.tiktok.png"


async def test_package_requests_are_checked(harness: ApiHarness, editor: ApiTenant) -> None:
    planned = await submitted(harness, state="approved")
    started: list[Any] = harness.started  # type: ignore[attr-defined]
    accepted = await editor.client.post(f"/v1/versions/{planned.version_id}:package", json={}, headers=key())
    assert accepted.status_code == 202 and accepted.json()["platforms"] == ["tiktok"]  # the version's targets
    assert started[-1][0] == "PackagingWorkflow" and started[-1][1]["args"] == {"platforms": ["tiktok"]}
    unknown = await editor.client.post(
        f"/v1/versions/{planned.version_id}:package", json={"platforms": ["myspace"]}, headers=key()
    )
    assert unknown.status_code == 422
    early = await submitted(harness, state="previz_ready")
    assert (
        await editor.client.post(f"/v1/versions/{early.version_id}:package", json={}, headers=key())
    ).status_code == 409


async def test_every_export_rule_then_the_export(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    mock = await rendered(harness, sub, provenance="mock_dev")
    url = f"/v1/renders/{mock['render']}/exports"
    body = {"platform": "tiktok", "packaging_id": str(mock["packaging"]), "disclosure_checklist": CHECKLIST}
    refused = await editor.client.post(url, json=body, headers=key())
    assert refused.status_code == 409 and "mock provenance" in refused.json()["detail"].lower()

    research = {
        "claims": [{"key": "clm_1", "text": "90% fail.", "verdict": "unsupported", "evidence_ids": []}],
        "source_ids": [],
        "closed_book": False,
    }
    sub = await submitted(harness, research=research)
    made = await rendered(harness, sub)
    url = f"/v1/renders/{made['render']}/exports"
    body = {"platform": "tiktok", "packaging_id": str(made["packaging"]), "disclosure_checklist": CHECKLIST}
    youtube = await editor.client.post(url, json={**body, "platform": "youtube_shorts"}, headers=key())
    assert youtube.status_code == 422  # a TikTok preset, or a packaging of another platform
    unchecked = await editor.client.post(
        url, json={**body, "disclosure_checklist": {"ai_generated_label": True}}, headers=key()
    )
    assert unchecked.status_code == 422 and {i["code"] for i in unchecked.json()["issues"]} == {"checklist_unchecked"}
    draft = await editor.client.post(url, json=body, headers=key())
    assert draft.status_code == 409 and "approve the packaging" in draft.json()["detail"]
    assert (await editor.client.post(f"/v1/packaging/{made['packaging']}:approve")).status_code == 200
    async with harness.services.db.transaction() as session:
        claim = Claim(
            org_id=ALEX.ORG_ID, video_id=sub.video_id, claim_key="clm_1", first_version_id=sub.version_id,
            text="90% fail.", verdict="unsupported", blocking=True, overridable=True,
        )  # fmt: skip
        session.add(claim)
    blocked = await editor.client.post(url, json=body, headers=key())
    assert blocked.status_code == 409 and "unsupported claims" in blocked.json()["detail"]
    override = await editor.client.post(f"/v1/claims/{claim.id}:override", json={"reason": "Our own survey."})
    assert override.status_code == 200
    pending = await editor.client.post(url, json={**body, "caption_languages": ["en", "de"]}, headers=key())
    assert pending.status_code == 409 and "de translation is pending" in pending.json()["detail"]
    started: list[Any] = harness.started  # type: ignore[attr-defined]
    before = len(started)
    export_key = key()
    created = await editor.client.post(url, json=body, headers=export_key)
    assert created.status_code == 201, created.text
    export = created.json()
    # Regression (audit A7): a retry with the same key returned the stored answer, whose presigned
    # URLs expire long before the idempotency record does; now the downloads are signed again.
    from ce_db.models.tenancy import IdempotencyKey

    async with harness.services.db.transaction() as session:
        row = (
            await session.execute(sa.select(IdempotencyKey).where(IdempotencyKey.key == export_key["Idempotency-Key"]))
        ).scalar_one()
        stale = dict(row.response or {})
        stale["body"] = {
            **stale["body"],
            "downloads": {"video": {"url": "http://expired.invalid/", "expires_at": None}},
        }
        row.response = stale
    replayed = await editor.client.post(url, json=body, headers=export_key)
    assert replayed.status_code == 201 and replayed.json()["id"] == export["id"]
    assert replayed.json()["downloads"]["video"]["url"] != "http://expired.invalid/"
    meta = export["metadata_doc"]
    assert meta["provenance"]["mode"] == "real" and meta["provenance"]["ai_generated"] is True
    assert meta["packaging"]["title"] == "Why agents are not chatbots"
    assert meta["packaging"]["hashtags"] == ["#ai", "#agents"] and meta["packaging"]["description"].endswith(
        "#ai #agents"
    )
    assert {(c["language"], c["format"]) for c in meta["captions"]} == {("en", "ass"), ("en", "srt")}  # de is pending
    assert meta["platform"]["rules_verified"] is False and meta["disclosure_checklist"] == CHECKLIST
    assert set(export["downloads"]) >= {"video", "metadata", "thumbnail", "captions.en.ass", "captions.en.srt"}
    assert [w for w, _ in started[before:]] == ["MemoryUpdateWorkflow"]
    assert started[-1][1]["args"]["trigger"] == "exported" and started[-1][1]["target_id"] == str(sub.version_id)
    assert export["id"] in {e["id"] for e in (await editor.client.get(f"/v1/versions/{sub.version_id}/exports")).json()}
    assert (await editor.client.get(f"/v1/exports/{export['id']}")).json()["metadata_doc"] == meta
    async with harness.services.db.session() as session:
        actions = (
            (await session.execute(sa.select(AuditLog.action).where(AuditLog.target_id == export["id"])))
            .scalars()
            .all()
        )
        job = (
            await session.execute(
                sa.select(GenerationJob).where(
                    GenerationJob.kind == "memory_update", GenerationJob.target_id == sub.version_id
                )
            )
        ).scalar_one()
    assert actions == ["export.create"] and job.input["trigger"] == "exported"
    approved_de = await editor.client.post(f"/v1/captions/{made['captions']['de.ass']}:approve")
    assert approved_de.status_code == 200
    with_de = (await editor.client.post(url, json=body, headers=key())).json()
    assert ("de", "srt") in {(c["language"], c["format"]) for c in with_de["metadata_doc"]["captions"]}

    not_ready = await submitted(harness, state="needs_review")
    late = await rendered(harness, not_ready)
    await editor.client.post(f"/v1/packaging/{late['packaging']}:approve")
    review = await editor.client.post(
        f"/v1/renders/{late['render']}/exports", json={**body, "packaging_id": str(late["packaging"])}, headers=key()
    )
    assert review.status_code == 409 and "ready version" in review.json()["detail"]


async def test_exports_and_packaging_are_org_scoped(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    made = await rendered(harness, sub)
    stranger = await harness.new_tenant()
    for method, path, body in (
        ("POST", f"/v1/versions/{sub.version_id}/captions:translate", {"language": "de"}),
        ("POST", f"/v1/captions/{made['captions']['de.ass']}:approve", {}),
        ("POST", f"/v1/versions/{sub.version_id}:package", {}),
        ("GET", f"/v1/versions/{sub.version_id}/packaging", None),
        ("PATCH", f"/v1/packaging/{made['packaging']}", {"title": "theirs"}),
        ("POST", f"/v1/packaging/{made['packaging']}:approve", None),
        ("GET", f"/v1/packaging/{made['packaging']}/thumbnails/{made['thumbs'][0]}", None),
        ("POST", f"/v1/renders/{made['render']}/exports", {"platform": "tiktok", "disclosure_checklist": CHECKLIST}),
        ("GET", f"/v1/versions/{sub.version_id}/exports", None),
    ):
        response = await stranger.client.request(method, path, json=body, headers=key())
        assert response.status_code == 404, (method, path, response.status_code, response.text)
    async with harness.services.db.session() as session:
        packaging = await session.get_one(Packaging, made["packaging"])
    assert packaging.title == "Why agents are not chatbots"
