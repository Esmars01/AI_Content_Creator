"""Behavior, coverage and take observations (§30): the documents a build wrote (CBS, compiled
behavior, coverage report) and the observation rows, read back through the API."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from ce_api.testing import ApiHarness, ApiTenant
from ce_behavior.compiler import realized_methods
from ce_behavior.coverage import coverage_report
from ce_core.behavior.observed import ItemObservation
from ce_core.canonical import canonical_json
from ce_core.enums import ObservationVerdict
from ce_db.models.assets import Artifact
from ce_db.models.behavior import BehaviorObservation, QCReport
from ce_db.models.videos import BuildManifestEntry, Take, Video, VideoVersion
from ce_storage import content_key
from ce_testing.behavior import GLOBAL_ONLY, bundle, catalog_with, version_behavior
from ce_testing.seed import example_version_row

pytestmark = [pytest.mark.infra, pytest.mark.behavior]


async def _version(harness: ApiHarness, tenant: ApiTenant) -> uuid.UUID:
    project = (await tenant.client.post("/v1/projects", json={"name": "P"})).json()
    async with harness.services.db.transaction() as session:
        video = Video(org_id=tenant.org_id, project_id=uuid.UUID(project["id"]))
        session.add(video)
        await session.flush()
        row = example_version_row(tenant.org_id) | {"id": uuid.uuid4(), "video_id": video.id, "state": "generating"}
        session.add(VideoVersion(**row))
        return row["id"]  # type: ignore[no-any-return]


async def _store(harness: ApiHarness, tenant: ApiTenant, version_id: uuid.UUID, node_key: str, data: Any) -> None:
    """Writes a node output document the way the runtime does (content-addressed + manifest row)."""
    raw = canonical_json({"v": 1, "node_kind": node_key.split(":", 1)[0], "data": data, "refs": {}}).encode()
    sha = hashlib.sha256(raw).hexdigest()
    bucket = harness.services.settings.s3_bucket_artifacts
    await harness.services.storage.ensure_bucket(bucket)
    await harness.services.storage.put(bucket, content_key(sha), raw, content_type="application/json")
    async with harness.services.db.transaction() as session:
        artifact = Artifact(
            org_id=tenant.org_id,
            kind="other",
            storage_key=content_key(sha),
            mime="application/json",
            bytes=len(raw),
            sha256=sha,
        )
        session.add(artifact)
        await session.flush()
        session.add(
            BuildManifestEntry(org_id=tenant.org_id, version_id=version_id, node_key=node_key, artifact_id=artifact.id)
        )


async def _built(harness: ApiHarness, tenant: ApiTenant, *, observed: bool) -> tuple[uuid.UUID, Any]:
    version_id = await _version(harness, tenant)
    vb = version_behavior(catalog_with(GLOBAL_ONLY))
    cbs = vb.cbs["scn_hook"]
    await _store(harness, tenant, version_id, "behavior.resolve:scn_hook", {"content": cbs.model_dump(mode="json")})
    for compiled in vb.compiled:
        kind = "behavior.compile_visual" if ":c" in compiled.target_key else "behavior.compile_voice"
        doc = {"compiled": compiled.model_dump(mode="json", by_alias=True), "downgrades": []}
        await _store(harness, tenant, version_id, f"{kind}:{compiled.target_key}", doc)
    if observed:
        observations = {
            (c.item_ref, c.dimension): ItemObservation(
                item_ref=c.item_ref,
                character_key=c.character_key,
                dimension=c.dimension,
                verdict=ObservationVerdict.NOT_MEASURABLE,
                method="x",
            )
            for c in cbs.requested_controls
        }
        report = coverage_report(
            vb.cbs, realized_methods(vb.compiled), bundle().vocab, stage="observed", observations=observations
        )
        await _store(
            harness,
            tenant,
            version_id,
            "behavior.coverage:video",
            {
                "report": report.model_dump(mode="json"),
                "summary": report.summary_counts(),
                "decision": {"action": "pass", "ladder": "phase 11"},
                "downgrades": [],
            },
        )
    return version_id, vb


async def test_coverage_while_building_is_the_compiled_report(harness: ApiHarness, owner: ApiTenant) -> None:
    version_id, vb = await _built(harness, owner, observed=False)
    body = (await owner.client.get(f"/v1/versions/{version_id}/coverage")).json()
    assert body["stage"] == "compiled" and body["decision"] is None
    assert len(body["entries"]) == 25 and all(e["observed"] is None for e in body["entries"])
    assert body["summary"]["level:HONORED"] == sum(1 for e in vb.report.entries if e.compiled.level == "HONORED")


async def test_observed_coverage_and_scene_behavior(harness: ApiHarness, owner: ApiTenant) -> None:
    version_id, vb = await _built(harness, owner, observed=True)
    coverage = (await owner.client.get(f"/v1/versions/{version_id}/coverage")).json()
    assert coverage["stage"] == "observed" and coverage["decision"]["ladder"] == "phase 11"
    assert {e["outcome"] for e in coverage["entries"]} == {"NOT_MEASURABLE"}
    scene = (await owner.client.get(f"/v1/versions/{version_id}/behavior", params={"scene_key": "scn_hook"})).json()
    assert scene["cbs"]["envelope"]["content_digest"] == vb.cbs["scn_hook"].digest()
    assert scene["cbs"]["envelope"]["scope"]["scene_key"] == "scn_hook"
    assert {c["target_key"] for c in scene["compiled"]} == {"seg_1", "seg_2", "sht_1:c1"}
    assert len(scene["coverage"]) == 25 and scene["stage"] == "observed"
    default = (await owner.client.get(f"/v1/versions/{version_id}/behavior")).json()
    assert default["scene_key"] == "scn_hook"  # the first scene when none is named


async def test_missing_behavior_and_unknown_scenes_are_404(harness: ApiHarness, owner: ApiTenant) -> None:
    version_id = await _version(harness, owner)
    missing = await owner.client.get(f"/v1/versions/{version_id}/coverage")
    assert missing.status_code == 404 and missing.headers["content-type"].startswith("application/problem+json")
    assert (await owner.client.get(f"/v1/versions/{version_id}/behavior")).status_code == 404
    built, _ = await _built(harness, owner, observed=False)
    unknown = await owner.client.get(f"/v1/versions/{built}/behavior", params={"scene_key": "scn_nope"})
    assert unknown.status_code == 404


async def test_take_observations(harness: ApiHarness, owner: ApiTenant) -> None:
    version_id = await _version(harness, owner)
    async with harness.services.db.transaction() as session:
        take = Take(org_id=owner.org_id, version_id=version_id, shot_key="sht_1", take_key="tk_1_1", take_index=1)
        session.add(take)
        await session.flush()
        report = QCReport(
            org_id=owner.org_id,
            version_id=version_id,
            target_type="take",
            target_id=take.id,
            checks={"score": 0.9, "metric_score": 1.0, "behavior": {"score": 0.6, "decision": {"action": "warn"}}},
            verdict="warn",
            thresholds_digest="sha256:" + "0" * 64,
        )
        session.add(report)
        await session.flush()
        take.qc_report_id = report.id
        session.add(
            BehaviorObservation(
                org_id=owner.org_id,
                version_id=version_id,
                take_id=take.id,
                level_scope="take",
                item_ref="/scenes[scn_hook]/acting/events[ev_1]",
                character_key="char_alex",
                dimension="gaze",
                requested={"value": "look_away:down_left@seg_2.w2+700ms"},
                level="HONORED",
                method="native_parametric",
                verdict="CONFIRMED",
                outcome="HONORED_CONFIRMED",
                measures={"look_away_ms": 600.0},
                confidence=0.85,
                adapter_id="mock_avatar_segment",
                translator_version="0.1.0",
                model_revision="1",
                language="en-US",
            )
        )
        take_id = take.id
    body = (await owner.client.get(f"/v1/takes/{take_id}/observations")).json()
    assert body["take_key"] == "tk_1_1" and body["qc"] == {
        "verdict": "warn",
        "score": 0.9,
        "metric_score": 1.0,
        "behavior_score": 0.6,
        "decision": {"action": "warn"},
    }
    (observation,) = body["observations"]
    assert observation["outcome"] == "HONORED_CONFIRMED" and observation["adapter_id"] == "mock_avatar_segment"
    assert (await owner.client.get(f"/v1/takes/{uuid.uuid4()}/observations")).status_code == 404
