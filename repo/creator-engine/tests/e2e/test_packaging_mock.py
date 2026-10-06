"""Phase 12 packaging, translation and export end to end on the Compose infrastructure (mock
engines, fixture LLM), through the HTTP API and Temporal:

- `captions:translate` adds a language as an auto-applied edit; the derived version builds a
  `captions.translate` node (the mock translator in dev — `captions_llm` is not smoke-validated)
  and its files arrive `pending`; approval reviews every format of the language;
- `:package` runs Director stage 12: with the fixture LLM there is no recorded packaging answer, so
  the labelled template packaging is used, within the (design-default) limits, with thumbnails
  rendered from the final render's frames;
- an export of a dev render is refused: its provenance is `mock_dev` (§32)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from ce_api.services import build_services
from ce_api.testing import ApiHarness
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra]

TIMEOUT_S = 900


@pytest_asyncio.fixture
async def api(stack: Stack) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(build_services(stack.effective, pool_size=4))
    harness.services.workflows.before_start = None  # the stack runs the orchestrator worker
    yield harness
    await harness.aclose()


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _wait(api: ApiHarness, job_id: str, tenant: Any) -> dict[str, Any]:
    for _ in range(TIMEOUT_S):
        job = (await tenant.client.get(f"/v1/jobs/{job_id}")).json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return dict(job)
        await asyncio.sleep(1.0)
    raise AssertionError(f"job {job_id} did not finish")


async def _version_ready(tenant: Any, version_id: str) -> dict[str, Any]:
    version: dict[str, Any] = {}
    for _ in range(TIMEOUT_S):
        response = await tenant.client.get(f"/v1/versions/{version_id}")
        version = response.json()
        if response.status_code == 200 and version["state"] in ("ready", "needs_review", "partial", "failed"):
            return dict(version)
        await asyncio.sleep(1.0)
    raise AssertionError(f"version {version_id} did not finish building: {version}")


async def test_translate_package_and_refuse_a_mock_export(stack: Stack, api: ApiHarness) -> None:
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    spec["meta"]["platform_targets"] = ["tiktok"]
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), TIMEOUT_S)
    assert result.state == "ready", (result.failed, result.statuses)
    editor = await api.add_member(ALEX.ORG_ID, "editor")
    version_id = str(submitted.version_id)

    translated = await editor.client.post(
        f"/v1/versions/{version_id}/captions:translate", json={"language": "de"}, headers=key()
    )
    assert translated.status_code == 202, translated.text
    derived = translated.json()["new_version_id"]
    assert (await _wait(api, translated.json()["job_id"], editor))["status"] == "succeeded"
    built = await _version_ready(editor, derived)
    assert built["state"] == "ready", built
    captions = (await editor.client.get(f"/v1/versions/{derived}/captions")).json()
    german = [c for c in captions if c["language"] == "de"]
    assert {c["format"] for c in german} >= {"ass"} and {c["review_state"] for c in german} == {"pending"}
    reviewed = await editor.client.post(f"/v1/captions/{german[0]['id']}:approve", json={"note": "checked"})
    assert reviewed.status_code == 200 and len(reviewed.json()["caption_ids"]) == len(german)

    packaged = await editor.client.post(f"/v1/versions/{version_id}:package", json={}, headers=key())
    assert packaged.status_code == 202, packaged.text
    job = await _wait(api, packaged.json()["job_id"], editor)
    assert job["status"] == "succeeded", job
    (packaging,) = (await editor.client.get(f"/v1/versions/{version_id}/packaging")).json()
    assert packaging["platform"] == "tiktok" and packaging["status"] == "draft"
    assert packaging["generator"]["kind"] == "template"  # the fixture LLM has no packaging answer: labelled
    assert any(i["code"] == "llm_fallback" for i in packaging["issues"])
    assert not [i for i in packaging["issues"] if i["code"] == "limit"]
    assert set(packaging["limits"]["sources"].values()) == {"design_default"}
    candidates = packaging["thumbnail_candidates"]
    assert len(candidates) == stack.effective.bundle.app.packaging.thumbnail_candidates
    assert packaging["thumbnail_artifact_ids"] == [candidates[0]["artifact_id"]]
    thumb = await editor.client.get(f"/v1/packaging/{packaging['id']}/thumbnails/{candidates[1]['artifact_id']}")
    assert thumb.status_code == 200
    approved = await editor.client.post(f"/v1/packaging/{packaging['id']}:approve")
    assert approved.status_code == 200, approved.text

    renders = (await editor.client.get(f"/v1/versions/{version_id}/renders")).json()
    final = next(
        r for r in (renders.get("items", renders) if isinstance(renders, dict) else renders) if not r["is_proxy"]
    )
    assert final["provenance_mode"] == "mock_dev"
    checklist = {
        c["key"]: True
        for c in next(p for p in (await editor.client.get("/v1/platforms")).json() if p["id"] == "tiktok")["checklist"]
    }
    refused = await editor.client.post(
        f"/v1/renders/{final['id']}/exports",
        json={"platform": "tiktok", "packaging_id": packaging["id"], "disclosure_checklist": checklist},
        headers=key(),
    )
    assert refused.status_code == 409 and "mock provenance" in refused.json()["detail"].lower()
