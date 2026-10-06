"""Research sources and the claim ledger over HTTP (Phase 12, ADR 0058): sources are registered and
ingested by a `research_ingest` job (the API reads nothing itself); obviously unfetchable URLs and
unreadable documents are refused before any job; the ingestion stages extract facts with spans and
store embeddings; the claim ledger is read per version and overridden only in open-book videos;
every route is org-scoped (I12).

Workflow starts are recorded, and the job's stages run in-process here; the whole workflow through
Temporal is `tests/e2e/test_research_mock.py`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services, upload_asset
from ce_db.models.platform import AuditLog
from ce_db.models.research import Claim, ResearchFact, ResearchSource
from ce_db.models.videos import Project, Video, VideoVersion
from ce_exec.context import build_services as build_exec_services
from ce_exec.studio import StudioContext, run_stage
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX
from ce_testing.placeholders import placeholder_pdf, placeholder_png
from ce_testing.seed import example_version_row

pytestmark = [pytest.mark.infra]


@pytest_asyncio.fixture
async def harness(seeded_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(
        seeded_db.url, storage="local_fs", storage_root=tmp_path / "storage", extra_env={"MOCK_GPU": "true"}
    )
    for bucket in (services.settings.s3_bucket_assets, services.settings.s3_bucket_artifacts):
        await services.storage.ensure_bucket(bucket)
    h = ApiHarness(services)
    h.exec = build_exec_services(services.effective, pool_size=2)  # type: ignore[attr-defined]
    h.exec.storage = services.storage  # type: ignore[attr-defined]  # the same local storage root
    yield h
    await h.exec.close()  # type: ignore[attr-defined]
    await h.aclose()


@pytest_asyncio.fixture
async def editor(harness: ApiHarness) -> ApiTenant:
    return await harness.add_member(ALEX.ORG_ID, "editor")


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _record(harness: ApiHarness) -> list[tuple[str, Any]]:
    started: list[tuple[str, Any]] = []

    async def start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        started.append((workflow, arg))

    harness.services.workflows.start = start  # type: ignore[method-assign]
    return started


async def _ingest(harness: ApiHarness, ctx: dict[str, Any]) -> dict[str, Any]:
    """Runs the job's stages in-process; the embedding calls get unit vectors of EMBEDDING_DIM."""
    svc = harness.exec  # type: ignore[attr-defined]
    context = StudioContext.model_validate(ctx)
    step = await run_stage(svc, context, "start", {})
    if step.done:
        return dict(step.result or {})
    dim = int(svc.settings.embedding_dim)
    outputs = [
        {"key": c.key, "result": {"vectors": [[1.0] + [0.0] * (dim - 1) for _ in c.request["texts"]]}}
        for c in step.calls
    ]
    done = await run_stage(svc, context, step.next or "", {**step.data, "outputs": outputs})
    assert done.done
    return dict(done.result or {})


async def test_a_note_is_ingested_into_facts_with_spans_and_embeddings(harness: ApiHarness, editor: ApiTenant) -> None:
    started = _record(harness)
    text = "Our customers save 3 hours a week. " * 3 + "They also report fewer late payments."
    created = await editor.client.post(
        f"/v1/projects/{ALEX.PROJECT_ID}/sources",
        json={"kind": "note", "title": "Interview notes", "text": text},
        headers={"Idempotency-Key": "note-1"},
    )
    assert created.status_code == 202, created.text
    again = await editor.client.post(
        f"/v1/projects/{ALEX.PROJECT_ID}/sources",
        json={"kind": "note", "title": "Interview notes", "text": text},
        headers={"Idempotency-Key": "note-1"},
    )
    assert again.json() == created.json() and len(started) == 1  # replayed, no second job
    workflow, ctx = started[0]
    assert workflow == "ResearchIngestWorkflow" and ctx["kind"] == "research_ingest"
    source_id = created.json()["source_id"]
    pending = (await editor.client.get(f"/v1/sources/{source_id}")).json()
    assert pending["status"] == "pending" and pending["trust"] == "user_provided" and pending["facts"] == []

    result = await _ingest(harness, ctx)
    assert result["facts"] >= 1 and result["embedded"] == result["facts"], result
    source = (await editor.client.get(f"/v1/sources/{source_id}")).json()
    assert source["status"] == "ingested" and source["fact_count"] == result["facts"]
    assert source["embedding_model"].startswith("mock_embed:") and source["extract"]["characters"] == len(text)
    for fact in source["facts"]:
        assert text[fact["quote_span"]["start"] : fact["quote_span"]["end"]] == fact["text"]
        assert fact["embedding_model"] == source["embedding_model"]
    listed = (await editor.client.get(f"/v1/projects/{ALEX.PROJECT_ID}/sources")).json()["items"]
    assert source_id in {s["id"] for s in listed}

    reingest = await editor.client.post(f"/v1/sources/{source_id}:reingest")
    assert reingest.status_code == 202 and started[-1][0] == "ResearchIngestWorkflow"
    await _ingest(harness, started[-1][1])
    async with harness.services.db.session() as session:  # replaced, not duplicated (stable fact ids)
        count = await session.scalar(
            sa.select(sa.func.count()).select_from(ResearchFact).where(ResearchFact.source_id == uuid.UUID(source_id))
        )
    assert count == result["facts"]
    deleted = await editor.client.delete(f"/v1/sources/{source_id}")
    assert deleted.status_code == 204
    assert (await editor.client.get(f"/v1/sources/{source_id}")).status_code == 404


async def test_an_uploaded_pdf_is_read_from_storage(harness: ApiHarness, editor: ApiTenant) -> None:
    pdf = placeholder_pdf(["Field study 2026.", "Battery life improved 18 percent after the update."])
    asset = await upload_asset(harness, editor, pdf, mime="application/pdf", kind="document", filename="study.pdf")
    started = _record(harness)
    created = await editor.client.post(
        f"/v1/projects/{ALEX.PROJECT_ID}/sources", json={"kind": "pdf", "asset_id": asset["id"]}, headers=_key()
    )
    assert created.status_code == 202, created.text
    await _ingest(harness, started[-1][1])
    source = (await editor.client.get(f"/v1/sources/{created.json()['source_id']}")).json()
    assert source["status"] == "ingested" and source["mime"] == "application/pdf" and source["extract"]["pages"] == 1
    assert any("18 percent" in f["text"] for f in source["facts"])


async def test_unreadable_inputs_are_refused_before_any_job(harness: ApiHarness, editor: ApiTenant) -> None:
    image = await upload_asset(harness, editor, placeholder_png("logo"), mime="image/png", kind="image")
    started = _record(harness)  # after the upload: its validation job must run
    url = f"/v1/projects/{ALEX.PROJECT_ID}/sources"
    for uri in (
        "http://127.0.0.1/admin",
        "file:///etc/passwd",
        "https://user:pw@example.com/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost:80/",
        "https://example.com:8443/",
    ):
        refused = await editor.client.post(url, json={"kind": "url", "uri": uri}, headers=_key())
        assert refused.status_code == 422, (uri, refused.text)
        assert refused.json()["issues"][0]["code"] == "ssrf_blocked"
    both = await editor.client.post(
        url, json={"kind": "note", "text": "x", "uri": "https://example.com"}, headers=_key()
    )
    assert both.status_code == 422
    as_pdf = await editor.client.post(url, json={"kind": "pdf", "asset_id": image["id"]}, headers=_key())
    assert as_pdf.status_code == 422 and as_pdf.json()["issues"][0]["code"] == "unsupported_document"
    assert [s for s in started if s[0] == "ResearchIngestWorkflow"] == []


async def test_a_url_blocked_at_fetch_time_marks_the_source_failed(harness: ApiHarness, editor: ApiTenant) -> None:
    started = _record(harness)
    created = await editor.client.post(
        f"/v1/projects/{ALEX.PROJECT_ID}/sources",
        json={"kind": "url", "uri": "https://internal.example.test/x"},
        headers=_key(),
    )
    assert created.status_code == 202
    svc = harness.exec  # type: ignore[attr-defined]

    async def private(host: str, port: int) -> list[str]:  # DNS points at a private address
        return ["10.0.0.7"]

    svc.fetch_resolver = private

    with pytest.raises(Exception, match="ssrf_blocked"):
        await run_stage(svc, StudioContext.model_validate(started[-1][1]), "start", {})
    source = (await editor.client.get(f"/v1/sources/{created.json()['source_id']}")).json()
    assert source["status"] == "failed" and source["error"]["code"] == "ssrf_blocked"


async def _video(harness: ApiHarness) -> tuple[uuid.UUID, uuid.UUID]:
    async with harness.services.db.transaction() as session:  # a project of its own: the seeded one stays as seeded
        project = Project(org_id=ALEX.ORG_ID, name=f"research {uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        video = Video(org_id=ALEX.ORG_ID, project_id=project.id)
        session.add(video)
        await session.flush()
        row = example_version_row(ALEX.ORG_ID) | {"id": uuid.uuid4(), "video_id": video.id, "state": "previz_ready"}
        session.add(VideoVersion(**row))
    return video.id, row["id"]


async def _claims(harness: ApiHarness, editor: ApiTenant) -> tuple[uuid.UUID, dict[str, Claim]]:
    video_id, version_id = await _video(harness)
    rows = {
        "open": Claim(
            org_id=ALEX.ORG_ID,
            video_id=video_id,
            claim_key="clm_1",
            first_version_id=version_id,
            last_version_id=version_id,
            text="Most people misread agents.",
            verdict="unsupported",
            blocking=True,
            overridable=True,
        ),
        "closed": Claim(
            org_id=ALEX.ORG_ID,
            video_id=video_id,
            claim_key="clm_2",
            first_version_id=version_id,
            text="80% of projects stall.",
            verdict="unsupported",
            closed_book=True,
            blocking=True,
            overridable=False,
        ),
        "supported": Claim(
            org_id=ALEX.ORG_ID,
            video_id=video_id,
            claim_key="clm_3",
            first_version_id=version_id,
            text="Agents plan steps.",
            verdict="supported",
        ),
    }
    async with harness.services.db.transaction() as session:
        session.add_all(rows.values())
    return version_id, rows


async def test_the_claim_ledger_and_its_overrides(harness: ApiHarness, editor: ApiTenant) -> None:
    version_id, rows = await _claims(harness, editor)
    ledger = (await editor.client.get(f"/v1/versions/{version_id}/claims")).json()
    assert [c["claim_key"] for c in ledger] == ["clm_1", "clm_2", "clm_3"]
    closed = await editor.client.post(f"/v1/claims/{rows['closed'].id}:override", json={"reason": "trust me"})
    assert closed.status_code == 409 and "closed-book" in closed.json()["detail"]
    supported = await editor.client.post(f"/v1/claims/{rows['supported'].id}:override", json={"reason": "why"})
    assert supported.status_code == 409
    viewer = await harness.add_member(ALEX.ORG_ID, "viewer")
    denied = await viewer.client.post(f"/v1/claims/{rows['open'].id}:override", json={"reason": "viewer"})
    assert denied.status_code == 403
    ok = await editor.client.post(f"/v1/claims/{rows['open'].id}:override", json={"reason": "Our own survey."})
    assert ok.status_code == 200, ok.text
    assert ok.json()["override_by"] == str(editor.user_id) and ok.json()["override_reason"] == "Our own survey."
    async with harness.services.db.session() as session:
        actions = (
            (await session.execute(sa.select(AuditLog.action).where(AuditLog.target_id == str(rows["open"].id))))
            .scalars()
            .all()
        )
    assert actions == ["claim.override"]


async def test_research_routes_are_org_scoped(harness: ApiHarness, editor: ApiTenant) -> None:
    """I12: another organization sees and changes nothing of these (404, never 403)."""
    version_id, rows = await _claims(harness, editor)
    started = _record(harness)
    created = await editor.client.post(
        f"/v1/projects/{ALEX.PROJECT_ID}/sources",
        json={"kind": "note", "text": "A private fact."},
        headers=_key(),
    )
    source_id = created.json()["source_id"]
    assert started
    stranger = await harness.new_tenant()
    for method, path, body in (
        ("POST", f"/v1/projects/{ALEX.PROJECT_ID}/sources", {"kind": "note", "text": "x"}),
        ("GET", f"/v1/projects/{ALEX.PROJECT_ID}/sources", None),
        ("GET", f"/v1/sources/{source_id}", None),
        ("POST", f"/v1/sources/{source_id}:reingest", None),
        ("DELETE", f"/v1/sources/{source_id}", None),
        ("GET", f"/v1/versions/{version_id}/claims", None),
        ("POST", f"/v1/claims/{rows['open'].id}:override", {"reason": "not mine"}),
    ):
        response = await stranger.client.request(method, path, json=body, headers=_key())
        assert response.status_code == 404, (method, path, response.status_code)
    async with harness.services.db.session() as session:
        source = await session.get(ResearchSource, uuid.UUID(source_id))
        claim = await session.get_one(Claim, rows["open"].id)
    assert source is not None and claim.override_by is None
