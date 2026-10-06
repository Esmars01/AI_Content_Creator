"""I7 — Memory enters planning only through a pinned snapshot (§4, §18.5).

A plan pins exactly one MemorySnapshot per cast member; the CBS reads memory only from it; later
memory changes never alter an existing version's plan or cache keys (a replan reuses the pinned
snapshot unless memory is refreshed); snapshot rows are immutable in the database.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

import pytest
from ce_behavior.plan import version_cbs
from ce_build import build_graph, cache_key
from ce_build.refs import SnapshotRef
from ce_director import Director, PlanRequest
from ce_memory import MemoryRecord
from ce_testing.build import config_bundle, mock_catalog
from ce_testing.director import (
    NOW,
    alex_memory_records,
    alex_option,
    director_context,
    director_deps,
    fixture_inputs,
    maya_option,
    refs_for,
)
from ce_testing.fixtures import ALEX

pytestmark = pytest.mark.invariant

REQUEST = PlanRequest(input=fixture_inputs("explain_ai_agents")[0])


def _plan(memory: list[MemoryRecord] | None = None, pinned: dict | None = None):  # type: ignore[no-untyped-def,type-arg]
    ctx = director_context(creators=[alex_option(memory=memory), maya_option()], pinned=pinned)
    return asyncio.run(Director(director_deps()).plan(REQUEST, ctx))


def _keys(spec, snapshots):  # type: ignore[no-untyped-def]
    refs = asyncio.run(refs_for(spec, snapshots))
    graph = build_graph(spec, refs, config_bundle(), mock_catalog())
    return {n.key: cache_key(n, dict.fromkeys(n.deps, "0" * 64), "sha256:" + "1" * 64) for n in graph.nodes}, refs


def _snapshot_refs(outcome):  # type: ignore[no-untyped-def]
    return {
        sid: SnapshotRef(sid, d.digest(), tuple(i.model_dump(mode="json") for i in d.items))
        for sid, d in outcome.snapshots
    }


def test_a_plan_pins_one_snapshot_and_the_cbs_reads_memory_only_from_it() -> None:
    outcome = _plan()
    spec = outcome.spec
    (pin,) = spec.memory.snapshots
    (snapshot_id, draft) = outcome.snapshots[0]
    assert pin.snapshot_id == snapshot_id and pin.character_key == spec.cast[0].key
    assert {i.item_id for i in draft.items} == set(outcome.report.memory_items_used)
    assert ALEX.MEMORY_FACT_ID in draft.item_ids  # pinned items always enter
    _, refs = _keys(spec, _snapshot_refs(outcome))
    bundle = config_bundle()

    def cited(cbs: dict) -> set[str]:  # type: ignore[type-arg]
        return {
            s.split(":", 1)[1]
            for c in cbs.values()
            for p in c.provenance
            for s in p.supported_by
            if s.startswith("memory:")
        }

    memory = cited(version_cbs(spec, refs, bundle.vocab, bundle.app.behavior))
    assert memory and memory <= {str(i) for i in draft.item_ids}
    # Emptying the snapshot removes every memory citation: the snapshot is memory's only way in.
    refs.snapshots[snapshot_id] = SnapshotRef(snapshot_id, "sha256:" + "0" * 64, ())
    assert cited(version_cbs(spec, refs, bundle.vocab, bundle.app.behavior)) == set()


def test_later_memory_changes_never_alter_an_existing_versions_plan_or_cache_keys() -> None:
    first = _plan()
    keys_before, _ = _keys(first.spec, _snapshot_refs(first))
    (snapshot_id, draft) = first.snapshots[0]
    # Memory changes after the plan: a new habit, an edited phrase, a forgotten fact.
    records = alex_memory_records()
    changed = [dataclasses.replace(r, status="forgotten") if r.id == ALEX.MEMORY_FACT_ID else r for r in records]
    changed = [
        dataclasses.replace(r, value={**r.value, "text": "let me be clear"})
        if r.kind == "speech_habit.recurring_phrase"
        else r
        for r in changed
    ]
    changed.append(
        dataclasses.replace(
            records[0], id=ALEX.MEMORY_GAZE_ID.__class__(int=99), key="new", last_seen_at=NOW + timedelta(days=1)
        )
    )
    # The version keeps its snapshot: the build reads the snapshot, never live memory.
    keys_after, _ = _keys(first.spec, _snapshot_refs(first))
    assert keys_after == keys_before
    # A replan without refresh reuses the pinned snapshot: same pins, same plan, same keys.
    items = [i.model_dump(mode="json") for i in draft.items]
    replanned = _plan(memory=changed, pinned={ALEX.CREATOR_VERSION_ID: (snapshot_id, items)})
    assert replanned.snapshots == []  # nothing new to persist
    assert replanned.spec.memory == first.spec.memory

    def content(spec):  # type: ignore[no-untyped-def]
        data = spec.content_dict()
        data["brief"]["assumptions"] = [a for a in data["brief"]["assumptions"] if "pinned memory snapshot" not in a]
        return data

    assert content(replanned.spec) == content(first.spec)
    assert any("pinned memory snapshot" in a for a in replanned.spec.brief.assumptions)
    replanned_keys, _ = _keys(replanned.spec, _snapshot_refs(first))
    assert replanned_keys == keys_before
    # A fresh plan (refresh) takes a new snapshot that reflects the changes.
    refreshed = _plan(memory=changed)
    (new_id, new_draft) = refreshed.snapshots[0]
    assert new_id != snapshot_id and ALEX.MEMORY_FACT_ID not in new_draft.item_ids


@pytest.mark.infra
async def test_snapshot_rows_are_immutable(seeded_db, db) -> None:  # type: ignore[no-untyped-def]
    import sqlalchemy as sa
    from ce_memory.store import create_snapshot
    from sqlalchemy.exc import DBAPIError

    outcome = await Director(director_deps()).plan(REQUEST, director_context())
    draft = outcome.snapshots[0][1]
    async with db.transaction() as session:
        snapshot_id = await create_snapshot(session, ALEX.ORG_ID, draft)
    with pytest.raises(DBAPIError):
        async with db.transaction() as session:
            await session.execute(sa.text("DELETE FROM memory_snapshots WHERE id = :id"), {"id": snapshot_id})
