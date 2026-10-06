"""Records derived from behavior node outputs (§16.3, §16.6), written for executed and cached nodes
(the take's observed-behavior document and signature are recorded with the other take fields):

- `qc.shot` → a `qc_reports` row per take and one `behavior_observations` row per judged item
  (`level_scope = take`);
- `behavior.coverage` → viewer-level `behavior_observations`, `video_versions.coverage_summary`,
  the `coverage_changed` flag on a coverage downgrade, refreshed `model_behavior_profiles` and a
  `coverage.updated` event.

`behavior_observations` is append-only: rows are written once per (version, take, scope).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_behavior.profiles import ObservationRow, aggregate
from ce_core.behavior.coverage import outcome_of
from ce_core.build import ExecutionGraph, ExecutionNode
from ce_core.enums import CoverageLevel, ObservationVerdict, VersionFlag
from ce_db import execution as rec
from ce_db.models.behavior import BehaviorObservation, ModelBehaviorProfile, QCReport
from ce_db.models.videos import VideoVersion
from sqlalchemy.ext.asyncio import AsyncSession

from ce_exec.context import ExecServices, VersionData
from ce_exec.outputs import NodeOutput
from ce_exec.qcgate import take_checks, take_verdict

__all__ = ["coverage_records", "qc_shot_records", "refresh_profiles"]


def _route_of(graph: ExecutionGraph, shot_key: str, take: int) -> dict[str, str | None]:
    return _route_fields(graph.by_key().get(f"avatar.render:{shot_key}:c1:t{take}"))


def _target_route(graph: ExecutionGraph, target: str | None) -> dict[str, str | None]:
    """The engine route of a compile target: `<shot>:c<n>` → the shot's avatar render,
    `<segment>` → the segment's TTS."""
    if not target:
        return _route_fields(None)
    if ":c" in target:
        shot_key, _, chunk = target.partition(":c")
        return _route_fields(graph.by_key().get(f"avatar.render:{shot_key}:c{chunk}:t1"))
    return _route_fields(graph.by_key().get(f"tts.segment:{target}"))


def _route_fields(node: ExecutionNode | None) -> dict[str, str | None]:
    route = node.route if node is not None else None
    return {
        "adapter_id": route.adapter_id if route else None,
        "translator_version": route.translator_version if route else None,
        "model_revision": route.revision if route else None,
    }


async def _has_rows(session: AsyncSession, org_id: UUID, version_id: UUID, *, take_id: UUID | None) -> bool:
    query = sa.select(sa.func.count()).where(
        BehaviorObservation.org_id == org_id,
        BehaviorObservation.version_id == version_id,
        BehaviorObservation.level_scope == ("take" if take_id else "viewer"),
    )
    if take_id is not None:
        query = query.where(BehaviorObservation.take_id == take_id)
    return bool((await session.execute(query)).scalar_one())


def _requested(control: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
    if control is None:
        return {"value": fallback}
    keys = ("value", "priority", "planner_confidence", "observation_reliability", "temporal_precision")
    return {k: control.get(k) for k in keys}


async def qc_shot_records(
    session: AsyncSession,
    svc: ExecServices,
    org_id: UUID,
    data: VersionData,
    graph: ExecutionGraph,
    node: ExecutionNode,
    output: NodeOutput,
    cbs_controls: dict[tuple[str, str], dict[str, Any]],
) -> None:
    behavior = output.data.get("behavior")
    if not node.shot_key or not node.take:
        return
    take = await rec.upsert_take(
        session, org_id, version_id=data.version_id, shot_key=node.shot_key, take_index=node.take
    )
    if take.qc_report_id is None:
        policy_digest = svc.bundle.digests.get("qc/behavior.yaml", "")
        report = QCReport(
            org_id=org_id,
            version_id=data.version_id,
            target_type="take",
            target_id=take.id,
            checks=take_checks(output),
            verdict=take_verdict(output),
            thresholds_digest=policy_digest or "none",
        )
        session.add(report)
        await session.flush()
        take.qc_report_id = report.id
    if behavior is None or await _has_rows(session, org_id, data.version_id, take_id=take.id):
        return
    route = _route_of(graph, node.shot_key, node.take)
    levels = dict(behavior.get("levels", {}))
    language = str(data.spec.meta.language)
    for item in behavior.get("item_observations", []):
        key = f"{item['item_ref']}|{item['dimension']}"
        level, _, method = str(levels.get(key, "UNSUPPORTED|omit")).partition("|")
        session.add(
            BehaviorObservation(
                org_id=org_id,
                version_id=data.version_id,
                take_id=take.id,
                level_scope="take",
                item_ref=item["item_ref"],
                character_key=item["character_key"],
                dimension=item["dimension"],
                requested=_requested(cbs_controls.get((item["item_ref"], item["dimension"])), item["item_ref"]),
                level=level,
                method=method or "omit",
                approximation_executed=None,
                verdict=item["verdict"],
                outcome=str(outcome_of(CoverageLevel(level), ObservationVerdict(item["verdict"]))),
                measures=dict(item.get("measures", {})),
                confidence=float(item.get("confidence", 0.0)),
                language=language,
                **route,
            )
        )
    await session.flush()


async def coverage_records(
    session: AsyncSession,
    svc: ExecServices,
    org_id: UUID,
    data: VersionData,
    graph: ExecutionGraph,
    output: NodeOutput,
    cbs_controls: dict[tuple[str, str], dict[str, Any]],
) -> set[str]:
    """Viewer-level rows, the version's coverage summary and flags. Returns the adapters whose
    measured profiles should be refreshed."""
    report = dict(output.data.get("report", {}))
    entries = list(report.get("entries", []))
    version = await session.get_one(VideoVersion, data.version_id)
    summary = {
        **dict(output.data.get("summary", {})),
        "items": len(entries),
        "decision": (output.data.get("decision") or {}).get("action"),
        "downgrades": len(output.data.get("downgrades", [])),
    }
    version.coverage_summary = summary
    if output.data.get("downgrades") and VersionFlag.COVERAGE_CHANGED not in (version.flags or []):
        version.flags = [*(version.flags or []), VersionFlag.COVERAGE_CHANGED.value]
    adapters = {n.route.adapter_id for n in graph.nodes if n.kind in ("avatar.render", "tts.segment") and n.route}
    if await _has_rows(session, org_id, data.version_id, take_id=None):
        return adapters
    language = str(data.spec.meta.language)
    targets = dict(output.data.get("targets", {}))
    for entry in entries:
        observed = entry.get("observed") or {}
        compiled = entry.get("compiled") or {}
        if not observed or not entry.get("outcome"):
            continue
        session.add(
            BehaviorObservation(
                org_id=org_id,
                version_id=data.version_id,
                take_id=None,
                level_scope="viewer",
                item_ref=entry["item_ref"],
                character_key=entry["character_key"],
                dimension=entry["dimension"],
                requested=_requested(cbs_controls.get((entry["item_ref"], entry["dimension"])), entry["requested"]),
                level=compiled.get("level", "UNSUPPORTED"),
                method=compiled.get("method", "omit"),
                approximation_executed=entry.get("approximation_executed"),
                verdict=observed["verdict"],
                outcome=entry["outcome"],
                measures=dict(observed.get("measures", {})),
                confidence=float(observed.get("confidence", 0.0)),
                language=language,
                **_target_route(graph, targets.get(f"{entry['item_ref']}|{entry['dimension']}")),
            )
        )
    await session.flush()
    return adapters


async def refresh_profiles(session: AsyncSession, svc: ExecServices, adapters: set[str]) -> int:
    """Re-aggregates take-level observations of `adapters` into `model_behavior_profiles` (§16.6;
    on demand after each coverage report, across organizations — profiles are platform data)."""
    if not adapters:
        return 0
    rows = (
        await session.execute(
            sa.select(
                BehaviorObservation.adapter_id,
                BehaviorObservation.translator_version,
                BehaviorObservation.model_revision,
                BehaviorObservation.dimension,
                BehaviorObservation.language,
                BehaviorObservation.method,
                BehaviorObservation.verdict,
                BehaviorObservation.level_scope,
            ).where(BehaviorObservation.adapter_id.in_(sorted(adapters)))
        )
    ).all()
    audio = frozenset(k for k, d in svc.bundle.vocab.dimensions.items() if d.channel == "audio")
    profiles = aggregate((ObservationRow(*r) for r in rows), audio_dimensions=audio)
    for key, measured in profiles.items():
        manifest = svc.catalog.manifests.get(key.adapter_id)
        source = "mock" if manifest is not None and manifest.mock else "production"
        declared: dict[str, Any] = {}
        if manifest is not None and manifest.behavior_matrix is not None:
            declared = manifest.behavior_matrix.control(key.dimension).model_dump(mode="json")
        existing = (
            await session.execute(
                sa.select(ModelBehaviorProfile).where(
                    ModelBehaviorProfile.adapter_id == key.adapter_id,
                    ModelBehaviorProfile.translator_version == key.translator_version,
                    ModelBehaviorProfile.revision == key.revision,
                    ModelBehaviorProfile.dimension == key.dimension,
                    ModelBehaviorProfile.language == key.language,
                    ModelBehaviorProfile.source == source,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ModelBehaviorProfile(
                    adapter_id=key.adapter_id,
                    translator_version=key.translator_version,
                    revision=key.revision,
                    dimension=key.dimension,
                    language=key.language,
                    source=source,
                    declared=declared,
                    measured=measured,
                )
            )
        else:
            existing.declared = declared
            existing.measured = measured
    await session.flush()
    return len(profiles)
