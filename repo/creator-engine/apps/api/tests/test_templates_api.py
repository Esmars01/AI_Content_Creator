"""Spec templates and brand kits over HTTP (Phase 12, ADR 0059): save (capture, body, composition),
strict validation against the configuration and the organization, immutable versions, the compose
preview, apply as an ordinary edit proposal (validated, applied, recorded in `meta.template_ids`),
brand-kit CRUD with the project default, and organization scoping (I12).

Workflow starts are recorded; the edit jobs' bodies run in-process (see test_edits_api.py)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services, upload_asset
from ce_db.models.platform import AuditLog
from ce_db.models.videos import Project, VideoVersion
from ce_exec.context import build_services as build_exec_services
from ce_exec.editing import propose_edit, run_apply_job
from ce_exec.submit import Submitted, submit_spec
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX, two_scene_spec_dict
from ce_testing.placeholders import placeholder_png, placeholder_wav

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
    yield h
    await h.exec.close()  # type: ignore[attr-defined]
    await h.aclose()


@pytest_asyncio.fixture
async def editor(harness: ApiHarness) -> ApiTenant:
    return await harness.add_member(ALEX.ORG_ID, "editor")


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def record(harness: ApiHarness) -> list[tuple[str, Any]]:
    started: list[tuple[str, Any]] = []

    async def start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        started.append((workflow, arg))

    harness.services.workflows.start = start  # type: ignore[method-assign]
    return started


async def submitted(harness: ApiHarness) -> Submitted:
    spec = two_scene_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    async with harness.services.db.transaction() as session:
        project = Project(org_id=ALEX.ORG_ID, name=f"templates {uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        project_id = project.id
    return await submit_spec(harness.exec, org_id=ALEX.ORG_ID, project_id=project_id, spec_data=spec)  # type: ignore[attr-defined]


async def spec_of(harness: ApiHarness, version_id: UUID | str) -> dict[str, Any]:
    async with harness.services.db.session() as session:
        return dict((await session.get_one(VideoVersion, UUID(str(version_id)))).spec)


async def test_capture_save_version_and_list(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    spec = await spec_of(harness, sub.version_id)
    created = await editor.client.post(
        "/v1/spec-templates",
        json={"name": "Captions", "kind": "caption", "from_version_id": str(sub.version_id), "paths": ["/captions"]},
    )
    assert created.status_code == 201, created.text
    first = created.json()
    assert first["version"] == 1 and first["source_version_id"] == str(sub.version_id)
    assert first["body"]["captions"]["style_id"] == spec["captions"]["style_id"]
    assert set(first["body"]) == {"captions"} and first["latest"] is True and first["conflicts"] == []
    patched = await editor.client.patch(
        f"/v1/spec-templates/{first['id']}", json={"body": {"captions": {"style_id": "minimal_lower"}}}
    )
    assert patched.status_code == 201, patched.text
    second = patched.json()
    assert second["version"] == 2 and second["parent_template_id"] == first["id"]
    assert second["body"] == {"captions": {"style_id": "minimal_lower"}} and second["id"] != first["id"]
    stale = await editor.client.patch(f"/v1/spec-templates/{first['id']}", json={"name": "x"})
    assert stale.status_code == 409  # only the latest version changes
    old = (await editor.client.get(f"/v1/spec-templates/{first['id']}")).json()
    assert old["latest"] is False and old["body"] == first["body"]  # immutable
    latest = (await editor.client.get("/v1/spec-templates", params={"kind": "caption"})).json()["items"]
    assert second["id"] in {t["id"] for t in latest} and first["id"] not in {t["id"] for t in latest}
    every = (await editor.client.get("/v1/spec-templates", params={"all_versions": True})).json()["items"]
    assert {first["id"], second["id"]} <= {t["id"] for t in every}
    archived = await editor.client.delete(f"/v1/spec-templates/{second['id']}")
    assert archived.status_code == 204
    listed = (await editor.client.get("/v1/spec-templates", params={"kind": "caption"})).json()["items"]
    assert second["id"] not in {t["id"] for t in listed}


async def test_invalid_templates_are_refused(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    kit, template = str(uuid.uuid4()), str(uuid.uuid4())
    outputs = [{"preset_id": "nope", "aspect": "9:16"}]
    cases: list[tuple[dict[str, Any], str]] = [
        ({"kind": "caption", "body": {"captions": {"style_id": "no_such_style"}}}, "unknown_caption_style"),
        ({"kind": "camera", "body": {"captions": {"style_id": "bold_pop_highlight"}}}, "template_body"),
        ({"kind": "video", "body": {"script": {"segments": []}}}, "template_body"),
        ({"kind": "video", "body": {"render": {"outputs": outputs}}}, "unknown_preset"),
        ({"kind": "scene", "body": {"shot_defaults": {"camera": {"framing": "sideways"}}}}, "unknown_vocab"),
        ({"kind": "brand", "body": {"brand": {"brand_kit_id": kit}}}, "unknown_brand_kit"),
        ({"kind": "video", "composes_from": [template]}, "unknown_template"),
        ({"kind": "camera", "from_version_id": str(sub.version_id), "paths": ["/captions"]}, "template_paths"),
    ]
    for payload, code in cases:
        response = await editor.client.post("/v1/spec-templates", json={"name": "x", **payload})
        assert response.status_code == 422, (payload, response.text)
        assert code in {i["code"] for i in response.json()["issues"]}, (code, response.json())
    both = await editor.client.post(
        "/v1/spec-templates", json={"name": "x", "kind": "video", "body": {}, "from_version_id": str(sub.version_id)}
    )
    assert both.status_code == 422


async def test_composition_preview_and_composed_templates(harness: ApiHarness, editor: ApiTenant) -> None:
    async def make(name: str, kind: str, body: dict[str, Any], composes: list[str] | None = None) -> dict[str, Any]:
        response = await editor.client.post(
            "/v1/spec-templates", json={"name": name, "kind": kind, "body": body, "composes_from": composes or []}
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    loud = await make("Loud", "caption", {"captions": {"style_id": "bold_pop_highlight", "highlight": "active_word"}})
    quiet = await make("Quiet", "caption", {"captions": {"style_id": "minimal_lower"}})
    fast = await make("Fast", "scene", {"scene_defaults": {"pacing": {"cut_cadence": "fast"}}})
    preview = await editor.client.post("/v1/spec-templates:compose", json={"template_ids": [loud["id"], quiet["id"]]})
    assert preview.status_code == 200
    out = preview.json()
    assert out["body"]["captions"] == {"style_id": "minimal_lower", "highlight": "active_word"}
    assert [c["path"] for c in out["conflicts"]] == ["/captions/style_id"]
    assert out["conflicts"][0]["winner"] == "minimal_lower" and out["sources"]["/captions/highlight"] == loud["id"]
    house = await make(
        "House", "video", {"meta": {"platform_targets": ["tiktok"]}}, [loud["id"], quiet["id"], fast["id"]]
    )
    assert house["effective"]["captions"]["style_id"] == "minimal_lower"
    assert house["effective"]["scene_defaults"] == {"pacing": {"cut_cadence": "fast"}}
    assert house["conflicts"] and house["sources"]["/meta/platform_targets"] == house["id"]
    narrow = await editor.client.post(
        "/v1/spec-templates", json={"name": "Cam", "kind": "camera", "body": {}, "composes_from": [loud["id"]]}
    )
    assert narrow.status_code == 422  # the composition holds slots a camera template cannot


async def test_apply_is_an_edit_proposal_recorded_in_the_spec(harness: ApiHarness, editor: ApiTenant) -> None:
    sub = await submitted(harness)
    template = (
        await editor.client.post(
            "/v1/spec-templates",
            json={
                "name": "Punchy",
                "kind": "video",
                "body": {
                    "captions": {"style_id": "minimal_lower", "highlight": "phrase"},
                    "scene_defaults": {"pacing": {"cut_cadence": "fast"}},
                    "provenance": {"visible_label": "on"},
                },
            },
        )
    ).json()
    url = f"/v1/spec-templates/{template['id']}:apply"
    preview = await editor.client.post(url, json={"version_id": str(sub.version_id), "preview": True})
    assert preview.status_code == 200, preview.text
    assert {o["op"] for o in preview.json()["operations"]} == {
        "set_meta",
        "set_captions",
        "set_pacing",
        "set_provenance_label",
    }
    started = record(harness)
    accepted = await editor.client.post(url, json={"version_id": str(sub.version_id)}, headers=key())
    assert accepted.status_code == 202, accepted.text
    ids = accepted.json()
    await harness.services.drain()
    assert [w for w, _ in started] == ["ProposeEditWorkflow"]  # one propose job, nothing else
    proposed = await propose_edit(harness.exec, ALEX.ORG_ID, UUID(ids["job_id"]))  # type: ignore[attr-defined]
    assert proposed["status"] == "proposed", proposed
    edit = (await editor.client.get(f"/v1/edits/{ids['edit_proposal_id']}")).json()
    assert edit["selection"]["kind"] == "from_template" and edit["selection"]["editor"]["template_id"] == template["id"]
    applied = (await editor.client.post(f"/v1/edits/{ids['edit_proposal_id']}:apply", headers=key())).json()
    await harness.services.drain()
    await run_apply_job(harness.exec, ALEX.ORG_ID, UUID(applied["job_id"]))  # type: ignore[attr-defined]
    spec = await spec_of(harness, applied["new_version_id"])
    assert spec["captions"]["style_id"] == "minimal_lower" and spec["provenance"]["visible_label"] == "on"
    assert all((s.get("pacing") or {}).get("cut_cadence") == "fast" for s in spec["scenes"])
    assert template["id"] in spec["meta"]["template_ids"]
    again = await editor.client.post(url, json={"version_id": applied["new_version_id"]}, headers=key())
    assert again.status_code == 409  # nothing left to change
    async with harness.services.db.session() as session:
        actions = (
            (await session.execute(sa.select(AuditLog.action).where(AuditLog.target_id == template["id"])))
            .scalars()
            .all()
        )
    assert "spec_template.apply" in actions and "spec_template.create" in actions


async def test_brand_kits_and_the_project_default(harness: ApiHarness, editor: ApiTenant) -> None:
    logo = await upload_asset(harness, editor, placeholder_png("acme"), mime="image/png", kind="logo")
    sound = await upload_asset(harness, editor, placeholder_wav(0.2), mime="audio/wav", kind="audio")
    created = await editor.client.post(
        "/v1/brand-kits",
        json={
            "name": "Acme",
            "logo_asset_id": logo["id"],
            "colors": {"primary": "#1A73E8"},
            "fonts": {"heading": "Inter"},
            "caption_style_id": "clean_subtitle",
        },
    )
    assert created.status_code == 201, created.text
    kit = created.json()
    for bad, code in (
        ({"name": "x", "colors": {"primary": "blue"}}, None),
        ({"name": "x", "caption_style_id": "nope"}, "unknown_caption_style"),
        ({"name": "x", "logo_asset_id": sound["id"]}, "logo_not_image"),
        ({"name": "x", "logo_asset_id": str(uuid.uuid4())}, "unknown_asset"),
        ({"name": "x", "v1": {"intro_asset_id": str(uuid.uuid4())}}, "v1_feature"),
    ):
        response = await editor.client.post("/v1/brand-kits", json=bad)
        assert response.status_code == 422, (bad, response.text)
        if code:
            assert code in {i["code"] for i in response.json()["issues"]}
    patched = await editor.client.patch(f"/v1/brand-kits/{kit['id']}", json={"colors": {"primary": "#000000"}})
    assert patched.status_code == 200 and patched.json()["colors"] == {"primary": "#000000"}
    assert patched.json()["logo_asset_id"] == logo["id"]
    cleared = await editor.client.patch(f"/v1/brand-kits/{kit['id']}", json={"clear_logo": True})
    assert cleared.json()["logo_asset_id"] is None

    project = (await editor.client.post("/v1/projects", json={"name": "Branded", "brand_kit_id": kit["id"]})).json()
    assert project["brand_kit_id"] == kit["id"]
    unknown = await editor.client.patch(f"/v1/projects/{project['id']}", json={"brand_kit_id": str(uuid.uuid4())})
    assert unknown.status_code == 422
    brand_template = await editor.client.post(
        "/v1/spec-templates",
        json={
            "name": "Acme brand",
            "kind": "brand",
            "body": {"brand": {"brand_kit_id": kit["id"], "logo_overlay": True}},
        },
    )
    assert brand_template.status_code == 201
    assert (await editor.client.delete(f"/v1/brand-kits/{kit['id']}")).status_code == 204
    listed = (await editor.client.get("/v1/brand-kits")).json()["items"]
    assert kit["id"] not in {k["id"] for k in listed}
    sub = await submitted(harness)
    stale = await editor.client.post(
        f"/v1/spec-templates/{brand_template.json()['id']}:apply",
        json={"version_id": str(sub.version_id), "preview": True},
    )
    assert stale.status_code == 422 and stale.json()["issues"][0]["code"] == "unknown_brand_kit"


async def test_templates_and_kits_are_org_scoped(harness: ApiHarness, editor: ApiTenant) -> None:
    """I12: another organization can neither see nor use them (404), nor apply them to its videos."""
    template = (
        await editor.client.post(
            "/v1/spec-templates",
            json={"name": "Mine", "kind": "caption", "body": {"captions": {"style_id": "minimal_lower"}}},
        )
    ).json()
    kit = (await editor.client.post("/v1/brand-kits", json={"name": "Mine"})).json()
    sub = await submitted(harness)
    stranger = await harness.new_tenant()
    tid, kid = template["id"], kit["id"]
    for method, path, body in (
        ("GET", f"/v1/spec-templates/{tid}", None),
        ("PATCH", f"/v1/spec-templates/{tid}", {"name": "theirs"}),
        ("DELETE", f"/v1/spec-templates/{tid}", None),
        ("POST", "/v1/spec-templates:compose", {"template_ids": [tid]}),
        ("POST", f"/v1/spec-templates/{tid}:apply", {"version_id": str(sub.version_id), "preview": True}),
        ("GET", f"/v1/brand-kits/{kid}", None),
        ("PATCH", f"/v1/brand-kits/{kid}", {"name": "theirs"}),
        ("DELETE", f"/v1/brand-kits/{kid}", None),
    ):
        response = await stranger.client.request(method, path, json=body, headers=key())
        assert response.status_code == 404, (method, path, response.status_code, response.text)
    theirs = await stranger.client.post(
        "/v1/spec-templates",
        json={"name": "x", "kind": "brand", "body": {"brand": {"brand_kit_id": kid}}},
    )
    assert theirs.status_code == 422  # another org's kit is unknown
    composed = await stranger.client.post(
        "/v1/spec-templates", json={"name": "x", "kind": "caption", "composes_from": [tid]}
    )
    assert composed.status_code == 422
    assert (await stranger.client.get("/v1/spec-templates")).json()["items"] == []
