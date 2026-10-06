"""Database-derived operations metrics (Phase 14, `ce_db.ops_metrics`): each series is computed
from rows in the window, labelled without ids, and set as gauges by the scheduler leader."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from ce_config.schemas import SchedulerConfig
from ce_contracts.plugins import discover
from ce_db import ops_metrics
from ce_db.models.assets import ExecutionNode, GenerationJob, JobAttempt
from ce_db.models.behavior import BehaviorObservation, QCReport
from ce_db.models.creators import ConsistencyReport
from ce_db.models.platform import CostLedger
from ce_db.models.videos import Render, Video, VideoVersion
from ce_db.session import Database
from ce_obs.metrics import REGISTRY
from ce_scheduler.service import Scheduler
from ce_testing.database import TestDatabase
from ce_testing.fixtures import ALEX
from ce_testing.seed import example_version_row, seed_dev

pytestmark = pytest.mark.infra


@pytest.fixture(scope="module")
def ops_db(repo_vocab: Any) -> Any:
    database = TestDatabase(prefix="ce_ops")

    async def create() -> None:
        await database.create()
        db = Database(database.url, pool_size=1)
        async with db.transaction() as session:
            await seed_dev(session, repo_vocab)
            session.add(Video(id=ALEX.VIDEO_ID, org_id=ALEX.ORG_ID, project_id=ALEX.PROJECT_ID))
            await session.flush()
            session.add(VideoVersion(**example_version_row()))
            await session.flush()
            org, version = ALEX.ORG_ID, ALEX.VERSION_ID
            job = GenerationJob(
                org_id=org, kind="generate", target_type="video_version", target_id=version, video_version_id=version
            )
            session.add(job)
            await session.flush()
            avatar = ExecutionNode(
                org_id=org,
                job_id=job.id,
                version_id=version,
                node_key="avatar.render:s1",
                node_kind="avatar.render",
                shot_key="s1",
                route={"adapter_id": "mock_avatar"},
            )
            world = ExecutionNode(
                org_id=org, job_id=job.id, version_id=version, node_key="qc.world", node_kind="qc.world"
            )
            session.add_all([avatar, world])
            await session.flush()
            attempts = (
                (1, "initial", "failed", "oom"),
                (2, "infra_retry", "failed", "lease_expired"),
                (3, "infra_retry", "succeeded", None),
            )
            for no, reason, status, error in attempts:
                session.add(
                    JobAttempt(
                        org_id=org,
                        node_id=avatar.id,
                        attempt_no=no,
                        reason=reason,
                        status=status,
                        error_class=error,
                        started_at=datetime.now(UTC),
                    )
                )
            session.add_all(
                [
                    QCReport(
                        org_id=org,
                        version_id=version,
                        target_type="shot",
                        target_id=avatar.id,
                        checks={"shot_key": "s1"},
                        verdict="fail",
                        thresholds_digest="t",
                    ),
                    QCReport(
                        org_id=org,
                        version_id=version,
                        target_type="node",
                        target_id=world.id,
                        checks={"kind": "qc.world"},
                        verdict="warn",
                        thresholds_digest="t",
                    ),
                ]
            )
            for verdict, outcome in (("CONFIRMED", "HONORED_CONFIRMED"), ("NOT_MEASURABLE", "NOT_MEASURABLE")):
                session.add(
                    BehaviorObservation(
                        org_id=org,
                        version_id=version,
                        level_scope="viewer",
                        item_ref=f"w{uuid.uuid4().hex[:4]}",
                        character_key="host",
                        dimension="gaze",
                        level="HONORED",
                        method="native_parametric",
                        verdict=verdict,
                        outcome=outcome,
                        adapter_id="mock_avatar",
                    )
                )
            session.add(
                ConsistencyReport(
                    org_id=org,
                    creator_id=ALEX.CREATOR_ID,
                    creator_version_id=ALEX.CREATOR_VERSION_ID,
                    version_id=version,
                    verdict="out_of_band",
                )
            )
            session.add(
                CostLedger(
                    org_id=org,
                    version_id=version,
                    kind="gpu",
                    quantity=60,
                    unit="gpu_second",
                    unit_price_usd=Decimal("0.001"),
                    amount_usd=Decimal("0.06"),
                )
            )
            session.add(
                Render(
                    org_id=org,
                    version_id=version,
                    preset_id="p",
                    aspect="9:16",
                    provenance_mode="mock_dev",
                    status="ready",
                )
            )
        await db.dispose()

    asyncio.run(create())
    yield database
    asyncio.run(database.drop())


async def test_ops_metrics_are_derived_from_rows_in_the_window(ops_db: TestDatabase) -> None:
    db = Database(ops_db.url, pool_size=2)
    try:
        async with db.session() as session:
            m = await ops_metrics.collect(session, since=datetime.now(UTC) - timedelta(hours=1))
            later = await ops_metrics.collect(session, since=datetime.now(UTC) + timedelta(hours=1))
    finally:
        await db.dispose()
    assert m.qc_verdicts == {("mock_avatar", "en-US", "fail"): 1}
    assert m.attempts == {
        ("initial", "failed", "oom"): 1,
        ("infra_retry", "failed", "lease_expired"): 1,
        ("infra_retry", "succeeded", "none"): 1,
    }
    assert m.coverage == {
        ("mock_avatar", "gaze", "HONORED_CONFIRMED"): 1,
        ("mock_avatar", "gaze", "NOT_MEASURABLE"): 1,
    }
    assert m.not_measurable == {"gaze": 1}
    assert m.observations_total == 2
    assert m.world_qc == {"warn": 1}
    assert m.consistency == {"out_of_band": 1}
    assert m.spend_usd == pytest.approx(0.06)
    assert m.rendered_minutes == pytest.approx(6.0 / 60.0)  # the example spec targets 6 s
    assert m.cost_per_output_minute == pytest.approx(0.6)
    assert m.as_dict()["qc_verdicts"] == {"mock_avatar|en-US|fail": 1}
    # nothing in a window that starts after the rows (the memory-conflict gauge is a current state)
    assert later.qc_verdicts == {} and later.spend_usd == 0 and later.cost_per_output_minute is None


async def test_scheduler_leader_sets_the_ops_gauges(ops_db: TestDatabase) -> None:
    db = Database(ops_db.url, pool_size=2)
    s = Scheduler(
        db=db,
        storage=None,  # type: ignore[arg-type]  # not used by the metrics loop
        bucket="unused",
        registry=discover(app_env="test", include_mocks=True),
        completer=None,  # type: ignore[arg-type]
        config=SchedulerConfig(ops_metrics_window_s=3600),
        registration_token="reg",
    )
    try:
        assert await s.refresh_ops_metrics() is not None
    finally:
        await db.dispose()
    value = REGISTRY.get_sample_value
    assert value("ce_ops_qc_shots", {"adapter": "mock_avatar", "language": "en-US", "verdict": "fail"}) == 1
    assert value("ce_ops_attempts", {"reason": "initial", "status": "failed", "error_class": "oom"}) == 1
    assert value("ce_ops_not_measurable", {"dimension": "gaze"}) == 1
    assert value("ce_ops_consistency", {"verdict": "out_of_band"}) == 1
    assert value("ce_ops_window_seconds") == 3600
    assert value("ce_ops_spend_usd") == pytest.approx(0.06)


def test_the_worker_api_port_serves_no_metrics() -> None:
    """Remote GPU workers reach the scheduler's API port; metrics are on the side port only."""
    from pathlib import Path

    from ce_config.settings import load_effective
    from ce_scheduler.app import create_app

    root = Path(__file__).resolve().parents[3]
    app = create_app(load_effective(root / "config"), start_loops=False)
    assert "/metrics" not in {getattr(r, "path", "") for r in app.routes}
