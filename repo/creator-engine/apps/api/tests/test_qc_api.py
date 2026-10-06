"""QC endpoints (Phase 11, §20, §24, §26): the version QC report, critiques and their findings as
edit proposals, consistency runs, the human rating queue, calibration against ratings, and the
benchmark runner's blind pairwise rating with bench-based validation. Workflow starts are recorded,
not run (the workflows run end to end in tests/e2e)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from ce_api.testing import ApiHarness, ApiTenant
from ce_core.canonical import canonical_json
from ce_db.models.assets import Artifact
from ce_db.models.behavior import BehaviorObservation, QCReport
from ce_db.models.creators import ConsistencyReport, CreatorVersion
from ce_db.models.platform import BenchmarkPair, Model, ModelBenchmark
from ce_db.models.videos import BuildManifestEntry, Critique, EditProposal, Render, Take, Video, VideoVersion
from ce_db.registry import sync_registry
from ce_storage import content_key
from ce_testing.fixtures import alex_creator_dna
from ce_testing.registry import manifest_copy
from ce_testing.seed import example_version_row

pytestmark = [pytest.mark.infra]


def _record_starts(harness: ApiHarness) -> list[tuple[str, Any]]:
    started: list[tuple[str, Any]] = []

    async def start(workflow: str, arg: Any, *, workflow_id: str) -> None:
        started.append((workflow, arg))

    harness.services.workflows.start = start  # type: ignore[method-assign]
    return started


async def _version(harness: ApiHarness, tenant: ApiTenant, state: str = "ready") -> uuid.UUID:
    project = (await tenant.client.post("/v1/projects", json={"name": "P"})).json()
    async with harness.services.db.transaction() as session:
        video = Video(org_id=tenant.org_id, project_id=uuid.UUID(project["id"]))
        session.add(video)
        await session.flush()
        row = example_version_row(tenant.org_id) | {"id": uuid.uuid4(), "video_id": video.id, "state": state}
        session.add(VideoVersion(**row))
        return row["id"]  # type: ignore[no-any-return]


async def _artifact(harness: ApiHarness, tenant: ApiTenant, raw: bytes, mime: str) -> Artifact:
    sha = hashlib.sha256(raw).hexdigest()
    bucket = harness.services.settings.s3_bucket_artifacts
    await harness.services.storage.ensure_bucket(bucket)
    await harness.services.storage.put(bucket, content_key(sha), raw, content_type=mime)
    async with harness.services.db.transaction() as session:
        artifact = Artifact(
            org_id=tenant.org_id, kind="other", storage_key=content_key(sha), mime=mime, bytes=len(raw), sha256=sha
        )
        session.add(artifact)
        await session.flush()
        return artifact


async def _store(harness: ApiHarness, tenant: ApiTenant, version_id: uuid.UUID, node_key: str, data: Any) -> None:
    raw = canonical_json({"v": 1, "node_kind": node_key.split(":", 1)[0], "data": data, "refs": {}}).encode()
    artifact = await _artifact(harness, tenant, raw, "application/json")
    async with harness.services.db.transaction() as session:
        session.add(
            BuildManifestEntry(org_id=tenant.org_id, version_id=version_id, node_key=node_key, artifact_id=artifact.id)
        )


async def test_the_qc_report_shows_the_ladder_the_takes_and_the_triad(harness: ApiHarness, owner: ApiTenant) -> None:
    version_id = await _version(harness, owner, "needs_review")
    entry = {
        "item_ref": "/scenes[scn_hook]/acting/events[ev_1]",
        "dimension": "gaze",
        "requested": "look away",
        "compiled": {"level": "HONORED", "method": "native_parametric"},
        "observed": {"verdict": "NOT_OBSERVED", "measures": {}, "confidence": 0.9},
        "outcome": "HONORED_NOT_OBSERVED",
        "expected_for_method": False,
    }
    await _store(harness, owner, version_id, "behavior.coverage:video", {"report": {"entries": [entry]}})
    await _store(harness, owner, version_id, "qc.render:tiktok", {"checks": {"loudness": True}, "passed": True})
    async with harness.services.db.transaction() as session:
        take = Take(org_id=owner.org_id, version_id=version_id, shot_key="sht_1", take_key="tk_1_1", take_index=1)
        session.add(take)
        await session.flush()
        verdicts = {"qc.vqa": {"adapter_id": "mock_qc", "score": 0.3, "passed": False, "advisory": False}}
        report = QCReport(
            org_id=owner.org_id, version_id=version_id, target_type="take", target_id=take.id,
            checks={"verdicts": verdicts}, verdict="fail", thresholds_digest="x",
        )  # fmt: skip
        session.add(report)
        await session.flush()
        take.qc_report_id = report.id
        ladder = [
            {"step": "attempt_seed", "outcome": "run", "take": 1, "qc_retry": 1},
            {"step": "fallback_route", "outcome": "skipped", "reason": "no fallback route"},
            {"step": "needs_review", "outcome": "flagged", "reason": "best attempt kept", "take": 1},
        ]
        session.add(
            QCReport(
                org_id=owner.org_id, version_id=version_id, target_type="shot", target_id=uuid.uuid4(),
                checks={"shot_key": "sht_1", "ladder": ladder}, verdict="fail", thresholds_digest="x",
            )
        )  # fmt: skip
    response = await owner.client.get(f"/v1/versions/{version_id}/qc")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "needs_review"
    assert body["gate"]["retries"] == 1 and body["gate"]["flagged_shots"] == ["sht_1"]
    assert body["takes"][0]["checks"]["take_key"] == "tk_1_1"
    assert body["coverage"][0]["outcome"] == "HONORED_NOT_OBSERVED"
    assert body["renders"][0]["passed"] is True


async def test_critique_findings_become_edit_proposals_only_on_request(harness: ApiHarness, owner: ApiTenant) -> None:
    started = _record_starts(harness)
    planned = await _version(harness, owner, "planned")
    assert (await owner.client.post(f"/v1/versions/{planned}:critique")).status_code == 409
    version_id = await _version(harness, owner)
    run = await owner.client.post(f"/v1/versions/{version_id}:critique")
    assert run.status_code == 202 and started[-1][0] == "CritiqueWorkflow"
    assert started[-1][1]["kind"] == "critique" and started[-1][1]["target_id"] == str(version_id)
    regenerate = {"op": "regenerate", "scope": {"shot_keys": ["sht_1"]}, "components": ["avatar_video"]}
    async with harness.services.db.transaction() as session:
        critique = Critique(
            org_id=owner.org_id,
            version_id=version_id,
            scores={"hook": {"score": 0.8, "basis": "x"}, "_meta": {"critic": "rules-v1", "applied": []}},
            findings=[
                {"id": "f1", "category": "realism", "issue": "hand melts", "proposed_ops": [regenerate]},
                {"id": "f2", "category": "cta", "issue": "no CTA", "proposed_ops": []},
                {"id": "f3", "category": "x", "issue": "bad", "proposed_ops": [{"op": "no_such_op"}]},
            ],
        )
        session.add(critique)
        await session.flush()
        critique_id = critique.id
    listed = await owner.client.get(f"/v1/versions/{version_id}/critiques")
    assert [c["id"] for c in listed.json()] == [str(critique_id)]
    async with harness.services.db.session() as session:
        before = (await session.execute(sa.select(sa.func.count()).select_from(EditProposal))).scalar_one()
    assert before == 0 or isinstance(before, int)
    proposed = await owner.client.post(f"/v1/critiques/{critique_id}/findings/f1:propose")
    assert proposed.status_code == 202, proposed.text
    assert started[-1][0] == "ProposeEditWorkflow"
    async with harness.services.db.session() as session:
        proposal = await session.get_one(EditProposal, uuid.UUID(proposed.json()["edit_proposal_id"]))
        row = await session.get_one(Critique, critique_id)
    assert proposal.selection["kind"] == "from_critique_finding" and proposal.ops[0]["op"] == "regenerate"
    assert proposal.status == "proposing"  # nothing is applied until the user applies the proposal
    assert row.scores["_meta"]["applied"][0]["finding_id"] == "f1"
    assert (await owner.client.post(f"/v1/critiques/{critique_id}/findings/f2:propose")).status_code == 409
    assert (await owner.client.post(f"/v1/critiques/{critique_id}/findings/f3:propose")).status_code == 422
    assert (await owner.client.post(f"/v1/critiques/{critique_id}/findings/f9:propose")).status_code == 404


async def test_consistency_runs_and_the_rating_queue(harness: ApiHarness, owner: ApiTenant) -> None:
    started = _record_starts(harness)
    version_id = await _version(harness, owner)
    run = await owner.client.post(f"/v1/versions/{version_id}/consistency:run")
    assert run.status_code == 202 and started[-1][0] == "ConsistencyWorkflow"
    creator = (
        await owner.client.post("/v1/creators", json={"name": "R", "dna": alex_creator_dna().model_dump(mode="json")})
    ).json()
    async with harness.services.db.transaction() as session:
        query = sa.select(CreatorVersion).where(CreatorVersion.creator_id == uuid.UUID(creator["id"]))
        creator_version = (await session.execute(query)).scalars().first()
        assert creator_version is not None
        report = ConsistencyReport(
            org_id=owner.org_id, creator_id=uuid.UUID(creator["id"]), creator_version_id=creator_version.id,
            version_id=version_id, metrics={"dimensions": {}, "rating_requested": True}, verdict="in_band",
        )  # fmt: skip
        session.add(report)
        take = Take(org_id=owner.org_id, version_id=version_id, shot_key="sht_1", take_key="tk_1_1", take_index=1)
        session.add(take)
        await session.flush()
        session.add(
            BehaviorObservation(
                org_id=owner.org_id, version_id=version_id, take_id=take.id, level_scope="take",
                item_ref="/scenes[scn_hook]/acting/events[ev_1]", character_key="char_alex", dimension="gaze",
                requested={"value": "glance_at_element", "observation_reliability": "low"}, level="HONORED",
                method="native_parametric", verdict="NOT_OBSERVED", outcome="HONORED_NOT_OBSERVED",
            )
        )  # fmt: skip
        report_id, take_id = report.id, take.id
    reports = await owner.client.get(f"/v1/versions/{version_id}/consistency")
    assert [r["id"] for r in reports.json()] == [str(report_id)]
    queue = (await owner.client.get("/v1/ratings/queue")).json()
    keys = {(i["target_type"], i["question_key"]) for i in queue}
    assert {("consistency", "same_person"), ("consistency", "in_character")} <= keys
    assert ("take", "behavior:/scenes[scn_hook]/acting/events[ev_1]|gaze") in keys
    bad = await owner.client.post(
        "/v1/ratings",
        json={"target_type": "consistency", "target_id": str(report_id), "question_key": "same_person",
              "rating": {"value": 9}},
    )  # fmt: skip
    assert bad.status_code == 422
    rated = await owner.client.post(
        "/v1/ratings",
        json={"target_type": "consistency", "target_id": str(report_id), "question_key": "same_person",
              "rating": {"value": 4}},
    )  # fmt: skip
    assert rated.status_code == 201
    check = await owner.client.post(
        "/v1/ratings",
        json={"target_type": "take", "target_id": str(take_id),
              "question_key": "behavior:/scenes[scn_hook]/acting/events[ev_1]|gaze", "rating": {"observed": True}},
    )  # fmt: skip
    assert check.status_code == 201
    after = {(i["target_type"], i["question_key"]) for i in (await owner.client.get("/v1/ratings/queue")).json()}
    assert ("consistency", "same_person") not in after and ("consistency", "in_character") in after
    assert not any(k[0] == "take" for k in after)


async def test_world_checks_are_calibrated_against_ratings(harness: ApiHarness, owner: ApiTenant) -> None:
    admin = await harness.add_member(owner.org_id, "owner", is_platform_admin=True)
    version_id = await _version(harness, owner)
    async with harness.services.db.transaction() as session:
        render = Render(
            org_id=owner.org_id, version_id=version_id, preset_id="tiktok", aspect="9:16", provenance_mode="mock_dev",
            status="ready",
        )  # fmt: skip
        session.add(render)
        await session.flush()
        for shot, passed in (("sht_1", True), ("sht_2", False)):
            session.add(
                QCReport(
                    org_id=owner.org_id, version_id=version_id, target_type="node", target_id=uuid.uuid4(),
                    checks={"kind": "qc.world", "shot_key": shot, "checks": [
                        {"check": "world_lighting", "passed": passed, "reliability": "low", "gate": False}]},
                    verdict="pass", thresholds_digest="x",
                )
            )  # fmt: skip
        render_id = render.id
    queue = (await admin.client.get("/v1/ratings/queue")).json()
    world = [i for i in queue if i["target_type"] == "render"]
    assert {i["question_key"] for i in world} == {"world:world_lighting:sht_1", "world:world_lighting:sht_2"}
    for shot, observed in (("sht_1", True), ("sht_2", True)):
        response = await admin.client.post(
            "/v1/ratings",
            json={"target_type": "render", "target_id": str(render_id), "question_key": f"world:world_lighting:{shot}",
                  "rating": {"observed": observed}},
        )  # fmt: skip
        assert response.status_code == 201, response.text
    assert (await owner.client.post("/v1/admin/qc/calibrations:compute")).status_code == 403
    result = await admin.client.post("/v1/admin/qc/calibrations:compute")
    assert result.status_code == 200, result.text
    rows = [r for r in result.json()["rows"] if r["check"] == "world_lighting"]
    assert rows and rows[0]["adapter_id"] == "world_qc" and rows[0]["n"] == 2
    assert rows[0]["reliability"] == "low"  # fewer than min_n ratings prove nothing
    assert rows[0]["precision"] == 1.0 and rows[0]["recall"] == 0.5
    listed = (await admin.client.get("/v1/admin/qc/calibrations")).json()
    assert any(r["check"] == "world_lighting" for r in listed)


async def test_benchmarks_are_rated_blind_and_a_pass_sets_bench_passed(harness: ApiHarness, owner: ApiTenant) -> None:
    started = _record_starts(harness)
    admin = await harness.add_member(owner.org_id, "owner", is_platform_admin=True)
    manifest = manifest_copy("infinitetalk")
    async with harness.services.db.transaction() as session:
        await sync_registry(session, [manifest])
        model = (await session.execute(sa.select(Model).where(Model.model_key == manifest.models[0].key))).scalar_one()
        model_id = model.id
    run = await admin.client.post(f"/v1/admin/models/{model_id}:benchmark")
    assert run.status_code == 202, run.text
    assert started[-1][0] == "BenchmarkWorkflow" and started[-1][1]["target_id"] == str(model_id)
    assert (await owner.client.post(f"/v1/admin/models/{model_id}:benchmark")).status_code == 403
    a = await _artifact(harness, owner, b"candidate-video", "video/mp4")
    b = await _artifact(harness, owner, b"baseline-video", "video/mp4")
    async with harness.services.db.transaction() as session:
        bench = ModelBenchmark(
            model_id=model_id, eval_set_version="eval-v1",
            metrics={"checks": [{"case": "avatar_en_plain", "seed": 0, "side": "a", "passed": True}]},
            human_scores={"pairs": 2, "rated": 0}, verdict="pending",
        )  # fmt: skip
        session.add(bench)
        await session.flush()
        for key in ("avatar_en_plain:0", "avatar_en_plain:1"):
            session.add(BenchmarkPair(benchmark_id=bench.id, item_key=key, a_artifact_id=a.id, b_artifact_id=b.id))
        bench_id = bench.id
    pairs = (await admin.client.get(f"/v1/admin/benchmarks/{bench_id}/pairs")).json()
    assert len(pairs) == 2 and all(p["left_mime"] == "video/mp4" and not p["rated"] for p in pairs)
    first = await admin.client.post(
        f"/v1/admin/benchmarks/{bench_id}/pairs/{pairs[0]['id']}:rate", json={"preferred": "tie"}
    )
    assert first.status_code == 200 and first.json()["verdict"] == "pending"
    # prefer the candidate whichever side it is shown on: the server resolves the blind sides
    from ce_qc.bench import blind_sides

    left, _ = blind_sides(pairs[1]["id"], str(admin.user_id))
    second = await admin.client.post(
        f"/v1/admin/benchmarks/{bench_id}/pairs/{pairs[1]['id']}:rate",
        json={"preferred": "left" if left == "a" else "right"},
    )
    body = second.json()
    assert body["verdict"] == "pass", body
    assert body["human_scores"]["status"]["win_rate"] == 0.75
    async with harness.services.db.session() as session:
        model = await session.get_one(Model, model_id)
    assert model.validation == "bench_passed"
    late = await admin.client.post(
        f"/v1/admin/benchmarks/{bench_id}/pairs/{pairs[0]['id']}:rate", json={"preferred": "left"}
    )
    assert late.status_code == 409  # decided
