"""I12 at the API layer: a caller from org B can neither read nor change org A's resources.

Every route with a resource id in its path is listed below (a completeness check fails when a
new route is added without a cross-tenant case). Each case must answer 404 — another org's
resource is indistinguishable from a missing one — and org A's data must be unchanged.
List endpoints never include another org's rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from ce_api.testing import ApiHarness, ApiTenant, build_test_services, upload_asset
from ce_testing.database import TestDatabase
from ce_testing.fixtures import alex_appearance_dna, alex_creator_dna, grey_hoodie, home_office_world
from ce_testing.placeholders import placeholder_png

pytestmark = [pytest.mark.infra, pytest.mark.invariant]


@pytest_asyncio.fixture
async def harness(migrated_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(migrated_db.url, storage="local_fs", storage_root=tmp_path / "storage")
    await services.storage.ensure_bucket(services.settings.s3_bucket_assets)
    h = ApiHarness(services)
    yield h
    await h.aclose()


async def org_a_resources(harness: ApiHarness, a: ApiTenant) -> dict[str, str]:
    c = a.client
    ids: dict[str, str] = {}
    ids["project_id"] = (await c.post("/v1/projects", json={"name": "A"})).json()["id"]
    creator = (
        await c.post("/v1/creators", json={"name": "A", "dna": alex_creator_dna().model_dump(mode="json")})
    ).json()
    ids["creator_id"], ids["creator_version_id"] = creator["id"], creator["versions"][0]["id"]
    appearance = (
        await c.post(
            f"/v1/creators/{creator['id']}/appearances",
            json={"name": "A", "dna": alex_appearance_dna().model_dump(mode="json")},
        )
    ).json()
    ids["appearance_id"], ids["appearance_version_id"] = appearance["id"], appearance["versions"][0]["id"]
    wardrobe = (
        await c.post(
            f"/v1/creators/{creator['id']}/wardrobes", json={"name": "A", "spec": grey_hoodie().model_dump(mode="json")}
        )
    ).json()
    ids["wardrobe_id"], ids["wardrobe_version_id"] = wardrobe["id"], wardrobe["versions"][0]["id"]
    world = (await c.post("/v1/worlds", json={"dna": home_office_world().model_dump(mode="json")})).json()
    ids["world_id"], ids["world_version_id"] = world["id"], world["versions"][0]["id"]
    memory = await c.post(
        f"/v1/creators/{creator['id']}/memory",
        json={"kind": "persona_fact", "value": {"subject": "A", "predicate": "is", "object": "private"}},
    )
    ids["memory_item_id"] = memory.json()["id"]
    asset = await upload_asset(harness, a, placeholder_png("a"), mime="image/png", kind="image")
    ids["asset_id"] = asset["id"]
    ids["key_id"] = (await c.post("/v1/api-keys", json={"scopes": ["read"]})).json()["id"]
    ids["user_id"] = str(a.user_id)
    from ce_db.models.creators import Voice, VoiceVersion

    async with harness.services.db.transaction() as session:
        voice = Voice(org_id=a.org_id, creator_id=uuid.UUID(creator["id"]), name="A", kind="designed")
        session.add(voice)
        await session.flush()
        version = VoiceVersion(org_id=a.org_id, voice_id=voice.id, number=1)
        session.add(version)
        await session.flush()
        ids["voice_id"], ids["voice_version_id"] = str(voice.id), str(version.id)
    from ce_db.models.videos import Video, VideoVersion
    from ce_testing.seed import example_version_row

    async with harness.services.db.transaction() as session:
        video = Video(org_id=a.org_id, project_id=uuid.UUID(ids["project_id"]))
        session.add(video)
        await session.flush()
        row = example_version_row(a.org_id) | {"id": uuid.uuid4(), "video_id": video.id}
        session.add(VideoVersion(**row))
        ids["video_id"], ids["version_id"] = str(video.id), str(row["id"])
    from ce_db.models.assets import GenerationJob
    from ce_db.models.videos import Render

    async with harness.services.db.transaction() as session:
        job = GenerationJob(
            org_id=a.org_id,
            kind="generate",
            status="running",
            target_type="video_version",
            target_id=uuid.UUID(ids["version_id"]),
            video_version_id=uuid.UUID(ids["version_id"]),
        )
        render = Render(
            org_id=a.org_id,
            version_id=uuid.UUID(ids["version_id"]),
            preset_id="tiktok_1080x1920_30",
            aspect="9:16",
            provenance_mode="mock_dev",
        )
        from ce_db.models.videos import Caption

        caption = Caption(
            org_id=a.org_id,
            version_id=uuid.UUID(ids["version_id"]),
            language="en",
            style_id="clean_subtitle",
            format="srt",
        )
        session.add_all([job, render, caption])
        await session.flush()
        ids["job_id"], ids["render_id"], ids["caption_id"] = str(job.id), str(render.id), str(caption.id)
    from ce_db.models.videos import Take

    async with harness.services.db.transaction() as session:
        take = Take(
            org_id=a.org_id, version_id=uuid.UUID(ids["version_id"]), shot_key="sht_1", take_key="tk_1_1", take_index=1
        )
        session.add(take)
        await session.flush()
        ids["take_id"] = str(take.id)
    from ce_db.models.videos import EditProposal

    async with harness.services.db.transaction() as session:
        proposal = EditProposal(
            org_id=a.org_id, version_id=uuid.UUID(ids["version_id"]), instruction="private", status="proposed"
        )
        session.add(proposal)
        await session.flush()
        ids["edit_proposal_id"] = str(proposal.id)
    from ce_db.models.creators import CreatorTest, VoiceCandidate

    async with harness.services.db.transaction() as session:  # Phase 10 studio records
        test = CreatorTest(org_id=a.org_id, creator_version_id=uuid.UUID(ids["creator_version_id"]), scorecard={})
        candidate = VoiceCandidate(org_id=a.org_id, voice_id=uuid.UUID(ids["voice_id"]), engine="mock_voice")
        session.add_all([test, candidate])
        await session.flush()
        ids["creator_test_id"], ids["candidate_id"] = str(test.id), str(candidate.id)
    from ce_db.models.research import Consent

    async with harness.services.db.transaction() as session:
        consent = Consent(org_id=a.org_id, subject_name="A", scope="voice", statement_text="private")
        session.add(consent)
        await session.flush()
        ids["consent_id"] = str(consent.id)
        from ce_db.models.assets import Artifact

        artifact = Artifact(org_id=a.org_id, kind="audio", storage_key="sha256/aa/bb/a", mime="audio/wav",
                            bytes=1, sha256="a" * 64)  # fmt: skip
        session.add(artifact)
        await session.flush()
        ids["artifact_id"] = str(artifact.id)
    from ce_db.models.videos import Critique

    async with harness.services.db.transaction() as session:  # Phase 11 critique
        critique = Critique(
            org_id=a.org_id,
            version_id=uuid.UUID(ids["version_id"]),
            scores={},
            findings=[{"id": "f1", "proposed_ops": [{"op": "regenerate", "components": ["avatar_video"]}]}],
        )
        session.add(critique)
        await session.flush()
        ids["critique_id"], ids["finding_id"] = str(critique.id), "f1"
    from ce_db.models.research import BrandKit, Claim, ResearchSource, SpecTemplate
    from ce_db.models.videos import Export, Packaging

    async with harness.services.db.transaction() as session:  # Phase 12 records
        source = ResearchSource(
            org_id=a.org_id, project_id=uuid.UUID(ids["project_id"]), kind="note", trust="user_provided",
            status="ingested", note_text="private",
        )  # fmt: skip
        claim = Claim(
            org_id=a.org_id, video_id=uuid.UUID(ids["video_id"]), claim_key="clm_1",
            first_version_id=uuid.UUID(ids["version_id"]), text="private", verdict="unsupported", blocking=True,
        )  # fmt: skip
        template = SpecTemplate(
            org_id=a.org_id, kind="caption", name="private", body={"captions": {"style_id": "clean_subtitle"}}
        )
        kit = BrandKit(org_id=a.org_id, name="private")
        packaging = Packaging(
            org_id=a.org_id, version_id=uuid.UUID(ids["version_id"]), platform="tiktok", title="private",
            thumbnail_candidates=[{"artifact_id": ids["artifact_id"]}],
        )  # fmt: skip
        session.add_all([source, claim, template, kit, packaging])
        await session.flush()
        export = Export(
            org_id=a.org_id, render_id=uuid.UUID(ids["render_id"]), platform="tiktok",
            version_id=uuid.UUID(ids["version_id"]),
        )  # fmt: skip
        session.add(export)
        await session.flush()
        ids.update(
            source_id=str(source.id), claim_id=str(claim.id), spec_template_id=str(template.id),
            brand_kit_id=str(kit.id), packaging_id=str(packaging.id), export_id=str(export.id),
        )  # fmt: skip
    return ids


DNA = alex_creator_dna().model_dump(mode="json")

# (method, path template, body) — every route with an id in its path.
CASES: list[tuple[str, str, Any]] = [
    ("DELETE", "/v1/api-keys/{key_id}", None),
    ("PATCH", "/v1/members/{user_id}", {"role": "viewer"}),
    ("DELETE", "/v1/members/{user_id}", None),
    ("GET", "/v1/projects/{project_id}", None),
    ("PATCH", "/v1/projects/{project_id}", {"name": "hijacked"}),
    ("DELETE", "/v1/projects/{project_id}", None),
    ("GET", "/v1/projects/{project_id}/videos", None),
    ("POST", "/v1/projects/{project_id}/videos", {"input": "Make a video about my project."}),
    ("GET", "/v1/videos/{video_id}", None),
    ("GET", "/v1/videos/{video_id}/versions", None),
    ("GET", "/v1/versions/{version_id}", None),
    ("GET", "/v1/versions/{version_id}/previz", None),
    ("GET", "/v1/versions/{version_id}/intent", None),
    ("GET", "/v1/versions/{version_id}/storyboard", None),
    ("POST", "/v1/versions/{version_id}:replan", {"instruction": "hijack"}),
    ("POST", "/v1/versions/{version_id}:approve", {}),
    ("GET", "/v1/versions/{version_id}/memory-snapshots", None),
    ("GET", "/v1/versions/{version_id}/behavior", None),
    ("GET", "/v1/versions/{version_id}/coverage", None),
    ("GET", "/v1/takes/{take_id}/observations", None),
    ("GET", "/v1/creators/{creator_id}", None),
    ("POST", "/v1/creators/{creator_id}/versions", {"dna": DNA}),
    ("GET", "/v1/creator-versions/{creator_version_id}", None),
    ("PATCH", "/v1/creator-versions/{creator_version_id}", {"dna": DNA}),
    ("POST", "/v1/creator-versions/{creator_version_id}:approve", {"attest_adult_presentation": True}),
    (
        "POST",
        "/v1/creators/{creator_id}/appearances",
        {"name": "x", "dna": alex_appearance_dna().model_dump(mode="json")},
    ),
    ("GET", "/v1/appearances/{appearance_id}", None),
    ("POST", "/v1/appearances/{appearance_id}/versions", {}),
    ("GET", "/v1/appearance-versions/{appearance_version_id}", None),
    (
        "PATCH",
        "/v1/appearance-versions/{appearance_version_id}",
        {"dna": alex_appearance_dna().model_dump(mode="json")},
    ),
    ("POST", "/v1/appearance-versions/{appearance_version_id}:approve", None),
    ("POST", "/v1/creators/{creator_id}/wardrobes", {"name": "x", "spec": grey_hoodie().model_dump(mode="json")}),
    ("GET", "/v1/creators/{creator_id}/wardrobes", None),
    ("POST", "/v1/wardrobes/{wardrobe_id}/versions", {}),
    ("GET", "/v1/wardrobe-versions/{wardrobe_version_id}", None),
    ("PATCH", "/v1/wardrobe-versions/{wardrobe_version_id}", {"spec": grey_hoodie().model_dump(mode="json")}),
    ("POST", "/v1/wardrobe-versions/{wardrobe_version_id}:approve", None),
    ("GET", "/v1/voices/{voice_id}", None),
    ("GET", "/v1/voice-versions/{voice_version_id}", None),
    # Phase 10 studio routes
    ("POST", "/v1/appearance-versions/{appearance_version_id}/identity-pack:generate", {"candidates": 2}),
    ("POST", "/v1/appearance-versions/{appearance_version_id}/identity-pack:choose", {"asset_id": "{asset_id}"}),
    ("POST", "/v1/appearance-versions/{appearance_version_id}/identity-pack:review", {"approve": [], "reject": []}),
    ("GET", "/v1/appearance-versions/{appearance_version_id}/identity-pack", None),
    ("POST", "/v1/wardrobe-versions/{wardrobe_version_id}/references:generate", None),
    ("GET", "/v1/voices/{voice_id}/candidates", None),
    ("POST", "/v1/voices/{voice_id}/candidates/{candidate_id}:select", None),
    ("GET", "/v1/voice-versions/{voice_version_id}/detail", None),
    ("PATCH", "/v1/voice-versions/{voice_version_id}", {"description": "hijacked"}),
    ("POST", "/v1/voice-versions/{voice_version_id}:test", {"text": "hi"}),
    ("POST", "/v1/voice-versions/{voice_version_id}:approve", None),
    ("POST", "/v1/creator-versions/{creator_version_id}/tests", {}),
    ("GET", "/v1/creator-tests/{creator_test_id}", None),
    ("POST", "/v1/creator-tests/{creator_test_id}/ratings", {"human_rating": 1}),
    ("GET", "/v1/creators/{creator_id}/tests", None),
    ("GET", "/v1/creators/{creator_id}/baselines", None),
    ("GET", "/v1/creators/{creator_id}/consistency", None),
    ("POST", "/v1/world-versions/{world_version_id}/plates:generate", {}),
    ("GET", "/v1/worlds/{world_id}/continuity", None),
    ("GET", "/v1/consents/{consent_id}", None),
    ("GET", "/v1/versions/{version_id}/qc", None),
    ("POST", "/v1/versions/{version_id}:critique", None),
    ("GET", "/v1/versions/{version_id}/critiques", None),
    ("GET", "/v1/critiques/{critique_id}", None),
    ("POST", "/v1/critiques/{critique_id}/findings/{finding_id}:propose", None),
    ("POST", "/v1/versions/{version_id}/consistency:run", None),
    ("GET", "/v1/versions/{version_id}/consistency", None),
    ("GET", "/v1/artifacts/{artifact_id}/download", None),
    ("GET", "/v1/creators/{creator_id}/appearances", None),
    ("POST", "/v1/consents/{consent_id}:revoke", None),
    ("POST", "/v1/consents/{consent_id}:submit", {"consent_media_asset_id": "{asset_id}"}),
    ("GET", "/v1/worlds/{world_id}", None),
    ("POST", "/v1/worlds/{world_id}/versions", {}),
    ("GET", "/v1/world-versions/{world_version_id}", None),
    ("PATCH", "/v1/world-versions/{world_version_id}", {"patch": {"style_tags": ["x"]}}),
    (
        "POST",
        "/v1/world-versions/{world_version_id}/plates:choose",
        {
            "camera_position_key": "cam_desk_front",
            "time_of_day": "late_afternoon",
            "weather": "clear",
            "asset_id": "{asset_id}",
        },
    ),
    ("POST", "/v1/world-versions/{world_version_id}:approve", None),
    ("GET", "/v1/world-versions/{world_version_id}/diff", None),
    ("GET", "/v1/creators/{creator_id}/memory", None),
    (
        "POST",
        "/v1/creators/{creator_id}/memory",
        {"kind": "persona_fact", "value": {"subject": "B", "predicate": "is", "object": "x"}},
    ),
    ("PATCH", "/v1/memory-items/{memory_item_id}", {"action": "forget"}),
    ("DELETE", "/v1/memory-items/{memory_item_id}", None),
    ("GET", "/v1/memory-items/{memory_item_id}/history", None),
    ("GET", "/v1/creators/{creator_id}/usage", None),
    ("GET", "/v1/jobs/{job_id}", None),
    ("POST", "/v1/jobs/{job_id}:cancel", None),
    ("GET", "/v1/versions/{version_id}/manifest", None),
    ("GET", "/v1/versions/{version_id}/renders", None),
    ("POST", "/v1/versions/{version_id}/renders", {"preset_ids": ["tiktok_1080x1920_30"]}),
    ("GET", "/v1/renders/{render_id}", None),
    ("GET", "/v1/renders/{render_id}/download", None),
    ("GET", "/v1/renders/{render_id}/verify", None),
    ("GET", "/v1/versions/{version_id}/captions", None),
    ("GET", "/v1/captions/{caption_id}/download", None),
    ("POST", "/v1/versions/{version_id}/edits", {"instruction": "hijack"}),
    ("GET", "/v1/versions/{version_id}/edits", None),
    ("GET", "/v1/edits/{edit_proposal_id}", None),
    ("POST", "/v1/edits/{edit_proposal_id}:apply", {}),
    ("POST", "/v1/edits/{edit_proposal_id}:reject", None),
    ("PUT", "/v1/versions/{version_id}/locks", {"locks": []}),
    ("POST", "/v1/versions/{version_id}/scenes/scn_hook:regenerate", {"components": ["broll"]}),
    ("POST", "/v1/versions/{version_id}/shots/sht_1:regenerate", {"components": ["avatar_video"]}),
    ("POST", "/v1/versions/{version_id}:reroute", {"node_keys": ["avatar.render:sht_1:c1:t1"]}),
    ("POST", "/v1/versions/{version_id}/shots/sht_1/takes/tk_1:select", None),
    ("GET", "/v1/versions/{version_id}/takes", None),
    ("POST", "/v1/versions/{version_id}:resume", None),
    ("POST", "/v1/versions/{version_id}:branch", {"name": "hijack"}),
    ("POST", "/v1/versions/{version_id}:restore", None),
    ("POST", "/v1/videos/{video_id}:duplicate", {"version_id": "{version_id}"}),
    ("POST", "/v1/versions/{version_id}:variants", None),
    ("POST", "/v1/versions/{version_id}:remix", {"transform": "shorten"}),
    ("GET", "/v1/videos/{video_id}/compare?a={version_id}&b={version_id}", None),
    ("GET", "/v1/assets/{asset_id}", None),
    ("GET", "/v1/assets/{asset_id}/screen-analysis", None),
    ("POST", "/v1/assets/{asset_id}:complete", {}),
    ("DELETE", "/v1/assets/{asset_id}", None),
    # Phase 12: research, the claim ledger, templates, brand kits, translation, packaging, exports
    ("POST", "/v1/projects/{project_id}/sources", {"kind": "note", "text": "hijack"}),
    ("GET", "/v1/projects/{project_id}/sources", None),
    ("GET", "/v1/sources/{source_id}", None),
    ("POST", "/v1/sources/{source_id}:reingest", None),
    ("DELETE", "/v1/sources/{source_id}", None),
    ("GET", "/v1/versions/{version_id}/claims", None),
    ("POST", "/v1/claims/{claim_id}:override", {"reason": "hijack"}),
    ("GET", "/v1/spec-templates/{spec_template_id}", None),
    ("PATCH", "/v1/spec-templates/{spec_template_id}", {"name": "hijacked"}),
    ("DELETE", "/v1/spec-templates/{spec_template_id}", None),
    ("POST", "/v1/spec-templates/{spec_template_id}:apply", {"version_id": "{version_id}", "preview": True}),
    ("GET", "/v1/brand-kits/{brand_kit_id}", None),
    ("PATCH", "/v1/brand-kits/{brand_kit_id}", {"name": "hijacked"}),
    ("DELETE", "/v1/brand-kits/{brand_kit_id}", None),
    ("POST", "/v1/versions/{version_id}/captions:translate", {"language": "de"}),
    ("POST", "/v1/captions/{caption_id}:approve", {}),
    ("POST", "/v1/captions/{caption_id}:reject", {"note": "hijack"}),
    ("POST", "/v1/versions/{version_id}:package", {"platforms": ["tiktok"]}),
    ("GET", "/v1/versions/{version_id}/packaging", None),
    ("PATCH", "/v1/packaging/{packaging_id}", {"title": "hijacked"}),
    ("POST", "/v1/packaging/{packaging_id}:approve", None),
    ("GET", "/v1/packaging/{packaging_id}/thumbnails/{artifact_id}", None),
    ("POST", "/v1/renders/{render_id}/exports", {"platform": "tiktok", "disclosure_checklist": {}}),
    ("GET", "/v1/versions/{version_id}/exports", None),
    ("GET", "/v1/exports/{export_id}", None),
]

# Routes with a path parameter that is not an org resource id (the local-storage route is
# outside the schema and checks its own signatures, ADR 0030).
NOT_RESOURCE_ROUTES = {("POST", "/v1/invitations/{token}:accept")}
# Platform (global) tables carry no org_id (§29): the model registry describes installed software,
# every org reads it, and its admin routes are platform-admin only
# (apps/api/tests/test_models_api.py checks both).
PLATFORM_ROUTES = {
    ("GET", "/v1/models/{model_id}"),
    ("POST", "/v1/admin/models/{model_id}:promote"),
    ("POST", "/v1/admin/models/{model_id}:disable"),
    ("POST", "/v1/admin/models/{model_id}:calibrate"),
    ("POST", "/v1/admin/models/{model_id}/validations"),
    ("POST", "/v1/admin/models/{model_id}/calibrations"),
    ("POST", "/v1/admin/models/{model_id}/benchmarks"),
    # the benchmark runner (Phase 11): platform evidence, platform admins only (apps/api/tests/test_qc_api.py)
    ("POST", "/v1/admin/models/{model_id}:benchmark"),
    ("GET", "/v1/admin/benchmarks/{benchmark_id}"),
    ("GET", "/v1/admin/benchmarks/{benchmark_id}/pairs"),
    ("POST", "/v1/admin/benchmarks/{benchmark_id}/pairs/{pair_id}:rate"),
    # the GPU fleet (Phase 9): platform resources, platform admins only (tests/test_gpu_api.py)
    ("PATCH", "/v1/admin/gpu/providers/{provider_id}"),
    ("POST", "/v1/admin/gpu/workers/{worker_id}:stop"),
}


def test_every_id_route_has_a_cross_tenant_case(harness: ApiHarness) -> None:
    routes = {
        (method.upper(), path)
        for path, operations in harness.app.openapi()["paths"].items()
        if "{" in path
        for method in operations
    }

    def route(template: str) -> str:  # literal keys stand in for {scene_key}, {shot_key}, {take_key}
        path = template.split("?", 1)[0]
        for literal, name in (("scn_hook", "scene_key"), ("sht_1", "shot_key"), ("tk_1", "take_key")):
            path = path.replace(f"/{literal}:", f"/{{{name}}}:").replace(f"/{literal}/", f"/{{{name}}}/")
        return path

    covered = {(m, route(p)) for m, p, _ in CASES} | NOT_RESOURCE_ROUTES | PLATFORM_ROUTES
    assert routes - covered == set(), "add a cross-tenant case for these routes"
    assert {(m, route(p)) for m, p, _ in CASES} - routes == set(), "stale cases"


def fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {k: fill(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, ids) for v in value]
    return value


async def test_org_b_cannot_reach_org_a(harness: ApiHarness) -> None:
    a = await harness.new_tenant("owner", name="Org A")
    b = await harness.new_tenant("owner", name="Org B")
    ids = await org_a_resources(harness, a)
    readable = [template for method, template, _ in CASES if method == "GET"]

    async def snapshot() -> dict[str, Any]:
        out = {}
        for template in readable:
            body = (await a.client.get(template.format(**ids))).json()
            if isinstance(body, dict):
                for volatile in ("download_url", "url", "expires_at"):  # presigned: expiry moves with the clock
                    body.pop(volatile, None)
            out[template] = body
        return out

    before = await snapshot()
    failures = []
    for method, template, body in CASES:
        path = template.format(**ids)
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        response = await b.client.request(
            method, path, json=fill(body, ids) if body is not None else None, headers=headers
        )
        if response.status_code != 404:
            failures.append((method, template, response.status_code, response.text[:200]))
    assert failures == []
    await harness.services.drain()
    after = await snapshot()
    assert after == before, "org A's data changed"
    # lists never show another org's rows
    for path in (
        "/v1/projects",
        "/v1/creators",
        "/v1/worlds",
        "/v1/assets",
        "/v1/voices",
        "/v1/members",
        "/v1/api-keys",
        "/v1/jobs",
    ):
        body = (await b.client.get(path)).json()
        items = body["items"] if isinstance(body, dict) else body
        leaked = {str(v) for item in items for v in item.values()} & set(ids.values()) - {str(b.user_id)}
        assert leaked == set(), (path, leaked)
