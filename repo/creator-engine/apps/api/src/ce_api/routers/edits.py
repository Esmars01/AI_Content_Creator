"""Incremental editing and versions (§28, §30 "Generation", "Edits and locks", "Versions"; §12.8).

An edit is a proposal first: `POST /v1/versions/{id}/edits` stores it as `proposing` and starts
`ProposeEditWorkflow`; the result (operations, patch, impact, coverage delta, alternatives) arrives
as `edit.proposed` (read it with `GET /v1/edits/{id}`). `:apply` creates the derived version
(`ApplyEditWorkflow`). Regenerate, take selection, lock changes and re-routes are single-operation
proposals applied automatically once they validate. Restore, branch and duplicate copy a version
forward without the Director. Versions are never mutated (I3). The API never calls an LLM or
builds a graph itself (§9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.behavior.cbs import CBSContent
from ce_core.edit.diff import DiffEntry, area_of, spec_diff
from ce_core.edit.locks import regenerate_refusals
from ce_core.edit.ops import EditOperation, operation_list, parse_operations
from ce_core.enums import JobKind, VersionOrigin, VersionState
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError
from ce_core.ids import new_id
from ce_core.spec.videospec import VideoSpec
from ce_db.models.assets import Artifact, GenerationJob
from ce_db.models.videos import EditProposal, Project, Render, Take, Video, VideoVersion
from ce_db.versions import insert_derived_version, passed_approval
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from ce_api.common import audit, begin_idempotent, finish_idempotent
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.errors import NotYetImplementedError
from ce_api.jobs import create_job, start_job
from ce_api.routers.behavior import _cbs_by_scene, _report
from ce_api.routers.planning import MediaLink, _build_arg
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["edits"])

ACTING_REGENERATE = "Regenerate the acting of the selected scenes"
"""Acting regeneration needs the Director (`requires_director`): it runs as this edit instruction."""


# ------------------------------------------------------------------------------------- bodies
class EditSelection(Body):
    """What "this", "here", "him" refer to: a time range on the timeline and/or element keys."""

    time_range_s: tuple[float, float] | None = Field(default=None, description="seconds on the timeline")
    scene_keys: list[str] | None = None
    shot_keys: list[str] | None = None
    character_keys: list[str] | None = None


class EditCreate(Body):
    """An instruction (the Director turns it into operations) or typed operations (the Advanced
    editors). Instructions are data, never control (I10)."""

    model_config = examples(
        [
            {"instruction": "make him more skeptical", "selection": {"scene_keys": ["scn_reveal"]}},
            {"operations": [{"op": "set_camera", "add_moves": [{"type": "handheld_drift", "scale": 0.6}]}]},
        ]
    )
    instruction: str = Field(default="", max_length=2000)
    selection: EditSelection | None = None
    operations: list[dict[str, Any]] | None = Field(
        default=None, max_length=50, description="EditOperation objects (§28); see the JSON schema"
    )


class EditAccepted(Out):
    edit_proposal_id: UUID
    job_id: UUID


class EditOut(Out):
    id: UUID
    version_id: UUID
    job_id: UUID | None
    instruction: str
    selection: dict[str, Any] = Field(description="`kind` (edit, regenerate, …) and the editor selection")
    status: str = Field(description="proposing | proposed | applied | rejected | superseded | failed")
    ops: list[Any]
    patch: list[Any]
    impact: dict[str, Any] = Field(
        description="graph impact (regenerate, cascade, reuse, no_visible_effect, estimate), issues, assumptions"
    )
    coverage_delta: dict[str, Any]
    alternatives: list[Any]
    result_version_id: UUID | None
    created_at: datetime


class ApplyBody(Body):
    model_config = examples([{"alternative": "editorial_only"}, {}])
    alternative: Literal["full_reperformance", "editorial_only", "lipsync_patch"] | None = None


class ApplyAccepted(Out):
    job_id: UUID
    new_version_id: UUID


class DerivedAccepted(Out):
    """A single-operation proposal that is applied once it validates (§28)."""

    edit_proposal_id: UUID
    job_id: UUID
    new_version_id: UUID = Field(description="the derived version, created when the proposal validates")
    estimate: dict[str, Any] | None = Field(
        default=None,
        description="regenerate: the planned routes' cost of the components in scope; otherwise null, and the "
        "proposal's estimate arrives with `edit.proposed`",
    )


class LockScopeIn(Body):
    scene_keys: list[str] | None = None
    character_keys: list[str] | None = None
    shot_keys: list[str] | None = None


class LockIn(Body):
    group: str
    scope: LockScopeIn = Field(default_factory=LockScopeIn)


class LocksBody(Body):
    """The complete set of locks the version should have (§12.7)."""

    model_config = examples([{"locks": [{"group": "voice", "scope": {"character_keys": ["char_alex"]}}]}])
    locks: list[LockIn] = Field(max_length=50)


class RegenerateBody(Body):
    model_config = examples([{"components": ["avatar_video"], "seed_policy": "new"}])
    components: list[str] = Field(min_length=1, max_length=12)
    seed_policy: Literal["same", "new"] = "new"
    takes: int | None = Field(default=None, ge=1, le=4)
    quality_tier: Literal["draft", "final"] | None = None
    strategy: Literal["full", "lipsync_patch"] = "full"


class RerouteBody(Body):
    model_config = examples([{"node_keys": ["avatar.render:sht_1"], "reason": "the engine was retired"}])
    node_keys: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(default="", max_length=500)
    adapter_id: str | None = None


class BranchBody(Body):
    model_config = examples([{"name": "punchier-hook"}])
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")


class DuplicateBody(Body):
    model_config = examples([{"version_id": "0192f0a0-0000-7000-8000-000000000001"}])
    version_id: UUID
    project_id: UUID | None = None


class RegenerateEstimate(Body):
    version_id: UUID
    components: list[str] = Field(min_length=1, max_length=12)
    scene_keys: list[str] | None = None
    shot_keys: list[str] | None = None
    takes: int | None = Field(default=None, ge=1, le=4)


class EstimateBody(Body):
    """One of: a version (its full generation), an edit proposal, or a regeneration."""

    model_config = examples(
        [
            {"version_id": "0192f0a0-0000-7000-8000-000000000001"},
            {
                "regenerate": {
                    "version_id": "0192f0a0-0000-7000-8000-000000000001",
                    "components": ["avatar_video"],
                    "shot_keys": ["sht_1"],
                }
            },
        ]
    )
    version_id: UUID | None = None
    edit_proposal_id: UUID | None = None
    regenerate: RegenerateEstimate | None = None


class EstimateNode(Out):
    node_key: str
    adapter_id: str
    usd: float
    seconds: float


class EstimateOut(Out):
    usd: float
    seconds: float
    basis: str = Field(description="planned_routes (the router's cost tables at plan time) or edit_proposal")
    nodes: list[EstimateNode] = Field(default_factory=list)
    pending: bool = Field(default=False, description="the proposal is still being computed")


class RemixBody(Body):
    model_config = examples([{"transform": "shorten"}])
    transform: Literal["shorten", "lengthen", "translate", "retarget_creator", "retarget_platform"]


class VersionAccepted(Out):
    video_id: UUID
    version_id: UUID
    job_id: UUID
    state: str


class ResumeAccepted(Out):
    job_id: UUID
    version_id: UUID


class DiffItem(Out):
    path: str
    kind: str
    area: str
    before: Any = None
    after: Any = None


class CompareOut(Out):
    video_id: UUID
    a: UUID
    b: UUID
    spec: list[DiffItem]
    intent: list[DiffItem]
    cbs: dict[str, list[DiffItem]] = Field(description="per scene; a scene whose CBS is unchanged is left out")
    cbs_available: dict[str, bool] = Field(description="whether each version has resolved behavior (previz ran)")
    coverage: dict[str, Any]
    renders: dict[str, list[dict[str, Any]]]


class TakeOut(Out):
    id: UUID
    shot_key: str
    take_key: str
    take_index: int
    rank: int | None
    selected: bool
    effective_seed: int | None
    behavior_signature: dict[str, Any] | None
    video: MediaLink | None


class TakesOut(Out):
    version_id: UUID
    takes: list[TakeOut]


class LockGroupOut(Out):
    group: str
    scope_fields: list[str]
    pins_routes: bool


class ComponentOut(Out):
    name: str
    requires_director: bool
    blocked_by: list[str]


class VocabularyOut(Out):
    """The closed vocabularies (§10, I13) the Advanced editors offer as dropdowns."""

    version: str
    categories: dict[str, list[str]]
    lock_groups: list[LockGroupOut]
    regenerate_components: list[ComponentOut]


# ------------------------------------------------------------------------------------- helpers
def _selection_issues(spec: VideoSpec, selection: EditSelection | None) -> list[Issue]:
    if selection is None:
        return []
    known = {
        "scene_keys": {s.key for s in spec.scenes},
        "shot_keys": {sh.key for _, sh in spec.shots()},
        "character_keys": {c.key for c in spec.cast},
    }
    issues = [
        Issue("unknown_key", f"no {field.removesuffix('_keys')} {key}", f"/selection/{field}")
        for field, keys in known.items()
        for key in getattr(selection, field) or []
        if key not in keys
    ]
    if selection.time_range_s is not None and selection.time_range_s[1] <= selection.time_range_s[0]:
        issues.append(Issue("time_range", "the time range must end after it starts", "/selection/time_range_s"))
    return issues


def _operations(raw: list[dict[str, Any]]) -> list[EditOperation]:
    try:
        return parse_operations(raw)
    except ValidationError as exc:
        issues = [
            Issue("invalid_operation", e["msg"], "/operations/" + "/".join(str(p) for p in e["loc"]))
            for e in exc.errors()[:20]
        ]
        raise InvalidInputError("invalid edit operations", issues=issues) from exc


async def _propose(
    session: DbSession,
    principal: Writer,
    version: VideoVersion,
    *,
    kind: str,
    instruction: str = "",
    operations: list[EditOperation] | None = None,
    editor: dict[str, Any] | None = None,
    auto_apply: bool = False,
) -> tuple[EditProposal, GenerationJob, UUID | None]:
    """The `proposing` row and its `edit_propose` job (started after the commit)."""
    row = EditProposal(
        org_id=principal.org_id,
        version_id=version.id,
        instruction=instruction,
        selection={"kind": kind, **({"editor": editor} if editor else {})},
        ops=operation_list.dump_python(operations, mode="json") if operations else [],
        status="proposing",
    )
    session.add(row)
    await session.flush()
    new_version = new_id() if auto_apply else None
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.EDIT_PROPOSE,
        target_type="video_version",
        target_id=version.id,
        requested_by=principal.user_id,
        video_version_id=version.id,
        input={
            "edit_proposal_id": str(row.id),
            "auto_apply": auto_apply,
            "structured": operations is not None,
            **({"new_version_id": str(new_version)} if new_version else {}),
        },
    )
    row.job_id = job.id
    await session.flush()
    return row, job, new_version


def _start(background: BackgroundTasks, services: ServicesDep, principal: Writer, job: GenerationJob) -> None:
    kind = JobKind(job.kind)
    arg = _build_arg(services, principal.org_id, job.id, job.video_version_id or job.target_id)
    background.add_task(start_job, services, principal.org_id, job.id, kind, arg)


async def _derived(
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
    version: VideoVersion,
    *,
    kind: str,
    idempotency: Any,
    operations: list[EditOperation] | None = None,
    instruction: str = "",
    editor: dict[str, Any] | None = None,
    estimate: dict[str, Any] | None = None,
) -> Any:
    row, job, new_version = await _propose(
        session,
        principal,
        version,
        kind=kind,
        instruction=instruction,
        operations=operations,
        editor=editor,
        auto_apply=True,
    )
    assert new_version is not None
    await audit(session, principal, f"edit.{kind}", "edit_proposal", row.id, request=request)
    accepted = DerivedAccepted(edit_proposal_id=row.id, job_id=job.id, new_version_id=new_version, estimate=estimate)
    await finish_idempotent(session, principal, idempotency, 202, accepted)
    _start(background, services, principal, job)
    return accepted


async def _idempotent(request: Request, principal: Writer, session: DbSession, services: ServicesDep) -> Any:
    return await begin_idempotent(
        session, principal, request, ttl_s=services.settings.idempotency_ttl_s, now=services.clock()
    )


# ------------------------------------------------------------------------------------- estimates
def _route_cost(route: dict[str, Any]) -> tuple[float, float]:
    """`est_usd` and `est_seconds` the router recorded in a planned route's reasons (§23)."""
    usd = seconds = 0.0
    for reason in route.get("reasons") or []:
        name, _, value = str(reason).partition("=")
        try:
            if name == "est_usd":
                usd = float(value)
            elif name == "est_seconds":
                seconds = float(value)
        except ValueError:
            continue
    return usd, seconds


def _estimate(routes: dict[str, Any], keep: Any) -> EstimateOut:
    nodes = []
    for key, route in sorted(routes.items()):
        if not isinstance(route, dict) or not keep(key):
            continue
        usd, seconds = _route_cost(route)
        nodes.append(EstimateNode(node_key=key, adapter_id=str(route.get("adapter_id", "")), usd=usd, seconds=seconds))
    return EstimateOut(
        usd=round(sum(n.usd for n in nodes), 6),
        seconds=round(sum(n.seconds for n in nodes), 3),
        basis="planned_routes",
        nodes=nodes,
    )


def _scope_keep(spec: dict[str, Any], kinds: set[str], scenes: list[str] | None, shots: list[str] | None) -> Any:
    shots_of = {s["key"]: {sh["key"] for sh in s["shots"]} for s in spec["scenes"]}
    allowed = set(shots or []) | {sh for sc in scenes or [] for sh in shots_of.get(sc, set())} | set(scenes or [])

    def keep(key: str) -> bool:
        kind, _, rest = key.partition(":")
        if kind not in kinds:
            return False
        return not allowed or bool(set(rest.split(":")) & allowed)

    return keep


@router.post("/v1/estimates", response_model=EstimateOut)
async def estimates(body: EstimateBody, principal: Reader, session: DbSession, services: ServicesDep) -> EstimateOut:
    """Synchronous estimates from the router's recorded costs (§30 "Estimates"); no model calls and
    no graph is built here. A proposal's estimate is the one its impact computed."""
    given = [x for x in (body.version_id, body.edit_proposal_id, body.regenerate) if x is not None]
    if len(given) != 1:
        raise InvalidInputError("give exactly one of version_id, edit_proposal_id or regenerate")
    if body.edit_proposal_id is not None:
        row = await get_scoped(session, EditProposal, principal.ctx, body.edit_proposal_id, "edit")
        estimate = dict((row.impact or {}).get("estimate") or {})
        return EstimateOut(
            usd=float(estimate.get("usd", 0.0)),
            seconds=float(estimate.get("gpu_seconds", 0.0)),
            basis="edit_proposal",
            pending=row.status == "proposing",
        )
    if body.version_id is not None:
        version = await get_scoped(session, VideoVersion, principal.ctx, body.version_id, "version")
        return _estimate(dict(version.planned_routes or {}), lambda _key: True)
    assert body.regenerate is not None
    regen = body.regenerate
    version = await get_scoped(session, VideoVersion, principal.ctx, regen.version_id, "version")
    vocab = services.vocab
    unknown = [c for c in regen.components if c not in vocab.regenerate_components]
    if unknown:
        raise InvalidInputError("unknown regenerate components", issues=[Issue("unknown_vocab", str(unknown))])
    kinds = {k for c in regen.components for k in vocab.regenerate_components[c].node_kinds}
    out = _estimate(
        dict(version.planned_routes or {}), _scope_keep(version.spec, kinds, regen.scene_keys, regen.shot_keys)
    )
    if regen.takes:  # takes beyond the planned ones cost like the first
        per_take = [n for n in out.nodes if n.node_key.endswith(":t1")]
        extra = sum(n.usd for n in per_take) * max(0, regen.takes - 1)
        out.usd = round(out.usd + extra, 6)
    return out


# ------------------------------------------------------------------------------------- vocabulary
EDITOR_CATEGORIES = (
    "emotion",
    "strategy.prosody",
    "strategy.gaze",
    "strategy.gesture",
    "strategy.posture",
    "strategy.reaction",
    "strategy.camera_awareness",
    "attention_target",
    "transition_style",
    "event_type",
    "event_purpose",
    "camera.move_type",
    "camera.transition",
    "camera.framing",
    "camera.angle",
    "intent.narrative_goal",
    "intent.audience_effect",
    "intent.persuasion_goal",
    "intent.information_goal",
    "intent.attention_goal",
    "intent.cta_goal",
    "intent.emotional_goal",
    "intent.reveal_strategy",
    "intent.performance_strategy",
    "situation_kind",
    "audience_stance",
    "element_state",
    "ambient_profile",
)


@router.get("/v1/vocabulary", response_model=VocabularyOut)
async def vocabulary(principal: Reader, services: ServicesDep) -> VocabularyOut:
    """Tokens of the vocabularies the editors use, the lock groups and the regenerate components."""
    vocab = services.vocab
    return VocabularyOut(
        version=vocab.version,
        categories={c: sorted(vocab.tokens(c)) for c in EDITOR_CATEGORIES if c in vocab.categories},
        lock_groups=[
            LockGroupOut(group=name, scope_fields=sorted(d.scope), pins_routes=d.pins_routes)
            for name, d in sorted(vocab.lock_groups.items())
        ],
        regenerate_components=[
            ComponentOut(name=name, requires_director=d.requires_director, blocked_by=list(d.blocked_by))
            for name, d in sorted(vocab.regenerate_components.items())
        ],
    )


# ------------------------------------------------------------------------------------- edits
@router.post("/v1/versions/{version_id}/edits", status_code=202, response_model=EditAccepted)
async def create_edit(
    version_id: UUID,
    body: EditCreate,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """A natural-language or structured edit (§28). The proposal arrives as `edit.proposed`; nothing
    changes until it is applied."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if not body.instruction.strip() and not body.operations:
        raise InvalidInputError("give an instruction or operations", issues=[Issue("empty_edit", "nothing to do")])
    issues = _selection_issues(VideoSpec.model_validate(version.spec), body.selection)
    if issues:
        raise InvalidInputError("the selection names unknown elements", issues=issues)
    operations = _operations(body.operations) if body.operations else None
    row, job, _ = await _propose(
        session,
        principal,
        version,
        kind="edit",
        instruction=body.instruction.strip(),
        operations=operations,
        editor=body.selection.model_dump(mode="json", exclude_none=True) if body.selection else None,
    )
    accepted = EditAccepted(edit_proposal_id=row.id, job_id=job.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    _start(background, services, principal, job)
    return accepted


@router.get("/v1/versions/{version_id}/edits", response_model=list[EditOut])
async def list_edits(version_id: UUID, principal: Reader, session: DbSession) -> list[EditOut]:
    """The version's proposals, newest first (at most 100)."""
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    rows = (
        await session.execute(
            sa.select(EditProposal)
            .where(EditProposal.org_id == principal.org_id, EditProposal.version_id == version_id)
            .order_by(EditProposal.created_at.desc(), EditProposal.id.desc())
            .limit(100)
        )
    ).scalars()
    return [EditOut.model_validate(r) for r in rows]


@router.get("/v1/edits/{edit_proposal_id}", response_model=EditOut)
async def get_edit(edit_proposal_id: UUID, principal: Reader, session: DbSession) -> EditOut:
    return EditOut.model_validate(await get_scoped(session, EditProposal, principal.ctx, edit_proposal_id, "edit"))


@router.post("/v1/edits/{edit_proposal_id}:apply", status_code=202, response_model=ApplyAccepted)
async def apply_edit(
    edit_proposal_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
    body: ApplyBody | None = None,
) -> Any:
    """Creates the derived version (`ApplyEditWorkflow`, §12.8): generation follows when the parent
    passed approval, previz otherwise. `alternative` picks one of the offered strategies."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    row = await lock_scoped(session, EditProposal, principal.ctx, edit_proposal_id, "edit")
    if row.status != "proposed":
        raise ConflictError(f"a {row.status} proposal cannot be applied")
    alternative = body.alternative if body else None
    offered = {a.get("strategy") for a in row.alternatives or []}
    if alternative and alternative != "full_reperformance" and alternative not in offered:
        raise ConflictError(f"this proposal offers no {alternative} alternative")
    new_version = new_id()
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.EDIT_APPLY,
        target_type="video_version",
        target_id=row.version_id,
        requested_by=principal.user_id,
        video_version_id=row.version_id,
        input={"edit_proposal_id": str(row.id), "new_version_id": str(new_version), "alternative": alternative},
    )
    await audit(
        session, principal, "edit.apply", "edit_proposal", row.id, request=request, after={"alternative": alternative}
    )
    accepted = ApplyAccepted(job_id=job.id, new_version_id=new_version)
    await finish_idempotent(session, principal, key, 202, accepted)
    _start(background, services, principal, job)
    return accepted


@router.post("/v1/edits/{edit_proposal_id}:reject", response_model=EditOut)
async def reject_edit(edit_proposal_id: UUID, principal: Writer, session: DbSession) -> EditOut:
    row = await lock_scoped(session, EditProposal, principal.ctx, edit_proposal_id, "edit")
    if row.status not in ("proposed", "failed"):
        raise ConflictError(f"a {row.status} proposal cannot be rejected")
    row.status = "rejected"
    await session.flush()
    return EditOut.model_validate(row)


# ------------------------------------------------------------------------------------- wrappers
_SCOPE_FIELDS = ("scene_keys", "character_keys", "shot_keys")


def _lock_ident(group: str, scope: dict[str, Any] | None) -> tuple[str, tuple[Any, ...]]:
    scope = scope or {}
    return group, tuple(tuple(scope[f]) if scope.get(f) is not None else None for f in _SCOPE_FIELDS)


@router.put("/v1/versions/{version_id}/locks", status_code=202, response_model=DerivedAccepted)
async def put_locks(
    version_id: UUID,
    body: LocksBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Sets the version's locks (§12.7) through a lock-change version. Removing a lock is the user's
    decision: only this endpoint or an operation the user wrote can do it, never an instruction."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    unknown = [
        Issue("unknown_vocab", f"{lock.group!r} is not a lock group", f"/locks/{i}/group")
        for i, lock in enumerate(body.locks)
        if lock.group not in services.vocab.lock_groups
    ]
    if unknown:
        raise InvalidInputError("unknown lock groups", issues=unknown)
    current = {_lock_ident(lock["group"], lock.get("scope")): lock for lock in version.spec.get("locks", [])}
    wanted = {_lock_ident(lock.group, lock.scope.model_dump()): lock for lock in body.locks}
    add = [
        {"group": lock.group, "scope": lock.scope.model_dump(), "set_by": "user"}
        for ident, lock in wanted.items()
        if ident not in current
    ]
    remove = [
        {"group": lock["group"], "scope": lock.get("scope")} for ident, lock in current.items() if ident not in wanted
    ]
    if not add and not remove:
        raise ConflictError("the version already has exactly these locks")
    ops = _operations([{"op": "set_lock", "add": add, "remove": remove, "reason": "locks set by the user"}])
    return await _derived(
        request, principal, session, services, background, version, kind="lock_change", operations=ops, idempotency=key
    )


async def _regenerate(
    version: VideoVersion,
    body: RegenerateBody,
    scope: dict[str, list[str]],
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
    idempotency: Any,
) -> Any:
    spec = VideoSpec.model_validate(version.spec)
    vocab = services.vocab
    unknown = [c for c in body.components if c not in vocab.regenerate_components]
    if unknown:
        issues = [Issue("unknown_vocab", f"no regenerate component {c!r}", "/components") for c in unknown]
        raise InvalidInputError("unknown regenerate components", issues=issues)
    refusals = regenerate_refusals(
        body.components,
        spec.model_dump(mode="json")["locks"],
        vocab,
        scenes=scope.get("scene_keys") or [None],
        shots=scope.get("shot_keys") or [None],
        seed_policy=body.seed_policy,
    )
    if refusals:  # synchronous: the lock is named and nothing is queued (§12.7)
        issues = [Issue("regenerate_refused", r.message, "/components", detail=r.as_dict()) for r in refusals]
        raise ConflictError(refusals[0].message, issues=issues)
    if any(vocab.regenerate_components[c].requires_director for c in body.components):
        if len(body.components) > 1:
            issues = [Issue("acting_alone", "regenerate acting on its own: it runs as a Director edit")]
            raise InvalidInputError("acting is regenerated on its own", issues=issues)
        return await _derived(
            request,
            principal,
            session,
            services,
            background,
            version,
            kind="regenerate",
            instruction=ACTING_REGENERATE,
            editor={"scene_keys": scope["scene_keys"]} if scope.get("scene_keys") else None,
            idempotency=idempotency,
        )
    estimate = _estimate(
        dict(version.planned_routes or {}),
        _scope_keep(
            version.spec,
            {k for c in body.components for k in vocab.regenerate_components[c].node_kinds},
            scope.get("scene_keys"),
            scope.get("shot_keys"),
        ),
    )
    raw: list[dict[str, Any]] = [
        {
            "op": "regenerate",
            "scope": scope,
            "components": body.components,
            "seed_policy": body.seed_policy,
            "takes": body.takes,
            "strategy": body.strategy,
        }
    ]
    if body.quality_tier and body.quality_tier != str(spec.meta.quality_tier):
        raw.append({"op": "set_meta", "quality_tier": body.quality_tier, "reason": "regenerate at this tier"})
    return await _derived(
        request,
        principal,
        session,
        services,
        background,
        version,
        kind="regenerate",
        operations=_operations(raw),
        idempotency=idempotency,
        estimate={"usd": estimate.usd, "seconds": estimate.seconds, "basis": estimate.basis},
    )


@router.post("/v1/versions/{version_id}/scenes/{scene_key}:regenerate", status_code=202, response_model=DerivedAccepted)
async def regenerate_scene(
    version_id: UUID,
    scene_key: str,
    body: RegenerateBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Regenerates components of a scene (§12.7); a component a lock blocks is refused with 409,
    naming the lock. `acting` runs as a Director edit of the scene."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    if scene_key not in {s["key"] for s in version.spec["scenes"]}:
        raise NotFoundError(f"no scene {scene_key}")
    return await _regenerate(
        version, body, {"scene_keys": [scene_key]}, request, principal, session, services, background, key
    )


@router.post("/v1/versions/{version_id}/shots/{shot_key}:regenerate", status_code=202, response_model=DerivedAccepted)
async def regenerate_shot(
    version_id: UUID,
    shot_key: str,
    body: RegenerateBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Regenerates components of one shot (a new seed, more takes or a lip-sync patch)."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    scene = next((s["key"] for s in version.spec["scenes"] for sh in s["shots"] if sh["key"] == shot_key), None)
    if scene is None:
        raise NotFoundError(f"no shot {shot_key}")
    scope = {"scene_keys": [scene], "shot_keys": [shot_key]}
    return await _regenerate(version, body, scope, request, principal, session, services, background, key)


@router.post("/v1/versions/{version_id}:reroute", status_code=202, response_model=DerivedAccepted)
async def reroute(
    version_id: UUID,
    body: RerouteBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Explicitly re-routes nodes (§12.4). When compiler approximations were planned for the old
    route, the build proposes removing them (§15.7)."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    raw = [{"op": "reroute", "node_keys": body.node_keys, "adapter_id": body.adapter_id, "reason": body.reason}]
    return await _derived(
        request,
        principal,
        session,
        services,
        background,
        version,
        kind="reroute",
        operations=_operations(raw),
        idempotency=key,
    )


@router.post(
    "/v1/versions/{version_id}/shots/{shot_key}/takes/{take_key}:select",
    status_code=202,
    response_model=DerivedAccepted,
)
async def select_take(
    version_id: UUID,
    shot_key: str,
    take_key: str,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Selects a take (§12.6) through a take-select version; every generated artifact is reused."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    raw = [{"op": "select_take", "shot_key": shot_key, "take_key": take_key}]
    return await _derived(
        request,
        principal,
        session,
        services,
        background,
        version,
        kind="take_select",
        operations=_operations(raw),
        idempotency=key,
    )


@router.get("/v1/versions/{version_id}/takes", response_model=TakesOut)
async def list_takes(version_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> TakesOut:
    """Every take of the version's generated shots with its rank, selection and video (§12.6)."""
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    takes = list(
        (
            await session.execute(
                sa.select(Take)
                .where(Take.org_id == principal.org_id, Take.version_id == version_id)
                .order_by(Take.shot_key, Take.take_index)
            )
        ).scalars()
    )
    first = {t.artifact_ids[0] for t in takes if t.artifact_ids}
    artifacts: dict[UUID, Artifact] = {}
    if first:
        found = await session.execute(
            sa.select(Artifact).where(Artifact.org_id == principal.org_id, Artifact.id.in_(first))
        )
        artifacts = {a.id: a for a in found.scalars()}
    ttl = services.config.storage.presign_ttl_s
    out: list[TakeOut] = []
    for take in takes:
        video = None
        artifact = artifacts.get(take.artifact_ids[0]) if take.artifact_ids else None
        if artifact is not None:
            signed = await services.storage.presign_get(
                services.settings.s3_bucket_artifacts, artifact.storage_key, ttl_s=ttl
            )
            video = MediaLink(url=signed.url, expires_at=signed.expires_at)
        out.append(
            TakeOut(
                id=take.id,
                shot_key=take.shot_key,
                take_key=take.take_key,
                take_index=take.take_index,
                rank=take.rank,
                selected=bool(take.selected),
                effective_seed=take.effective_seed,
                behavior_signature=take.behavior_signature,
                video=video,
            )
        )
    return TakesOut(version_id=version_id, takes=out)


# ------------------------------------------------------------------------------------- versions
@router.post("/v1/versions/{version_id}:resume", status_code=202, response_model=ResumeAccepted)
async def resume(
    version_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Re-runs failed and cancelled nodes **in place** (§12.3, §12.8): completed nodes are reused
    from the manifest; only the missing ones run."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    version = await lock_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    allowed = (VersionState.PARTIAL.value, VersionState.FAILED.value, VersionState.CANCELLED.value)
    if version.state not in allowed:
        raise ConflictError(f"only partial, failed or cancelled versions resume; this one is {version.state}")
    if not await passed_approval(session, principal.org_id, version):
        raise ConflictError("this version never started generating: replan or approve it instead")
    running = (
        await session.execute(
            sa.select(sa.func.count()).where(
                GenerationJob.org_id == principal.org_id,
                GenerationJob.video_version_id == version.id,
                GenerationJob.status.in_(("queued", "running")),
            )
        )
    ).scalar_one()
    if running:
        raise ConflictError("a job for this version is still running")
    job = await create_job(
        session,
        org_id=principal.org_id,
        kind=JobKind.GENERATE,
        target_type="video_version",
        target_id=version.id,
        requested_by=principal.user_id,
        video_version_id=version.id,
        input={"source": "resume"},
    )
    await audit(session, principal, "video_version.resume", "video_version", version.id, request=request)
    accepted = ResumeAccepted(job_id=job.id, version_id=version.id)
    await finish_idempotent(session, principal, key, 202, accepted)
    _start(background, services, principal, job)
    return accepted


async def _copy_forward(
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
    source: VideoVersion,
    *,
    origin: VersionOrigin,
    branch: str | None = None,
    video_id: UUID | None = None,
    keep_video_title: bool = False,
) -> VersionAccepted:
    version, job = await insert_derived_version(
        session,
        principal.org_id,
        source_version_id=source.id,
        new_version_id=new_id(),
        document=dict(source.spec),
        origin=origin,
        requested_by=principal.user_id,
        branch=branch,
        video_id=video_id,
        keep_video_title=keep_video_title,
    )
    await audit(
        session,
        principal,
        f"video_version.{origin.value}",
        "video_version",
        version.id,
        request=request,
        after={"source_version_id": str(source.id)},
    )
    _start(background, services, principal, job)
    return VersionAccepted(video_id=version.video_id, version_id=version.id, job_id=job.id, state=version.state)


@router.post("/v1/versions/{version_id}:branch", status_code=202, response_model=VersionAccepted)
async def branch(
    version_id: UUID,
    body: BranchBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """A copy of the version on a new branch; it becomes the current version (§12.8)."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    source = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    await lock_scoped(session, Video, principal.ctx, source.video_id, "video")  # serializes branch names
    taken = (
        await session.execute(
            sa.select(sa.func.count()).where(
                VideoVersion.org_id == principal.org_id,
                VideoVersion.video_id == source.video_id,
                VideoVersion.branch == body.name,
            )
        )
    ).scalar_one()
    if taken:
        raise ConflictError(f"the branch {body.name!r} exists")
    accepted = await _copy_forward(
        request, principal, session, services, background, source, origin=VersionOrigin.BRANCH, branch=body.name
    )
    await finish_idempotent(session, principal, key, 202, accepted)
    return accepted


@router.post("/v1/versions/{version_id}:restore", status_code=202, response_model=VersionAccepted)
async def restore(
    version_id: UUID,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Copies an older version forward as a **new** current version (§12.8; versions are never
    mutated, I3). Its build has the restored version as parent, so every node is a cache hit."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    source = await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    await lock_scoped(session, Video, principal.ctx, source.video_id, "video")
    accepted = await _copy_forward(
        request, principal, session, services, background, source, origin=VersionOrigin.RESTORE
    )
    await finish_idempotent(session, principal, key, 202, accepted)
    return accepted


@router.post("/v1/videos/{video_id}:duplicate", status_code=202, response_model=VersionAccepted)
async def duplicate(
    video_id: UUID,
    body: DuplicateBody,
    request: Request,
    principal: Writer,
    session: DbSession,
    services: ServicesDep,
    background: BackgroundTasks,
) -> Any:
    """Copies a version into a new video (§12.8). The seed namespace and the parent link travel with
    it, so the copy reuses every cached artifact."""
    key, replay = await _idempotent(request, principal, session, services)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    video = await get_scoped(session, Video, principal.ctx, video_id, "video")
    source = await get_scoped(session, VideoVersion, principal.ctx, body.version_id, "version")
    if source.video_id != video.id:
        raise NotFoundError("version not found in this video")
    project_id = body.project_id or video.project_id
    await get_scoped(session, Project, principal.ctx, project_id, "project")
    copy = Video(
        org_id=principal.org_id,
        project_id=project_id,
        title=f"{video.title} (copy)",
        mode=video.mode,
        status="active",
        budget_usd=video.budget_usd,
    )
    session.add(copy)
    await session.flush()
    accepted = await _copy_forward(
        request,
        principal,
        session,
        services,
        background,
        source,
        origin=VersionOrigin.DUPLICATE,
        video_id=copy.id,
        keep_video_title=True,
    )

    await finish_idempotent(session, principal, key, 202, accepted)
    return accepted


@router.post("/v1/versions/{version_id}:variants", status_code=501)
async def variants(version_id: UUID, principal: Writer, session: DbSession) -> Any:
    """Variants (several readings of one brief) are on the V1 roadmap."""
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    raise NotYetImplementedError("version variants", "V1")


@router.post("/v1/versions/{version_id}:remix", status_code=501)
async def remix(version_id: UUID, body: RemixBody, principal: Writer, session: DbSession) -> Any:
    """Remixes (shorten, lengthen, translate, retarget) are on the V1 roadmap."""
    await get_scoped(session, VideoVersion, principal.ctx, version_id, "version")
    raise NotYetImplementedError(f"remix ({body.transform})", "V1")


# ------------------------------------------------------------------------------------- compare
def _items(entries: list[DiffEntry], area: str | None = None) -> list[DiffItem]:
    return [
        DiffItem(path=e.path, kind=e.kind, area=area or area_of(e.path), before=e.before, after=e.after)
        for e in entries
    ]


async def _coverage(session: DbSession, services: ServicesDep, org_id: UUID, version_id: UUID) -> dict[str, Any] | None:
    try:
        report, data = await _report(session, services, org_id, version_id)
    except NotFoundError:
        return None
    return {
        "stage": str(report.stage),
        "summary": dict(data.get("summary") or report.summary_counts()),
        "entries": {f"{e.item_ref}|{e.dimension}": e.model_dump(mode="json") for e in report.entries},
    }


@router.get("/v1/videos/{video_id}/compare", response_model=CompareOut)
async def compare(
    video_id: UUID,
    a: Annotated[UUID, Query(description="the earlier version")],
    b: Annotated[UUID, Query(description="the later version")],
    principal: Reader,
    session: DbSession,
    services: ServicesDep,
) -> CompareOut:
    """Two versions of a video side by side (§12.8): structured spec and intent diffs, CBS diffs per
    scene, coverage entries that differ, and both versions' renders."""
    video = await get_scoped(session, Video, principal.ctx, video_id, "video")
    va = await get_scoped(session, VideoVersion, principal.ctx, a, "version")
    vb = await get_scoped(session, VideoVersion, principal.ctx, b, "version")
    if va.video_id != video.id or vb.video_id != video.id:
        raise NotFoundError("version not found in this video")
    spec_a, spec_b = VideoSpec.model_validate(va.spec), VideoSpec.model_validate(vb.spec)
    entries = spec_diff(spec_a.content_dict(), spec_b.content_dict())
    cbs_a: dict[str, CBSContent] = await _cbs_by_scene(session, services, principal.org_id, va.id)
    cbs_b: dict[str, CBSContent] = await _cbs_by_scene(session, services, principal.org_id, vb.id)
    cbs: dict[str, list[DiffItem]] = {}
    if cbs_a and cbs_b:
        for scene in sorted(set(cbs_a) | set(cbs_b)):
            left = cbs_a[scene].model_dump(mode="json") if scene in cbs_a else {}
            right = cbs_b[scene].model_dump(mode="json") if scene in cbs_b else {}
            diff = spec_diff(left, right)
            if diff:
                cbs[scene] = _items(diff, "cbs")
    cov_a = await _coverage(session, services, principal.org_id, va.id)
    cov_b = await _coverage(session, services, principal.org_id, vb.id)
    changed: list[dict[str, Any]] = []
    if cov_a and cov_b:
        for item in sorted(set(cov_a["entries"]) | set(cov_b["entries"])):
            before, after = cov_a["entries"].get(item), cov_b["entries"].get(item)
            if before != after:
                ref, dim = item.split("|", 1)
                changed.append({"item_ref": ref, "dimension": dim, "a": before, "b": after})
    renders: dict[str, list[dict[str, Any]]] = {}
    for label, version in (("a", va), ("b", vb)):
        rows = (
            await session.execute(
                sa.select(Render)
                .where(Render.org_id == principal.org_id, Render.version_id == version.id)
                .order_by(Render.created_at)
            )
        ).scalars()
        renders[label] = [
            {"render_id": str(r.id), "preset_id": r.preset_id, "is_proxy": r.is_proxy, "status": r.status} for r in rows
        ]
    return CompareOut(
        video_id=video.id,
        a=va.id,
        b=vb.id,
        spec=_items(entries),
        intent=_items([e for e in entries if area_of(e.path) == "intent"]),
        cbs=cbs,
        cbs_available={"a": bool(cbs_a), "b": bool(cbs_b)},
        coverage={
            "a": cov_a["summary"] if cov_a else None,
            "b": cov_b["summary"] if cov_b else None,
            "stage": {"a": cov_a["stage"] if cov_a else None, "b": cov_b["stage"] if cov_b else None},
            "changed": changed,
        },
        renders=renders,
    )
