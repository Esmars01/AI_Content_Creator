"""`MemoryUpdateWorkflow` (§18.4, Phase 12, ADR 0057): every Creator Memory write path except the
usage event on `ready` (written by the build itself since Phase 4) and authored items (the API).

One job kind, `memory_update`, with a `trigger` argument; the decisions are pure functions in
`ce_memory.loop`, this module loads and writes rows (studio loop, ADR 0055):

| trigger | target | writes |
| --- | --- | --- |
| `approved` | video version | persona facts and stances the script asserts → `proposed` (source `plan`) |
| `ready` | video version | habit evidence measured on the takes → observation items (merged; promoted |
|  |  | past the `config/memory.yaml` thresholds, never from mock evidence unless allowed) |
| `exported` | video version | proposed persona facts/stances of the video → `active`; the `exported` usage event |
| `edit_applied` | edit proposal | N edits of one kind on a creator (or an accepted critique |
|  |  | proposal) → a `proposed` preference |
| `embed` | creator | embeddings for live text-like items without one (or embedded by another model) |

Every trigger ends with the `embed` step for the creators it touched, so new items are indexed.
Embedding calls are `ModelCall`s (recorded as nodes of the job), pinned to the adapter
`ce_exec.embeddings.embedding_route` chooses. Memory never changes an existing version (I7): the
jobs write items, planning reads them only through new snapshots.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.identity.memory import dedup_key, value_hash
from ce_db.models.assets import GenerationJob, Notification
from ce_db.models.creators import CreatorVersion
from ce_db.models.memory import CreatorMemoryItem
from ce_db.models.videos import DirectorRun, EditProposal, VideoVersion
from ce_memory.loop import (
    edit_signatures,
    habit_evidence,
    merge_habit,
    persona_proposals,
    preference_proposals,
)
from ce_memory.retrieval import TEXT_KINDS
from ce_memory.store import LIVE, embedding_text, items_without_embedding, link_conflicts, propose_item
from ce_obs import get_logger
from ce_obs.events import EventType

from ce_exec.context import ExecServices
from ce_exec.embeddings import embed_request, embed_texts, embedding_route, model_string
from ce_exec.studio import ModelCall, StudioContext, StudioError, StudioStep, stage

__all__ = ["TRIGGERS", "enqueue", "memory_embed_store", "memory_start"]

_log = get_logger("ce.exec.memory")

TRIGGERS = ("approved", "ready", "exported", "edit_applied", "embed")
OBSERVATION_KINDS_SOURCE = "observation"


def context(
    svc: ExecServices,
    org_id: UUID,
    job_id: UUID,
    *,
    trigger: str,
    target_id: UUID,
    args: dict[str, Any] | None = None,
    user_id: UUID | None = None,
) -> dict[str, Any]:
    build = svc.bundle.app.build
    return StudioContext(
        kind="memory_update",
        org_id=str(org_id),
        job_id=str(job_id),
        target_id=str(target_id),
        args={"trigger": trigger, **(args or {})},
        user_id=str(user_id) if user_id else None,
        model_timeout_s=float(build.model_node_timeout_s),
        cpu_timeout_s=float(build.cpu_node_timeout_s),
    ).model_dump(mode="json")


async def enqueue(
    svc: ExecServices,
    org_id: UUID,
    *,
    trigger: str,
    target_type: str,
    target_id: UUID,
    version_id: UUID | None = None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Writes the `memory_update` job row and returns its workflow argument (the orchestrator
    starts it as an abandoned child); None when the trigger does not apply."""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown memory trigger {trigger!r}")
    memory = svc.bundle.memory
    if trigger == "ready" and (memory is None or not memory.update_after_ready):
        return None
    async with svc.db.transaction() as session:
        job = GenerationJob(
            org_id=org_id,
            kind="memory_update",
            status="queued",
            target_type=target_type,
            target_id=target_id,
            input={"trigger": trigger, **(args or {})},
            video_version_id=version_id,
        )
        session.add(job)
        await session.flush()
        job.temporal_workflow_id = f"memory_update-{job.id}"
        job_id = job.id
    return context(svc, org_id, job_id, trigger=trigger, target_id=target_id, args=args)


# ---------------------------------------------------------------------- helpers
async def _version(svc: ExecServices, org_id: UUID, version_id: UUID) -> Any:
    try:
        return await svc.version(org_id, version_id, fresh=True)
    except LookupError as exc:
        raise StudioError("the version does not exist") from exc


def _creators_of(data: Any) -> dict[str, UUID]:
    """character key → creator id of a version's cast."""
    out: dict[str, UUID] = {}
    for member in data.spec.cast:
        creator = data.refs.creators.get(member.creator_version_id)
        if creator is not None:
            out[member.key] = creator.creator_id
    return out


async def _notify_conflicts(svc: ExecServices, org_id: UUID, creator_id: UUID, items: list[CreatorMemoryItem]) -> None:
    if not items:
        return
    ids = sorted({str(i.id) for i in items})
    async with svc.db.transaction() as session:
        session.add(
            Notification(
                org_id=org_id, kind="memory_conflict", payload={"creator_id": str(creator_id), "memory_item_ids": ids}
            )
        )
    await svc.publish(org_id, EventType.MEMORY_CONFLICT, {"creator_id": str(creator_id), "memory_item_ids": ids})


async def _publish_proposed(svc: ExecServices, org_id: UUID, creator_id: UUID, ids: list[UUID], trigger: str) -> None:
    if ids:
        await svc.publish(
            org_id,
            EventType.MEMORY_PROPOSED,
            {"creator_id": str(creator_id), "memory_item_ids": [str(i) for i in ids], "trigger": trigger},
        )


async def _embed_step(
    svc: ExecServices, ctx: StudioContext, creator_ids: list[str], result: dict[str, Any]
) -> StudioStep:
    """Model calls embedding the creators' live text-like items that lack a comparable vector."""
    decision = embedding_route(svc)
    if decision is None:
        return StudioStep(done=True, result={**result, "embedded": 0, "embedding": "no embed.text adapter is routable"})
    model = model_string(decision)
    org_id = UUID(ctx.org_id)
    batches: list[dict[str, Any]] = []
    calls: list[ModelCall] = []
    size = int(svc.bundle.app.embeddings.batch_size)
    async with svc.db.session() as session:
        for creator_id in sorted(set(creator_ids)):
            items = await items_without_embedding(
                session, org_id, UUID(creator_id), model=model, kinds=sorted(TEXT_KINDS)
            )
            texts = [(str(i.id), embedding_text(i)) for i in items if embedding_text(i)]
            for start in range(0, len(texts), size):
                chunk = texts[start : start + size]
                key = f"memory.embed:{len(calls)}"
                calls.append(
                    ModelCall(
                        key=key,
                        capability="embed.text",
                        adapter_id=decision.adapter_id,
                        request=embed_request(svc, [t for _, t in chunk]),
                    )
                )
                batches.append({"key": key, "item_ids": [i for i, _ in chunk]})
    if not calls:
        return StudioStep(done=True, result={**result, "embedded": 0, "embedding_model": model})
    return StudioStep(
        calls=calls, next="embed_store", progress=0.7, data={"batches": batches, "model": model, "result": result}
    )


@stage("memory_update", "embed_store")
async def memory_embed_store(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    org_id = UUID(ctx.org_id)
    outputs = {o["key"]: o for o in data.get("outputs", [])}
    dim = int(svc.settings.embedding_dim)
    stored = 0
    async with svc.db.transaction() as session:
        for batch in data.get("batches", []):
            out = outputs.get(batch["key"])
            vectors = list(dict((out or {}).get("result") or {}).get("vectors") or [])
            if len(vectors) != len(batch["item_ids"]) or any(len(v) != dim for v in vectors):
                raise StudioError(f"embed.text returned unusable vectors for {batch['key']} (EMBEDDING_DIM {dim})")
            for item_id, vector in zip(batch["item_ids"], vectors, strict=True):
                await session.execute(
                    sa.update(CreatorMemoryItem)
                    .where(CreatorMemoryItem.org_id == org_id, CreatorMemoryItem.id == UUID(item_id))
                    .values(embedding=vector, embedding_model=data["model"])
                )
                stored += 1
    return StudioStep(
        done=True, result={**dict(data.get("result") or {}), "embedded": stored, "embedding_model": data["model"]}
    )


# ---------------------------------------------------------------------- triggers
@stage("memory_update", "start")
async def memory_start(svc: ExecServices, ctx: StudioContext, data: dict[str, Any]) -> StudioStep:
    trigger = str(ctx.args.get("trigger") or "")
    if trigger == "embed":
        return await _embed_step(svc, ctx, [ctx.target_id], {"trigger": trigger})
    if trigger == "approved":
        return await _approved(svc, ctx)
    if trigger == "ready":
        return await _ready(svc, ctx)
    if trigger == "exported":
        return await _exported(svc, ctx)
    if trigger == "edit_applied":
        return await _edit_applied(svc, ctx)
    raise StudioError(f"unknown memory trigger {trigger!r}")


async def _assertions_of(svc: ExecServices, org_id: UUID, version_id: UUID) -> list[dict[str, Any]]:
    """The fact-check stage's persona assertions of the version's plan (walking up derived
    versions to the plan that ran the Director)."""
    current: UUID | None = version_id
    async with svc.db.session() as session:
        for _ in range(64):
            if current is None:
                return []
            run = (
                await session.execute(
                    sa.select(DirectorRun.output)
                    .where(
                        DirectorRun.org_id == org_id,
                        DirectorRun.version_id == current,
                        DirectorRun.stage == "fact_check",
                    )
                    .order_by(DirectorRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if run is not None:
                return [dict(a) for a in dict(run or {}).get("assertions", [])]
            current = (
                await session.execute(
                    sa.select(VideoVersion.parent_version_id).where(
                        VideoVersion.org_id == org_id, VideoVersion.id == current
                    )
                )
            ).scalar_one_or_none()
    return []


async def _approved(svc: ExecServices, ctx: StudioContext) -> StudioStep:
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    data = await _version(svc, org_id, version_id)
    creators = _creators_of(data)
    assertions = await _assertions_of(svc, org_id, version_id)
    proposed: dict[str, list[UUID]] = defaultdict(list)
    for member in data.spec.cast:
        creator_id = creators.get(member.key)
        creator = data.refs.creators.get(member.creator_version_id)
        if creator_id is None or creator is None:
            continue
        mine = [a for a in assertions if _speaker(data.spec, a.get("segment_key")) in (member.key, None)]
        display = dict(dict(creator.dna or {}).get("identity") or {}).get("display_name")
        names = [str(display)] if display else []
        conflicts: list[CreatorMemoryItem] = []
        async with svc.db.transaction() as session:
            for proposal in persona_proposals(mine, self_names=names):
                item_id = await propose_item(
                    session,
                    org_id,
                    vocab=svc.bundle.vocab,
                    creator_id=creator_id,
                    kind=proposal.kind,
                    value=proposal.value,
                    text=proposal.text,
                    source={
                        "type": "plan",
                        "video_id": str(data.video_id),
                        "version_id": str(version_id),
                        "element_key": proposal.segment_key,
                    },
                    confidence=0.6,
                    conflicts=conflicts,
                )
                if item_id is not None:
                    proposed[str(creator_id)].append(item_id)
        await _notify_conflicts(svc, org_id, creator_id, conflicts)
    for creator_key, ids in proposed.items():
        await _publish_proposed(svc, org_id, UUID(creator_key), ids, "approved")
    result = {"trigger": "approved", "proposed": sum(len(v) for v in proposed.values()), "assertions": len(assertions)}
    return await _embed_step(svc, ctx, [str(c) for c in creators.values()], result)


def _speaker(spec: Any, segment_key: Any) -> str | None:
    for segment in spec.script.segments:
        if segment.key == segment_key:
            return str(segment.speaker_key)
    return None


async def _ready(svc: ExecServices, ctx: StudioContext) -> StudioStep:
    from ce_qc.consistency import video_features

    from ce_exec.creator_test import _outputs

    memory = svc.bundle.memory
    if memory is None:
        raise StudioError("config/memory.yaml is required")
    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    data = await _version(svc, org_id, version_id)
    creators = _creators_of(data)
    outputs = await _outputs(svc, org_id, version_id)
    promo = memory.promotion
    summary: dict[str, Any] = {"trigger": "ready", "creators": {}}
    for member in data.spec.cast:
        creator_id = creators.get(member.key)
        if creator_id is None:
            continue
        tracks: list[dict[str, Any]] = []
        mock = False
        for _, out in outputs.get("behavior.observe", []):
            observed = dict(out.data.get("observed") or {})
            for analyzer in observed.get("analyzers", []):
                mock = mock or str(analyzer.get("adapter_id", "")).startswith("mock")
            tracks.extend(t for t in observed.get("tracks", []) if t.get("character_key") == member.key)
        features = video_features(tracks=tracks, tracks_mock=mock)
        written: list[dict[str, Any]] = []
        conflicts: list[CreatorMemoryItem] = []
        proposed: list[UUID] = []
        async with svc.db.transaction() as session:
            for evidence in habit_evidence(features, [h.model_dump() for h in memory.observation_habits]):
                outcome = await _merge_observation(
                    session,
                    svc,
                    org_id,
                    creator_id,
                    evidence,
                    video_id=str(data.video_id),
                    version_id=str(version_id),
                    promo=promo,
                    conflicts=conflicts,
                )
                if outcome is not None:
                    written.append(outcome)
                    if outcome.get("created"):
                        proposed.append(UUID(outcome["item_id"]))
        await _notify_conflicts(svc, org_id, creator_id, conflicts)
        await _publish_proposed(svc, org_id, creator_id, proposed, "ready")
        summary["creators"][str(creator_id)] = {"habits": written, "mock_evidence": mock}
    return StudioStep(done=True, result=summary)


async def _merge_observation(
    session: Any,
    svc: ExecServices,
    org_id: UUID,
    creator_id: UUID,
    evidence: Any,
    *,
    video_id: str,
    version_id: str,
    promo: Any,
    conflicts: list[CreatorMemoryItem],
) -> dict[str, Any] | None:
    vocab = svc.bundle.vocab
    live = (
        (
            await session.execute(
                sa.select(CreatorMemoryItem)
                .where(
                    CreatorMemoryItem.org_id == org_id,
                    CreatorMemoryItem.creator_id == creator_id,
                    CreatorMemoryItem.kind == evidence.kind,
                    CreatorMemoryItem.status.in_(LIVE),
                    CreatorMemoryItem.deleted_at.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    observed = next((i for i in live if dict(i.source or {}).get("type") == OBSERVATION_KINDS_SOURCE), None)
    authored = next((i for i in live if dict(i.source or {}).get("type") == "authored"), None)
    authored_value = None
    if authored is not None and isinstance(dict(authored.value or {}).get(evidence.field), (int, float)):
        authored_value = float(authored.value[evidence.field])
    previous = list(dict(observed.source or {}).get("evidence", [])) if observed is not None else []
    merge = merge_habit(
        evidence,
        video_id=video_id,
        previous=previous,
        authored_value=authored_value,
        min_evidence_count=promo.min_evidence_count,
        min_confidence=promo.min_confidence,
        quantum=promo.habit_quantum,
        conflict_tolerance=promo.conflict_tolerance,
        allow_mock=promo.allow_mock_evidence,
    )
    typed = vocab.validate_memory_value(evidence.kind, merge.value).model_dump(mode="json")
    digest = value_hash(typed)
    key = dedup_key(vocab, evidence.kind, typed)
    same_value = next((i for i in live if i.value_hash == digest and i is not observed), None)
    source = {
        "type": OBSERVATION_KINDS_SOURCE,
        "video_id": video_id,
        "version_id": version_id,
        "feature": evidence.feature,
        "evidence": merge.evidence,
        "mock": any(bool(e.get("mock")) for e in merge.evidence),
        "promotion": {"promote": merge.promote, "reasons": merge.reasons},
    }
    now = svc.clock()
    created = False
    if same_value is not None:
        # an identical live value (authored, typically) already says this: the evidence reinforces it
        if observed is not None:
            observed.source, observed.evidence_count, observed.last_seen_at = source, merge.evidence_count, now
        return {
            "kind": evidence.kind,
            "value": typed,
            "reinforces": str(same_value.id),
            "evidence_count": merge.evidence_count,
        }
    if observed is None:
        observed = CreatorMemoryItem(
            org_id=org_id,
            creator_id=creator_id,
            category=evidence.kind.split(".")[0],
            kind=evidence.kind,
            key=key,
            value_hash=digest,
            value=typed,
            text=f"Measured {evidence.feature.replace('_', ' ')}: {typed[evidence.field]}",
            vocab_version=vocab.version,
            source=source,
            confidence=merge.confidence,
            evidence_count=merge.evidence_count,
            status="proposed",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(observed)
        created = True
    else:
        observed.value, observed.value_hash, observed.key = typed, digest, key
        observed.source, observed.confidence, observed.evidence_count = source, merge.confidence, merge.evidence_count
        observed.text = f"Measured {evidence.feature.replace('_', ' ')}: {typed[evidence.field]}"
        observed.last_seen_at = now
        observed.conflict_ids, observed.conflict_state = [], "none"
    if merge.promote and observed.status == "proposed":
        observed.status = "active"
    await session.flush()
    if merge.conflicts_with_authored:
        linked = await link_conflicts(session, observed)
        if linked:
            conflicts.extend([observed, *linked])
    return {
        "kind": evidence.kind,
        "item_id": str(observed.id),
        "value": typed,
        "status": observed.status,
        "evidence_count": merge.evidence_count,
        "confidence": merge.confidence,
        "reasons": merge.reasons,
        "created": created,
    }


async def _exported(svc: ExecServices, ctx: StudioContext) -> StudioStep:
    from ce_memory.store import write_usage_events

    org_id, version_id = UUID(ctx.org_id), UUID(ctx.target_id)
    data = await _version(svc, org_id, version_id)
    creators = _creators_of(data)
    hook = None
    brief = data.spec.brief
    if brief is not None and brief.selected_hook_key:
        hook = next((h.text for h in brief.hook_candidates if h.key == brief.selected_hook_key), None)
    embedded = None
    if hook:
        try:
            embedded = await embed_texts(svc, [hook], language=str(data.spec.meta.language))
        except Exception as exc:  # an index never blocks the record
            _log.warning("hook embedding failed", error=str(exc)[:200])
    activated: list[str] = []
    async with svc.db.transaction() as session:
        await write_usage_events(
            session,
            org_id,
            video_id=data.video_id,
            version_id=version_id,
            spec=data.spec,
            creators=creators,
            event="exported",
            hook_embedding=embedded.vectors[0] if embedded else None,
            hook_embedding_model=embedded.model if embedded else None,
        )
        for creator_id in set(creators.values()):
            rows = (
                (
                    await session.execute(
                        sa.select(CreatorMemoryItem).where(
                            CreatorMemoryItem.org_id == org_id,
                            CreatorMemoryItem.creator_id == creator_id,
                            CreatorMemoryItem.status == "proposed",
                            CreatorMemoryItem.kind.in_(("persona_fact", "stance")),
                        )
                    )
                )
                .scalars()
                .all()
            )
            from ce_memory.loop import activation_targets

            targets = set(
                activation_targets(
                    [{"id": r.id, "kind": r.kind, "status": r.status, "source": r.source} for r in rows],
                    version_id=str(version_id),
                    video_id=str(data.video_id),
                )
            )
            for row in rows:
                if str(row.id) in targets and row.conflict_state != "unresolved":
                    row.status, row.last_seen_at = "active", svc.clock()
                    activated.append(str(row.id))
    return StudioStep(
        done=True, result={"trigger": "exported", "activated": activated, "hook_embedded": embedded is not None}
    )


async def _edit_applied(svc: ExecServices, ctx: StudioContext) -> StudioStep:
    memory = svc.bundle.memory
    if memory is None:
        raise StudioError("config/memory.yaml is required")
    org_id, proposal_id = UUID(ctx.org_id), UUID(ctx.target_id)
    dims = {name: event.dimension for name, event in svc.bundle.vocab.events.items()}
    async with svc.db.session() as session:
        proposal = await session.get(EditProposal, proposal_id)
        if proposal is None or proposal.org_id != org_id or proposal.status != "applied":
            return StudioStep(done=True, result={"trigger": "edit_applied", "skipped": "the proposal is not applied"})
        critique = dict(proposal.selection or {}).get("kind") == "from_critique_finding"
        version = await session.get(VideoVersion, proposal.version_id)
        cast_versions = (
            [
                UUID(str(c["creator_version_id"]))
                for c in (version.spec or {}).get("cast", [])
                if c.get("creator_version_id")
            ]
            if version
            else []
        )
        creator_of = dict(
            (
                await session.execute(
                    sa.select(CreatorVersion.id, CreatorVersion.creator_id).where(CreatorVersion.org_id == org_id)
                )
            ).all()
        )
        creators = sorted({creator_of[v] for v in cast_versions if v in creator_of}, key=str)
        if not creators:
            return StudioStep(done=True, result={"trigger": "edit_applied", "skipped": "no creator in the cast"})
        rows = (
            await session.execute(
                sa.select(EditProposal, VideoVersion.spec)
                .join(
                    VideoVersion,
                    sa.and_(VideoVersion.org_id == EditProposal.org_id, VideoVersion.id == EditProposal.version_id),
                )
                .where(EditProposal.org_id == org_id, EditProposal.status == "applied")
                .order_by(EditProposal.id.desc())
                .limit(500)
            )
        ).all()
    threshold = memory.promotion.critique_threshold if critique else memory.promotion.repeated_edit_threshold
    made: dict[str, list[UUID]] = defaultdict(list)
    for creator_id in creators:
        history = []
        for row, spec in rows:
            if (dict(row.selection or {}).get("kind") == "from_critique_finding") != critique:
                continue
            versions = {
                UUID(str(c["creator_version_id"])) for c in (spec or {}).get("cast", []) if c.get("creator_version_id")
            }
            if creator_id not in {creator_of.get(v) for v in versions}:
                continue
            history.append((str(row.id), edit_signatures(list(row.ops or []), event_dimensions=dims)))
        if not any(pid == str(proposal_id) for pid, _ in history):
            continue
        for pref in preference_proposals(history, threshold=threshold):
            if str(proposal_id) not in pref.proposal_ids:
                continue  # only groups this edit belongs to
            async with svc.db.transaction() as session:
                existing = (
                    (
                        await session.execute(
                            sa.select(CreatorMemoryItem.id).where(
                                CreatorMemoryItem.org_id == org_id,
                                CreatorMemoryItem.creator_id == creator_id,
                                CreatorMemoryItem.kind == "preference",
                                CreatorMemoryItem.status.in_(LIVE),
                                CreatorMemoryItem.source["type"].astext.in_(("user_edit", "critique")),
                                CreatorMemoryItem.value["dimension"].astext == pref.value["dimension"],
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                same_direction = []
                for item_id in existing:
                    item = await session.get(CreatorMemoryItem, item_id)
                    if (
                        item is not None
                        and (item.value or {}).get("label") == pref.value.get("label")
                        and (float((item.value or {}).get("delta", 0)) * float(pref.value["delta"]) > 0)
                    ):
                        same_direction.append(item_id)
                if same_direction:
                    continue  # already proposed (or accepted): no new conflict for a moving average
                conflicts: list[CreatorMemoryItem] = []
                new_id = await propose_item(
                    session,
                    org_id,
                    vocab=svc.bundle.vocab,
                    creator_id=creator_id,
                    kind="preference",
                    value=pref.value,
                    text=pref.text,
                    source={
                        "type": "critique" if critique else "user_edit",
                        "edit_proposal_id": str(proposal_id),
                        "edit_proposal_ids": list(pref.proposal_ids),
                        "support": pref.support,
                    },
                    confidence=min(0.9, 0.3 + 0.1 * pref.support),
                    conflicts=conflicts,
                )
            await _notify_conflicts(svc, org_id, creator_id, conflicts)
            if new_id is not None:
                made[str(creator_id)].append(new_id)
    for creator_key, ids in made.items():
        await _publish_proposed(svc, org_id, UUID(creator_key), ids, "edit_applied")
    result = {"trigger": "edit_applied", "critique": critique, "proposed": sum(len(v) for v in made.values())}
    return await _embed_step(svc, ctx, [str(c) for c in creators], result)
