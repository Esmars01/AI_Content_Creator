"""I6 — Overrides never mutate identity (§19.5, §12.7, §18.4).

Scene overrides (element states, hidden, added and moved elements, lighting, acoustics), a move to
another world, an outfit or voice switch and memory feedback change the *video version* only:
World DNA, Creator DNA, wardrobe and voice versions are never written, and Creator Memory only
gains a `proposed` item the user confirms separately. Promotion into a world or creator version is
an explicit versioning action (World Studio, Phase 10).

The unit test runs the proposal path in memory; the infrastructure test applies the edit through
the API and compares every identity row before and after.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant, build_test_services
from ce_db.models.creators import AppearanceVersion, CreatorVersion, VoiceVersion, WardrobeVersion
from ce_db.models.memory import CreatorMemoryItem
from ce_db.models.videos import Project, VideoVersion
from ce_db.models.worlds import WorldVersion
from ce_exec.context import build_services as build_exec_services
from ce_exec.editing import propose_edit, run_apply_job
from ce_exec.submit import submit_spec
from ce_testing.build import example_build_refs
from ce_testing.database import TestDatabase
from ce_testing.edits import propose_ops
from ce_testing.fixtures import ALEX, two_scene_spec_dict

pytestmark = [pytest.mark.invariant]

HOOK = {"scene_keys": ["scn_hook"]}
REVEAL = {"scene_keys": ["scn_reveal"]}
OPERATIONS: list[dict[str, Any]] = [
    {
        "op": "set_world_override",
        "scope": HOOK,
        "element_states": {"el_neon": "off"},
        "hide": ["el_mug"],
        "acoustics": {"ambient_additions": ["rain_on_window"]},
    },
    {"op": "set_world_binding", "scope": REVEAL, "world_version_id": str(ALEX.OFFICE_WORLD_VERSION_ID)},
    {"op": "set_wardrobe", "scope": REVEAL, "wardrobe_version_id": str(ALEX.WARDROBE_NAVY_VERSION_ID)},
    {"op": "memory_feedback", "kind": "social_behavior.warmth", "value": {"level": 0.3}, "text": "cooler on camera"},
]


def snapshot(refs: Any) -> str:
    """Every identity record the build reads (World DNA and plates, Creator DNA, wardrobes, voices)."""
    parts = {name: {str(k): dataclasses.asdict(v) for k, v in getattr(refs, name).items()} for name in IDENTITY}
    return json.dumps(parts, sort_keys=True, default=str)


IDENTITY = ("worlds", "creators", "wardrobes", "voices", "appearances")


def test_overrides_change_the_version_and_never_the_identity_records() -> None:
    refs = example_build_refs()
    before = snapshot(refs)
    data = two_scene_spec_dict()
    proposal = propose_ops(data, OPERATIONS, refs=refs)
    assert proposal.status == "proposed", proposal.issues
    assert snapshot(refs) == before
    assert data == two_scene_spec_dict()  # the parent document is untouched too
    hook = proposal.spec.scene("scn_hook")
    assert hook.world.overrides.element_states["el_neon"] == "off" and "el_mug" in hook.world.overrides.hide_elements
    assert str(proposal.spec.scene("scn_reveal").world.world_version_id) == str(ALEX.OFFICE_WORLD_VERSION_ID)
    # the plate re-renders for the override; nothing writes a world version
    runs = {*proposal.impact["regenerate"], *proposal.impact["cascade"]}
    assert {"world.plate:scn_hook", "world.plate:scn_reveal"} <= runs
    assert proposal.side_effects == [
        {
            "kind": "memory_feedback",
            "character_key": "char_alex",
            "memory_kind": "social_behavior.warmth",
            "value": {"level": 0.3},
            "text": "cooler on camera",
        }
    ]


# ---------------------------------------------------------------------- through the API


@pytest_asyncio.fixture
async def harness(seeded_db: TestDatabase, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    services = build_test_services(
        seeded_db.url, storage="local_fs", storage_root=tmp_path / "storage", extra_env={"MOCK_GPU": "true"}
    )
    for bucket in (services.settings.s3_bucket_assets, services.settings.s3_bucket_artifacts):
        await services.storage.ensure_bucket(bucket)

    async def record_start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        return None

    services.workflows.start = record_start  # type: ignore[method-assign]
    h = ApiHarness(services)
    h.exec = build_exec_services(services.effective, pool_size=2)  # type: ignore[attr-defined]
    yield h
    await h.exec.close()  # type: ignore[attr-defined]
    await h.aclose()


async def identity_rows(harness: ApiHarness) -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with harness.services.db.session() as session:
        tables: list[tuple[str, Any, tuple[str, ...]]] = [
            ("worlds", WorldVersion, ("dna", "plates", "status", "updated_at")),
            ("creators", CreatorVersion, ("dna", "status", "updated_at")),
            ("appearances", AppearanceVersion, ("dna", "status", "updated_at")),
            ("wardrobes", WardrobeVersion, ("spec", "status", "updated_at")),
            ("voices", VoiceVersion, ("description", "status", "updated_at")),
        ]
        for name, model, columns in tables:
            rows = (await session.execute(sa.select(model).where(model.org_id == ALEX.ORG_ID))).scalars()
            out[name] = {str(r.id): {c: getattr(r, c) for c in columns} for r in rows}
        memory = (
            await session.execute(sa.select(CreatorMemoryItem).where(CreatorMemoryItem.org_id == ALEX.ORG_ID))
        ).scalars()
        out["memory"] = {str(m.id): {"value": m.value, "status": m.status, "source": m.source} for m in memory}
    return out


@pytest.mark.infra
async def test_applying_overrides_through_the_api_writes_no_identity_row(harness: ApiHarness) -> None:
    editor: ApiTenant = await harness.add_member(ALEX.ORG_ID, "editor")
    data = two_scene_spec_dict()
    data["generation"]["seed_namespace"] = str(uuid.uuid4())
    async with harness.services.db.transaction() as session:
        project = Project(org_id=ALEX.ORG_ID, name="I6")
        session.add(project)
        await session.flush()
        project_id = project.id
    sub = await submit_spec(
        harness.exec,  # type: ignore[attr-defined]
        org_id=ALEX.ORG_ID,
        project_id=project_id,
        spec_data=data,
    )
    before = await identity_rows(harness)
    response = await editor.client.post(
        f"/v1/versions/{sub.version_id}/edits",
        json={"operations": OPERATIONS},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202, response.text
    ids = response.json()
    await harness.services.drain()
    proposed = await propose_edit(harness.exec, ALEX.ORG_ID, UUID(ids["job_id"]))  # type: ignore[attr-defined]
    assert proposed["status"] == "proposed", proposed
    applied = await editor.client.post(
        f"/v1/edits/{ids['edit_proposal_id']}:apply", headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert applied.status_code == 202, applied.text
    await harness.services.drain()
    result = await run_apply_job(harness.exec, ALEX.ORG_ID, UUID(applied.json()["job_id"]))  # type: ignore[attr-defined]
    assert result["status"] == "succeeded", result

    after = await identity_rows(harness)
    for name in ("worlds", "creators", "appearances", "wardrobes", "voices"):
        assert after[name] == before[name], f"{name} changed"
    for item_id, item in before["memory"].items():
        assert after["memory"][item_id] == item  # existing memory is untouched
    (new_id,) = set(after["memory"]) - set(before["memory"])
    new_item = after["memory"][new_id]
    assert new_item["status"] == "proposed" and new_item["value"] == {"level": 0.3}
    assert new_item["source"]["edit_proposal_id"] == ids["edit_proposal_id"]
    async with harness.services.db.session() as session:
        child = await session.get_one(VideoVersion, UUID(result["version_id"]))
    hook = next(s for s in child.spec["scenes"] if s["key"] == "scn_hook")
    assert hook["world"]["overrides"]["element_states"]["el_neon"] == "off"


@pytest.mark.infra
async def test_world_studio_promotion_is_an_explicit_new_draft(harness: ApiHarness) -> None:
    """I6, Phase 10: making a scene override permanent is `POST /v1/worlds/{id}/versions` with a
    patch — a new draft; the approved version, the world's current version and every video's
    binding stay as they were until the draft is approved and a video is explicitly moved."""
    editor: ApiTenant = await harness.add_member(ALEX.ORG_ID, "editor")
    before = await identity_rows(harness)
    source = before["worlds"][str(ALEX.WORLD_VERSION_ID)]
    async with harness.services.db.session() as session:
        bound = {
            str(v.id): [s.get("world") for s in v.spec.get("scenes", [])]
            for v in (
                await session.execute(sa.select(VideoVersion).where(VideoVersion.org_id == ALEX.ORG_ID))
            ).scalars()
        }
    elements = [dict(e) for e in source["dna"]["elements"]]
    stateful = next(e for e in elements if e.get("states"))
    other = next(s for s in stateful["states"] if s != stateful.get("default_state"))
    stateful["default_state"] = other  # the scene override "made permanent"
    promoted = await editor.client.post(
        f"/v1/worlds/{ALEX.WORLD_ID}/versions",
        json={"from_version_id": str(ALEX.WORLD_VERSION_ID), "patch": {"elements": elements}},
    )
    assert promoted.status_code == 201, promoted.text
    draft = promoted.json()
    assert draft["status"] == "draft" and draft["parent_version_id"] == str(ALEX.WORLD_VERSION_ID)
    assert next(e for e in draft["dna"]["elements"] if e["key"] == stateful["key"])["default_state"] == other
    after = await identity_rows(harness)
    assert after["worlds"][str(ALEX.WORLD_VERSION_ID)] == source  # the approved version is untouched
    for name in ("creators", "appearances", "wardrobes", "voices"):
        assert after[name] == before[name]
    world = (await editor.client.get(f"/v1/worlds/{ALEX.WORLD_ID}")).json()
    assert world["current_version_id"] == str(ALEX.WORLD_VERSION_ID)  # until the draft is approved
    async with harness.services.db.session() as session:
        for version_id, bindings in bound.items():
            row = await session.get_one(VideoVersion, UUID(version_id))
            assert [s.get("world") for s in row.spec.get("scenes", [])] == bindings  # no video moved
