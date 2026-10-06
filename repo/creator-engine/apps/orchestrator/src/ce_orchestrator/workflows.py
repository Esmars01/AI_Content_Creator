"""Temporal workflows (§9). Workflow code is deterministic: it only orders activities and child
workflows; every read and write happens in activities. Payloads are ids and small summaries.

- `GenerateVersionWorkflow`: plans the version (graph pinned to the parent's routes), runs the
  video-level nodes that scenes need (voice preparation), one child `SceneBuildWorkflow` per
  scene, then the video-level nodes it owns (music, SFX, captions, mix, renders, provenance, QC,
  proxy, coverage), and closes the build (`ready`, `partial`, `failed` or `cancelled`).
- `SceneBuildWorkflow`: the nodes of one scene.
- `RenderWorkflow`: extra render presets of a version, outside its frozen BuildManifest.
- `PlanVideoWorkflow`: Director stages 1–11 (one activity) → a `planned` version, then its
  `PrevizWorkflow` as a child (each with its own `generation_jobs` row).
- `PrevizWorkflow`: TTS, verification, alignment, world plates and one keyframe per talking shot,
  outside the BuildManifest, then measured timings → `previz_ready` (§13).
- `ProposeEditWorkflow`: an edit proposal (§28; the Director's `edit` stage when an instruction is
  given); an auto-applied wrapper (regenerate, take selection, locks, re-route) then builds the
  derived version as a child `GenerateVersionWorkflow` or `PrevizWorkflow`.
- `ApplyEditWorkflow`: applies a proposal → the derived version, then its generation or previz.
- `AssetValidationWorkflow`, `DeletionWorkflow`: the Phase 1 jobs (ADR 0031); a validated screen
  recording is analyzed by a child `ScreenAnalysisWorkflow` (Phase 7, §27).

A node runs as `begin_node` (cache check; a hit ends there) → `run_local_node` (CPU on queue
`orchestrator`, render on queue `render`) or `dispatch_gpu` + `finalize_model_node` (model nodes
through the scheduler). A failed node fails its dependents (`skipped`); independent branches go
on, so a partial version keeps every artifact that completed (§25).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from ce_exec.qcgate import AcceptInput, GateDecision, GateInput, gate_members
    from ce_exec.runtime import FinishResult
    from ce_exec.studio import (
        CallBegun,
        ModelCall,
        StudioCallInput,
        StudioContext,
        StudioDispatchInput,
        StudioFailInput,
        StudioRecordInput,
        StudioStageInput,
        StudioStep,
    )

    from ce_orchestrator.models import (
        AssetValidationInput,
        BeginResult,
        BuildInput,
        BuildResult,
        CalibrationInput,
        CompleteInput,
        DeletionInput,
        DispatchInput,
        EditJobInput,
        EditJobResult,
        FailInput,
        FinalizeInput,
        LocalInput,
        MemoryEnqueueInput,
        NodeInfo,
        NodeRef,
        PlanInput,
        PlanResult,
        PlanVideoInput,
        PlanVideoResult,
        PrevizCompleteInput,
        SceneInput,
        ScreenAnalysisInput,
    )

__all__ = [
    "WORKFLOWS",
    "ApplyEditWorkflow",
    "AssetValidationWorkflow",
    "CalibrationWorkflow",
    "DeletionWorkflow",
    "GenerateVersionWorkflow",
    "PlanVideoWorkflow",
    "PrevizWorkflow",
    "ProposeEditWorkflow",
    "RenderWorkflow",
    "SceneBuildWorkflow",
    "ScreenAnalysisWorkflow",
]

CPU_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[
        "ValueError",
        "LookupError",
        "KeyError",
        "TypeError",
        "AttributeError",
        "AssertionError",
        "NotImplementedError",
        "AnchorError",
        "GraphError",
        "ValidationError",
    ],
)
NO_RETRY = RetryPolicy(maximum_attempts=1)
FAILED = ("failed", "skipped", "cancelled")


def _error(exc: BaseException) -> str:
    cause = exc.__cause__ or exc
    return f"{type(cause).__name__}: {cause}"[:1000]


class _Dag:
    """Runs nodes in dependency order with bounded parallelism."""

    def __init__(
        self, build: BuildInput, graph_sha: str, manifest: bool, outputs: dict[str, str], statuses: dict[str, str]
    ) -> None:
        self.build = build
        self.graph_sha = graph_sha
        self.manifest = manifest
        self.outputs = outputs
        self.statuses = statuses

        self.held: dict[str, str] = {}  # outputs awaiting a verdict (exact-script loop, QC gate)
        self.deferred: set[str] = set()  # nodes inside a QC loop: manifest rows wait for the verdict
        self.infos: dict[str, NodeInfo] = {}

    def _ref(self, node: NodeInfo, qc_retry: int = 0, route_override: str | None = None) -> NodeRef:
        known = {**self.outputs, **self.held}
        return NodeRef(
            org_id=self.build.org_id,
            job_id=self.build.job_id,
            version_id=self.build.version_id,
            graph_sha=self.graph_sha,
            node_key=node.key,
            upstream={d: known[d] for d in node.deps},
            manifest=self.manifest,
            qc_retry=qc_retry,
            route_override=route_override,
            defer_manifest=self.manifest and node.key in self.deferred,
        )

    async def _cpu(
        self, name: str, arg: Any, result_type: Any, *, queue: str | None = None, timeout_s: float | None = None
    ) -> Any:
        return await workflow.execute_activity(
            name,
            arg,
            task_queue=queue or self.build.queues.orchestrator,
            start_to_close_timeout=timedelta(seconds=timeout_s or self.build.cpu_timeout_s),
            retry_policy=CPU_RETRY,
            result_type=result_type,
        )

    async def _node(
        self, node: NodeInfo, *, qc_retry: int = 0, hold: bool = False, route_override: str | None = None
    ) -> bool | None:
        """Runs one node; returns its verdict when it is a check (asr.verify, behavior.coverage).
        `hold` keeps the output out of `outputs` (dependents do not start) until the caller releases
        it; `route_override` runs it on a fallback adapter of its route (QC ladder)."""
        ref = self._ref(node, qc_retry, route_override)
        target = self.held if hold else self.outputs
        begin: BeginResult | None = None
        try:
            begin = await self._cpu("begin_node", ref, BeginResult)
            if begin.status == "cached":
                target[node.key] = begin.sha or ""
                self.statuses[node.key] = "cached"
                self._flag(node, begin.passed)
                return begin.passed
            if begin.status == "local":
                render = node.queue == "render"
                done: FinishResult = await self._cpu(
                    "run_local_node",
                    LocalInput(ref=ref, begin=begin),
                    FinishResult,
                    queue=self.build.queues.render if render else self.build.queues.orchestrator,
                    timeout_s=self.build.render_timeout_s if render else self.build.cpu_timeout_s,
                )
            else:
                worker = await workflow.execute_activity(
                    "dispatch_gpu",
                    DispatchInput(ref=ref, begin=begin),
                    task_queue=self.build.queues.orchestrator,
                    start_to_close_timeout=timedelta(seconds=self.build.model_timeout_s),
                    retry_policy=NO_RETRY,
                    result_type=dict,
                )
                done = await self._cpu(
                    "finalize_model_node", FinalizeInput(ref=ref, begin=begin, worker=worker), FinishResult
                )
            target[node.key] = done.sha
            self.statuses[node.key] = "succeeded"
            self._flag(node, done.passed)
            return done.passed
        except ActivityError as exc:
            if isinstance(exc.cause, asyncio.CancelledError) or type(exc.cause).__name__ == "CancelledError":
                self.statuses[node.key] = "cancelled"
                return None
            self.statuses[node.key] = "failed"
            await self._cpu(
                "fail_node",
                FailInput(ref=ref, node_id=begin.node_id if begin else None, error=_error(exc)),
                None,
            )
            return None

    async def _verified(self, tts: NodeInfo, verify: NodeInfo) -> None:
        """The exact-script loop (§21): synthesize → transcribe → normalized WER/CER. A failing
        segment is re-synthesized with an attempt seed (`qc_retry` n) up to the tier's per-node
        budget; still failing, it is flagged `needs_review` (the version ends `needs_review`).
        The segment's audio is held until the verdict, so alignment and everything after it use
        the accepted attempt."""
        attempt = 0
        seen: dict[str, list[str]] = {}
        while True:
            await self._node(tts, qc_retry=attempt, hold=True)
            if self.statuses.get(tts.key) in FAILED:
                self.statuses[verify.key] = "skipped"
                self.held.pop(tts.key, None)
                return
            passed = await self._node(verify, hold=True)
            for key in (tts.key, verify.key):
                if key in self.held:
                    seen.setdefault(key, []).append(self.held[key])
            if passed is not False or self.statuses.get(verify.key) in FAILED:
                break
            if attempt >= self.build.qc_retries_per_node:
                self.statuses[verify.key] = "needs_review"
                break
            attempt += 1
            self.held.pop(verify.key, None)
        accepted = {k: self.held[k] for k in (tts.key, verify.key) if k in self.held}
        rejected = {k: [sha for sha in seen.get(k, []) if sha != accepted.get(k)] for k in accepted}
        await self._accept(AcceptInput(**self._ids(), accepted=accepted, rejected=rejected, retries={tts.key: attempt}))
        self.outputs[tts.key] = self.held.pop(tts.key)
        if verify.key in self.held:
            self.outputs[verify.key] = self.held.pop(verify.key)

    def _flag(self, node: NodeInfo, passed: bool | None) -> None:
        """A video-level check that failed (unresolved Performance QA retries or render defects in
        the coverage report, a render outside its loudness or format targets) flags the version for
        review; the output stays and the video renders with the flag visible (§16.5, §26)."""
        if node.kind in ("behavior.coverage", "qc.render") and passed is False:
            self.statuses[node.key] = "needs_review"

    def _ids(self) -> dict[str, Any]:
        return {
            "org_id": self.build.org_id,
            "job_id": self.build.job_id,
            "version_id": self.build.version_id,
            "graph_sha": self.graph_sha,
        }

    async def _accept(self, inp: AcceptInput) -> None:
        if self.manifest:
            await self._cpu("accept_outputs", inp, dict)

    async def _shot_gate(self, shot: str, qc_keys: list[str], members: set[str]) -> None:
        """The QC gate of one shot (§26, ADR 0056): ask the gate, re-run what it names (attempt seed
        or fallback route) until a take passes or the ladder ends, then accept the best attempt and
        release the shot's QC outputs to their dependents."""

        def current(key: str) -> str | None:
            return self.held.get(key) or self.outputs.get(key)

        def release() -> None:
            for key in qc_keys:
                if key in self.held:
                    self.outputs[key] = self.held.pop(key)

        if any(self.statuses.get(k) in FAILED for k in qc_keys):  # nothing to judge: keep what completed
            done = {k: sha for k in members if (sha := current(k))}
            await self._accept(AcceptInput(**self._ids(), accepted=done))
            release()
            return
        history: list[dict[str, Any]] = []
        snapshots: list[tuple[float, dict[str, str], dict[str, int], dict[str, str]]] = []
        retries: dict[str, int] = {}
        routes: dict[str, str] = {}
        attempt = 0
        verdict = "pass"
        while True:
            decision: GateDecision = await self._cpu(
                "qc_gate",
                GateInput(
                    **self._ids(),
                    shot_key=shot,
                    qc={k: current(k) or "" for k in qc_keys},
                    history=history,
                    attempt=attempt,
                ),
                GateDecision,
            )
            state = {k: sha for k in sorted(members) if (sha := current(k))}
            snapshots.append((float(decision.score or 0.0), state, dict(retries), dict(routes)))
            history.extend(decision.notes)
            if decision.action == "pass":
                if history:
                    history.append({"step": "pass", "outcome": "passed", "take": decision.take})
                chosen = snapshots[-1]
                break
            if decision.action == "needs_review":
                history.append(
                    {
                        "step": "needs_review",
                        "outcome": "flagged",
                        "reason": decision.reason,
                        "take": decision.take,
                        "failures": decision.failures,
                    }
                )
                chosen = max(snapshots, key=lambda s: s[0])  # the best attempt (the first on ties)
                verdict = "fail"
                break
            attempt = decision.qc_retry
            failed_key = None
            for key in decision.rerun:
                info = self.infos[key]
                override = (
                    decision.route_override
                    if decision.route_override
                    and key in decision.targets
                    and info.queue != "render"
                    and info.kind in ("avatar.render", "video.broll")
                    else None
                )
                await self._node(info, qc_retry=attempt, hold=key in qc_keys, route_override=override)
                if self.statuses.get(key) in FAILED:
                    failed_key = key
                    break
                retries[key] = attempt
                if override:
                    routes[key] = override
                else:
                    routes.pop(key, None)
            history.append(
                {
                    "step": decision.step,
                    "outcome": "failed" if failed_key else "run",
                    "take": decision.take,
                    "qc_retry": attempt,
                    "adapter_id": decision.route_override,
                    "targets": decision.targets,
                    "rerun": decision.rerun,
                    "failures": decision.failures,
                    "score_before": decision.score,
                }
            )
            if failed_key is not None:  # an infrastructure failure inside the ladder: keep the best attempt
                for key in decision.rerun:
                    self.statuses[key] = "succeeded"
                history.append(
                    {
                        "step": "needs_review",
                        "outcome": "flagged",
                        "reason": f"the re-run of {failed_key} failed",
                        "take": decision.take,
                    }
                )
                chosen = max(snapshots, key=lambda s: s[0])
                verdict = "fail"
                break
        score, state, kept_retries, kept_routes = chosen
        seen: dict[str, list[str]] = {}
        for _, snap, _, _ in snapshots:
            for key, sha in snap.items():
                seen.setdefault(key, []).append(sha)
        for key in members:  # leftovers of the last re-run that was not judged
            if (sha := current(key)) is not None:
                seen.setdefault(key, []).append(sha)
        for key, sha in state.items():
            if key in qc_keys:
                self.held[key] = sha
            else:
                self.outputs[key] = sha
        await self._accept(
            AcceptInput(
                **self._ids(),
                accepted=state,
                rejected={k: sorted({s for s in v if s != state.get(k)}) for k, v in seen.items()},
                retries={k: v for k, v in kept_retries.items() if k in state},
                routes={k: v for k, v in kept_routes.items() if k in state},
                shot_key=shot,
                verdict="fail" if verdict == "fail" else "pass",
                report={"ladder": history, "best_score": score, "attempts": len(snapshots)},
            )
        )
        if verdict == "fail":
            best_take = next((h.get("take") for h in reversed(history) if h.get("take")), None)
            flagged = next((k for k in qc_keys if k.endswith(f":t{best_take}")), qc_keys[0])
            self.statuses[flagged] = "needs_review"
        release()

    async def run(self, nodes: list[NodeInfo]) -> None:
        keys = {n.key for n in nodes}
        self.infos.update({n.key: n for n in nodes})
        members = gate_members((n.key, n.kind, n.deps, n.shot_key) for n in nodes)
        qc_by_shot = {
            shot: sorted(k for k in keys_ if self.infos[k].kind == "qc.shot") for shot, keys_ in members.items()
        }
        qc_hold = {k for v in qc_by_shot.values() for k in v}
        gated: set[str] = set()
        verify_for = {
            n.deps[0]: n
            for n in nodes
            if n.kind == "asr.verify"
            and len(n.deps) == 1
            and n.deps[0].startswith("tts.segment:")
            and n.deps[0] in keys
        }
        paired = {v.key for v in verify_for.values()}
        self.deferred |= {k for v in members.values() for k in v} | paired | set(verify_for)
        pending = {n.key: n for n in nodes if n.key not in paired}
        running: dict[str, asyncio.Task[None]] = {}
        while pending or running:
            for key, node in list(pending.items()):
                if any(self.statuses.get(d) in FAILED for d in node.deps):
                    self.statuses[key] = "skipped"
                    del pending[key]
            for shot, qc_keys in qc_by_shot.items():  # every take judged: the shot's QC gate
                if shot not in gated and all(k in self.statuses and k not in running for k in qc_keys):
                    gated.add(shot)
                    running[f"gate:{shot}"] = asyncio.create_task(self._shot_gate(shot, qc_keys, members[shot]))
            ready = [n for n in pending.values() if all(d in self.outputs for d in n.deps)]
            for node in ready[: max(0, self.build.max_parallel - len(running))]:
                check = verify_for.get(node.key)
                work = self._verified(node, check) if check is not None else self._node(node, hold=node.key in qc_hold)
                running[node.key] = asyncio.create_task(work)  # type: ignore[arg-type]
                del pending[node.key]
            if not running:
                for key in pending:  # dependencies that never completed
                    self.statuses[key] = "skipped"
                    if key in verify_for:
                        self.statuses[verify_for[key].key] = "skipped"
                return
            try:
                done, _ = await workflow.wait(list(running.values()), return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                for task in running.values():  # propagate the cancellation to the running activities
                    task.cancel()
                await workflow.wait(list(running.values()))
                raise
            for key in [k for k, t in running.items() if t in done]:
                task = running.pop(key)
                task.result()


async def _complete(
    build: BuildInput, statuses: dict[str, str], *, cancelled: bool, render_only: bool
) -> dict[str, Any]:
    result: dict[str, Any] = await workflow.execute_activity(
        "complete_build",
        CompleteInput(
            org_id=build.org_id,
            job_id=build.job_id,
            version_id=build.version_id,
            statuses=statuses,
            cancelled=cancelled,
            render_only=render_only,
        ),
        task_queue=build.queues.orchestrator,
        start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
        retry_policy=CPU_RETRY,
        result_type=dict,
    )
    return result


async def _after_build(build: BuildInput, state: str) -> None:
    """Jobs a finished build starts on its own (§20): the consistency report, as an abandoned child
    so the build result does not wait for it."""
    ctx: dict[str, Any] = await workflow.execute_activity(
        "enqueue_consistency",
        CompleteInput(org_id=build.org_id, job_id=build.job_id, version_id=build.version_id, statuses={"state": state}),
        task_queue=build.queues.orchestrator,
        start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
        retry_policy=CPU_RETRY,
        result_type=dict,
    )
    if ctx:
        await workflow.start_child_workflow(
            ConsistencyWorkflow.run,
            StudioContext.model_validate(ctx),
            id=f"consistency-{ctx['job_id']}",
            task_queue=build.queues.orchestrator,
            parent_close_policy=ParentClosePolicy.ABANDON,
        )
    if workflow.patched("memory-update-after-build"):  # Phase 12: the memory loop's `ready` path (§18.4)
        await _start_memory_update(
            build,
            MemoryEnqueueInput(
                org_id=build.org_id,
                trigger="ready",
                target_type="video_version",
                target_id=build.version_id,
                version_id=build.version_id,
                state=state,
            ),
        )


async def _start_memory_update(build: BuildInput, inp: MemoryEnqueueInput) -> None:
    """Writes the `memory_update` job and starts `MemoryUpdateWorkflow` as an abandoned child: the
    caller's result never waits for memory."""
    ctx: dict[str, Any] = await workflow.execute_activity(
        "enqueue_memory_update",
        inp,
        task_queue=build.queues.orchestrator,
        start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
        retry_policy=CPU_RETRY,
        result_type=dict,
    )
    if ctx:
        await workflow.start_child_workflow(
            MemoryUpdateWorkflow.run,
            StudioContext.model_validate(ctx),
            id=f"memory_update-{ctx['job_id']}",
            task_queue=build.queues.orchestrator,
            parent_close_policy=ParentClosePolicy.ABANDON,
        )


@workflow.defn
class SceneBuildWorkflow:
    @workflow.run
    async def run(self, inp: SceneInput) -> BuildResult:
        outputs = dict(inp.upstream)
        statuses: dict[str, str] = {}
        await _Dag(inp.build, inp.graph_sha, inp.manifest, outputs, statuses).run(inp.nodes)
        mine = {n.key for n in inp.nodes}
        return BuildResult(
            state="done",
            job_status="done",
            outputs={k: v for k, v in outputs.items() if k in mine},
            statuses=statuses,
        )


@workflow.defn
class GenerateVersionWorkflow:
    @workflow.run
    async def run(self, build: BuildInput) -> BuildResult:
        outputs: dict[str, str] = {}
        statuses: dict[str, str] = {}
        cancelled = False
        try:
            plan: PlanResult = await workflow.execute_activity(
                "plan_build",
                PlanInput(org_id=build.org_id, job_id=build.job_id, version_id=build.version_id),
                task_queue=build.queues.orchestrator,
                start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
                retry_policy=CPU_RETRY,
                result_type=PlanResult,
            )
        except ActivityError as exc:
            await _complete(build, {"plan": "failed"}, cancelled=False, render_only=False)
            return BuildResult(state="failed", job_status="failed", failed=["plan"], statuses={"plan": _error(exc)})
        build = build.model_copy(update={"qc_retries_per_node": plan.qc_retries_per_node})
        infos = {n.key: n for n in plan.nodes}
        dag = _Dag(build, plan.graph_sha, plan.manifest, outputs, statuses)
        try:
            await dag.run([infos[k] for k in plan.pre])
            children = []
            for scene_key, keys in plan.scenes.items():
                nodes = [infos[k] for k in keys]
                own = set(keys)
                upstream = {d: outputs[d] for n in nodes for d in n.deps if d not in own and d in outputs}
                children.append(
                    workflow.execute_child_workflow(
                        SceneBuildWorkflow.run,
                        SceneInput(
                            build=build,
                            graph_sha=plan.graph_sha,
                            scene_key=scene_key,
                            nodes=nodes,
                            upstream=upstream,
                            manifest=plan.manifest,
                        ),
                        id=f"{workflow.info().workflow_id}:scene:{scene_key}",
                    )
                )
            results = await asyncio.gather(*children, return_exceptions=True)
            for (_, keys), result in zip(plan.scenes.items(), results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    if isinstance(result, ChildWorkflowError) and isinstance(result.cause, asyncio.CancelledError):
                        raise asyncio.CancelledError()
                    for key in keys:
                        statuses.setdefault(key, "failed")
                    continue
                outputs.update(result.outputs)
                statuses.update(result.statuses)
            await dag.run([infos[k] for k in plan.post])
        except asyncio.CancelledError:
            cancelled = True
        summary = await _complete(build, statuses, cancelled=cancelled, render_only=False)
        if cancelled:
            raise asyncio.CancelledError("build cancelled")
        await _after_build(build, str(summary["state"]))
        return BuildResult(
            state=summary["state"],
            job_status=summary["job_status"],
            outputs=outputs,
            statuses=statuses,
            failed=summary["failed"],
        )


@workflow.defn
class RenderWorkflow:
    """Extra presets after `ready` (§9): upstream nodes hit the cache; new render nodes run."""

    @workflow.run
    async def run(self, build: BuildInput) -> BuildResult:
        plan: PlanResult = await workflow.execute_activity(
            "plan_build",
            PlanInput(
                org_id=build.org_id, job_id=build.job_id, version_id=build.version_id, preset_ids=build.preset_ids
            ),
            task_queue=build.queues.orchestrator,
            start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
            retry_policy=CPU_RETRY,
            result_type=PlanResult,
        )
        outputs: dict[str, str] = {}
        statuses: dict[str, str] = {}
        cancelled = False
        try:
            await _Dag(
                build.model_copy(update={"qc_retries_per_node": plan.qc_retries_per_node}),
                plan.graph_sha,
                plan.manifest,
                outputs,
                statuses,
            ).run(plan.nodes)
        except asyncio.CancelledError:
            cancelled = True
        summary = await _complete(build, statuses, cancelled=cancelled, render_only=True)
        if cancelled:
            raise asyncio.CancelledError("render cancelled")
        return BuildResult(
            state=summary["state"],
            job_status=summary["job_status"],
            outputs=outputs,
            statuses=statuses,
            failed=summary["failed"],
        )


@workflow.defn
class PrevizWorkflow:
    """Previz (§13): the subset of nodes approval depends on, cached by content so generation after
    approval reuses them; nothing is written to the BuildManifest."""

    @workflow.run
    async def run(self, build: BuildInput) -> BuildResult:
        outputs: dict[str, str] = {}
        statuses: dict[str, str] = {}
        cancelled = False
        try:
            plan: PlanResult = await workflow.execute_activity(
                "plan_build",
                PlanInput(org_id=build.org_id, job_id=build.job_id, version_id=build.version_id, previz=True),
                task_queue=build.queues.orchestrator,
                start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
                retry_policy=CPU_RETRY,
                result_type=PlanResult,
            )
        except ActivityError as exc:
            statuses = {"plan": "failed"}
            await self._complete(build, statuses, outputs, cancelled=False)
            return BuildResult(state="failed", job_status="failed", failed=["plan"], statuses={"plan": _error(exc)})
        try:
            await _Dag(
                build.model_copy(update={"qc_retries_per_node": plan.qc_retries_per_node}),
                plan.graph_sha,
                plan.manifest,
                outputs,
                statuses,
            ).run(plan.nodes)
        except asyncio.CancelledError:
            cancelled = True
        summary = await self._complete(build, statuses, outputs, cancelled=cancelled)
        if cancelled:
            raise asyncio.CancelledError("previz cancelled")
        return BuildResult(
            state=summary["state"],
            job_status="succeeded" if summary["state"] == "previz_ready" else "failed",
            outputs=outputs,
            statuses=statuses,
            failed=summary["failed"],
        )

    @staticmethod
    async def _complete(
        build: BuildInput, statuses: dict[str, str], outputs: dict[str, str], *, cancelled: bool
    ) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            "complete_previz",
            PrevizCompleteInput(
                org_id=build.org_id,
                job_id=build.job_id,
                version_id=build.version_id,
                statuses=statuses,
                outputs={k: v for k, v in outputs.items() if k.startswith("align.segment:")},
                cancelled=cancelled,
            ),
            task_queue=build.queues.orchestrator,
            start_to_close_timeout=timedelta(seconds=build.cpu_timeout_s),
            retry_policy=CPU_RETRY,
            result_type=dict,
        )
        return result


@workflow.defn
class PlanVideoWorkflow:
    """Director stages 1–11 in one activity (LLM calls, research, validation) → a `planned`
    version; then previz as a child workflow with its own job."""

    @workflow.run
    async def run(self, build: BuildInput) -> dict[str, Any]:
        planned: PlanVideoResult = await workflow.execute_activity(
            "plan_video",
            PlanVideoInput(org_id=build.org_id, job_id=build.job_id),
            task_queue=build.queues.orchestrator,
            start_to_close_timeout=timedelta(seconds=build.plan_timeout_s),
            retry_policy=CPU_RETRY,
            result_type=PlanVideoResult,
        )
        if planned.status != "succeeded" or not planned.previz_job_id or not planned.version_id:
            return {"plan": planned.model_dump(), "previz": None}
        previz = build.model_copy(update={"job_id": planned.previz_job_id, "version_id": planned.version_id})
        result: BuildResult = await workflow.execute_child_workflow(
            PrevizWorkflow.run, previz, id=f"previz-{planned.previz_job_id}"
        )
        return {"plan": planned.model_dump(), "previz": {"state": result.state, "failed": result.failed}}


async def _follow_derived(build: BuildInput, result: EditJobResult) -> dict[str, Any] | None:
    """Builds a derived version (§12.8): generation when its parent passed approval, else previz."""
    if not result.next_job_id or not result.version_id:
        return None
    child = build.model_copy(update={"job_id": result.next_job_id, "version_id": result.version_id})
    if result.next_kind == "previz":
        done: BuildResult = await workflow.execute_child_workflow(
            PrevizWorkflow.run, child, id=f"previz-{result.next_job_id}"
        )
    else:
        done = await workflow.execute_child_workflow(
            GenerateVersionWorkflow.run, child, id=f"generate-{result.next_job_id}"
        )
    return {"kind": result.next_kind, "state": done.state, "failed": done.failed}


@workflow.defn
class ProposeEditWorkflow:
    """§28: resolve → operations → patch → validation → impact → coverage delta → alternatives →
    `edit.proposed`; auto-applied wrappers continue with the derived version's build."""

    @workflow.run
    async def run(self, build: BuildInput) -> dict[str, Any]:
        result: EditJobResult = await workflow.execute_activity(
            "propose_edit",
            EditJobInput(org_id=build.org_id, job_id=build.job_id),
            task_queue=build.queues.orchestrator,
            start_to_close_timeout=timedelta(seconds=build.plan_timeout_s),
            retry_policy=CPU_RETRY,
            result_type=EditJobResult,
        )
        return {"proposal": result.model_dump(), "build": await _follow_derived(build, result)}


@workflow.defn
class ApplyEditWorkflow:
    @workflow.run
    async def run(self, build: BuildInput) -> dict[str, Any]:
        result: EditJobResult = await workflow.execute_activity(
            "apply_edit",
            EditJobInput(org_id=build.org_id, job_id=build.job_id),
            task_queue=build.queues.orchestrator,
            start_to_close_timeout=timedelta(seconds=build.plan_timeout_s),
            retry_policy=CPU_RETRY,
            result_type=EditJobResult,
        )
        if result.edit_proposal_id and result.status == "succeeded" and workflow.patched("memory-update-after-edit"):
            await _start_memory_update(  # repeated edits → a proposed preference (§18.4)
                build,
                MemoryEnqueueInput(
                    org_id=build.org_id,
                    trigger="edit_applied",
                    target_type="edit_proposal",
                    target_id=result.edit_proposal_id,
                    version_id=result.version_id,
                ),
            )
        return {"apply": result.model_dump(), "build": await _follow_derived(build, result)}


@workflow.defn
class AssetValidationWorkflow:
    """Validation (size, magic bytes, probe); a validated screen recording is then analyzed by a
    child `ScreenAnalysisWorkflow` with its own job (§27)."""

    @workflow.run
    async def run(self, inp: AssetValidationInput) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            "validate_asset",
            inp,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=CPU_RETRY,
            result_type=dict,
        )
        analysis_job = result.get("screen_analysis_job_id")
        if analysis_job:
            child = ScreenAnalysisInput(org_id=inp.org_id, job_id=str(analysis_job), asset_id=inp.asset_id)
            result["screen_analysis"] = await workflow.execute_child_workflow(
                ScreenAnalysisWorkflow.run, child, id=f"screen_analysis-{analysis_job}"
            )
        return result


@workflow.defn
class ScreenAnalysisWorkflow:
    """§27: scene changes → keyframes → OCR → text and pixel diffs → VLM summary → a
    `screen_analysis` artifact (one activity; `ce_exec.screen`)."""

    @workflow.run
    async def run(self, inp: ScreenAnalysisInput) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            "analyze_screen",
            inp,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=CPU_RETRY,
            result_type=dict,
        )
        return result


@workflow.defn
class CalibrationWorkflow:
    """§15.7: knob → observed-effect curves for one model, stored in `model_behavior_profiles`;
    the router overlay marks monotonic knobs calibrated (`ce_exec.calibration`). One activity: the
    sweep renders run where the adapter runs (CPU here; GPU families on their hosts, Phase 9 fleet)."""

    @workflow.run
    async def run(self, inp: CalibrationInput) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            "calibrate_model",
            inp,
            start_to_close_timeout=timedelta(hours=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
            result_type=dict,
        )
        return result


@workflow.defn
class DeletionWorkflow:
    @workflow.run
    async def run(self, inp: DeletionInput) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            "delete_target",
            inp,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=CPU_RETRY,
            result_type=dict,
        )
        return result


# ---------------------------------------------------------------------- studio jobs (Phase 10)
class StudioLoop:
    """The loop every studio workflow runs (`ce_exec.studio`): stage → model calls in parallel →
    next stage, until a stage is done. A child `GenerateVersionWorkflow` runs when a stage asks for
    a build (the Creator Test). Any failure fails the job with the reason recorded."""

    async def _call(self, ctx: StudioContext, call: ModelCall) -> dict[str, Any]:
        begun: CallBegun = await workflow.execute_activity(
            "studio_begin",
            StudioCallInput(ctx=ctx, call=call),
            start_to_close_timeout=timedelta(seconds=ctx.cpu_timeout_s),
            retry_policy=NO_RETRY,
            result_type=CallBegun,
        )
        if begun.done:
            return begun.output
        try:
            worker = await workflow.execute_activity(
                "studio_dispatch",
                StudioDispatchInput(ctx=ctx, begun=begun),
                start_to_close_timeout=timedelta(seconds=ctx.model_timeout_s),
                retry_policy=NO_RETRY,
                result_type=dict,
            )
            result: dict[str, Any] = await workflow.execute_activity(
                "studio_record",
                StudioRecordInput(ctx=ctx, begun=begun, worker=worker),
                start_to_close_timeout=timedelta(seconds=ctx.cpu_timeout_s),
                retry_policy=CPU_RETRY,
                result_type=dict,
            )
            return result
        except ActivityError as exc:
            await workflow.execute_activity(
                "studio_fail",
                StudioFailInput(ctx=ctx, error=_error(exc), node_id=begun.node_id),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=CPU_RETRY,
            )
            raise

    async def loop(self, ctx: StudioContext) -> dict[str, Any]:
        data: dict[str, Any] = {}
        name = "start"
        try:
            while True:
                step: StudioStep = await workflow.execute_activity(
                    "studio_stage",
                    StudioStageInput(ctx=ctx, stage=name, data=data),
                    start_to_close_timeout=timedelta(seconds=ctx.cpu_timeout_s),
                    retry_policy=NO_RETRY,
                    result_type=StudioStep,
                )
                if step.done:
                    return {"status": "succeeded", **step.result}
                built: dict[str, Any] | None = None
                if step.build is not None:
                    build = BuildInput.model_validate(step.build)
                    child: BuildResult = await workflow.execute_child_workflow(
                        GenerateVersionWorkflow.run, build, id=f"generate-{build.version_id}"
                    )
                    built = child.model_dump(mode="json")
                outputs = list(await asyncio.gather(*(self._call(ctx, c) for c in step.calls)))
                data = {**step.data, "outputs": outputs, **({"build": built} if built is not None else {})}
                name = step.next
        except (ActivityError, ChildWorkflowError) as exc:
            error = _error(exc)
            await workflow.execute_activity(
                "studio_fail",
                StudioFailInput(ctx=ctx, error=error),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=CPU_RETRY,
            )
            return {"status": "failed", "error": error}


@workflow.defn
class BuildIdentityPackWorkflow(StudioLoop):
    """`identity_pack`: face candidates, or expansions + identity scores + the VLM age check (§17.2)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class BuildWardrobeWorkflow(StudioLoop):
    """`wardrobe_refs`: wardrobe references conditioned on the canonical face, scored (§17.2)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class VoiceDesignWorkflow(StudioLoop):
    """`voice_design`: voice candidates from a description (§21)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class VoiceTestWorkflow(StudioLoop):
    """`voice_test`: the test bench — synthesis, WER, WPM, speech quality, speaker similarity (§21)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class BuildWorldPlatesWorkflow(StudioLoop):
    """`world_plates`: plate candidates, or fingerprints of the chosen plates (§19.2)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class CreatorTestWorkflow(StudioLoop):
    """`creator_test`: the fixed test video, then the scorecard and baselines (§17.4)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class ConsistencyWorkflow(StudioLoop):
    """`consistency`: a built version's creators against their baselines (§20, ADR 0056)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class CritiqueWorkflow(StudioLoop):
    """`critique`: the Creative Director's critique of a rendered version (§26)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class BenchmarkWorkflow(StudioLoop):
    """`benchmark`: a sandbox engine on the golden evaluation set vs the production default (§24)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class MemoryUpdateWorkflow(StudioLoop):
    """`memory_update`: Creator Memory write paths after approval, `ready`, export, edits and
    critiques, and embeddings of new items (§18.4, Phase 12)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class PackagingWorkflow(StudioLoop):
    """`package`: Director stage 12 — per-platform packaging and thumbnails (§13, Phase 12)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class ResearchIngestWorkflow(StudioLoop):
    """`research_ingest`: a persistent research source → text → facts → embeddings (§9, Phase 12)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


@workflow.defn
class AutonomousSuggestWorkflow(StudioLoop):
    """`autonomous_suggest`: V1 stub behind `features.autonomous_suggest_enabled` (off): it refuses
    to run; suggestions would still need the user's approval (§26)."""

    @workflow.run
    async def run(self, ctx: StudioContext) -> dict[str, Any]:
        return await self.loop(ctx)


WORKFLOWS = [
    GenerateVersionWorkflow,
    SceneBuildWorkflow,
    RenderWorkflow,
    PlanVideoWorkflow,
    PrevizWorkflow,
    ProposeEditWorkflow,
    ApplyEditWorkflow,
    AssetValidationWorkflow,
    ScreenAnalysisWorkflow,
    CalibrationWorkflow,
    DeletionWorkflow,
    BuildIdentityPackWorkflow,
    BuildWardrobeWorkflow,
    VoiceDesignWorkflow,
    VoiceTestWorkflow,
    BuildWorldPlatesWorkflow,
    CreatorTestWorkflow,
    ConsistencyWorkflow,
    CritiqueWorkflow,
    BenchmarkWorkflow,
    AutonomousSuggestWorkflow,
    MemoryUpdateWorkflow,
    PackagingWorkflow,
    ResearchIngestWorkflow,
]
