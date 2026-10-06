"""Phase 3 DoD on the Compose infrastructure: the fixture build produces a coverage report with
requested, compiled and observed entries for every CBS item; the behavior records (per-take and
viewer-level observations, QC reports, coverage summary, measured mock profiles) are written; a
re-route to another avatar engine flags the stale compiler approximation for re-proposal and
upgrades the coverage without any Director change."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
import sqlalchemy as sa
from ce_core.behavior.cbs import CBSContent
from ce_core.behavior.coverage import BehaviorCoverageReport
from ce_db.models.behavior import BehaviorObservation, ModelBehaviorProfile, QCReport
from ce_db.models.videos import EditProposal, VideoVersion
from ce_testing.fixtures import ALEX, example_spec_dict
from ce_testing.stack import Stack

pytestmark = [pytest.mark.infra, pytest.mark.behavior]

EV_1 = "/scenes[scn_hook]/acting/events[ev_1]"


async def _build(stack: Stack) -> tuple[Any, Any]:
    spec = example_spec_dict()
    spec["generation"]["seed_namespace"] = str(uuid.uuid4())
    submitted, handle = await stack.generate(ALEX.ORG_ID, ALEX.PROJECT_ID, spec)
    result = await asyncio.wait_for(handle.result(), 300)
    assert result.state == "ready", (result.failed, result.statuses)
    return submitted, result


async def _report(stack: Stack, result: Any) -> tuple[BehaviorCoverageReport, CBSContent, dict[str, Any]]:
    coverage = await stack.exec.docs.output(result.outputs["behavior.coverage:video"])
    resolved = await stack.exec.docs.output(result.outputs["behavior.resolve:scn_hook"])
    report = BehaviorCoverageReport.model_validate(coverage.data["report"])
    return report, CBSContent.model_validate(resolved.data["content"]), dict(coverage.data)


async def test_the_coverage_report_has_the_triad_for_every_cbs_item(stack: Stack) -> None:
    submitted, result = await _build(stack)
    report, cbs, data = await _report(stack, result)
    requested = {(c.item_ref, c.dimension) for c in cbs.requested_controls}
    assert {(e.item_ref, e.dimension) for e in report.entries} == requested and len(requested) == 25
    for entry in report.entries:
        assert entry.requested, entry.item_ref
        assert entry.compiled.level and entry.compiled.method
        assert entry.observed is not None and entry.outcome is not None, entry.item_ref
    assert report.stage == "observed" and data["decision"]["ladder"] == "qc_gate"
    assert data["targets"][f"{EV_1}|gaze"] == "sht_1:c1"

    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, submitted.version_id)
        rows = (
            (
                await session.execute(
                    sa.select(BehaviorObservation).where(BehaviorObservation.version_id == submitted.version_id)
                )
            )
            .scalars()
            .all()
        )
        reports = (
            (await session.execute(sa.select(QCReport).where(QCReport.version_id == submitted.version_id)))
            .scalars()
            .all()
        )
        profiles = (
            (await session.execute(sa.select(ModelBehaviorProfile).where(ModelBehaviorProfile.source == "mock")))
            .scalars()
            .all()
        )
    assert version.coverage_summary["items"] == 25 and "approximations_stale" not in (version.flags or [])
    viewer = [r for r in rows if r.level_scope == "viewer"]
    assert {(r.item_ref, r.dimension) for r in viewer} == requested
    assert {r.adapter_id for r in viewer if r.dimension.startswith("prosody")} == {"mock_voice"}
    assert {r.adapter_id for r in rows if r.level_scope == "take"} == {"mock_avatar_global"}
    take_reports = [r for r in reports if r.target_type == "take"]
    judged = [r for r in take_reports if r.checks["behavior"] is not None]
    assert len(judged) == 2  # both takes of the talking shot; the B-roll insert has no character
    assert all(r.checks["behavior"]["item_observations"] for r in judged)
    assert {p.adapter_id for p in profiles} >= {"mock_voice"}
    assert all(p.measured["n"] >= 1 for p in profiles)


async def test_a_reroute_flags_the_stale_approximation_and_upgrades_coverage(stack: Stack) -> None:
    # The operator turns the global-prompt mock off: the avatar now routes to the segment mock.
    stack.exec.catalog = replace(stack.exec.catalog, disabled=frozenset({"mock_avatar_global"}))
    submitted, result = await _build(stack)
    report, _, _ = await _report(stack, result)
    entries = {(e.item_ref, e.dimension): e for e in report.entries}
    assert str(entries[(EV_1, "gaze")].compiled.method) == "native_parametric"  # no cutaway crutch needed
    honored = sum(1 for e in report.entries if str(e.compiled.level) == "HONORED")
    assert honored >= 18

    async with stack.exec.db.session() as session:
        version = await session.get_one(VideoVersion, submitted.version_id)
        proposals = (
            (await session.execute(sa.select(EditProposal).where(EditProposal.version_id == submitted.version_id)))
            .scalars()
            .all()
        )
    assert "approximations_stale" in version.flags
    (proposal,) = proposals
    assert proposal.status == "proposed" and proposal.selection["kind"] == "approximations_stale"
    (op,) = proposal.ops  # a typed operation (§28), computed like any edit
    assert (op["op"], op["scene_key"], op["shot_key"]) == ("remove_shot", "scn_hook", "sht_2")
    assert "mock_avatar_segment" in op["reason"]
    assert proposal.patch and proposal.impact["planner"] == "system"
    (delta,) = proposal.coverage_delta["stale"]
    assert delta["approximates"] == EV_1 and delta["current"]["adapter_id"] == "mock_avatar_segment"
    assert delta["current"]["declared"] == {"dimension": "gaze", "control": "parametric", "temporal_precision": "word"}
