# Phase 2 — Execution backbone in mock mode: progress report

**Date:** 2026-10-04 · **Status:** DoD passed · **Path taken:** Docker (Compose `core` + `mock-gpu`), 2 vCPUs, no GPU. No money was spent and no paid GPU was provisioned.

## Definition of Done

> `make e2e-mock` produces a playable MP4 with captions and music from a fixture spec in under 3 minutes on CPU; a second run is 100% cache hits; killing a worker mid-task still completes after lease expiry; route pinning holds: changing router weights does not re-route clean nodes; the production config refuses to start with `PROVENANCE_MODE=mock_dev`; scheduler and router scoring tests pass. (§40) Plus, per §37: invariant tests I5, I11 and I14.

Verified on a **fresh clone** at commit `850a0e1`: `make bootstrap`, `make build-images` (both images rebuilt from the clone), `make e2e-mock` (which runs `make dev` first: 10 services healthy, migrated, seeded), `make lint`, `make typecheck` and `CE_REQUIRE_INFRA=1 make test` (infrastructure tests may not skip) all exited 0 — **931 tests passed, none skipped**. The fresh-clone `make e2e-mock`: built in 19.1 s, 41/41 cache hits on the rerun, kill → lease expiry → same-seed retry → `ready` 39 s after the kill. (A first fresh-clone attempt at `3b878a9` exposed a race in the script's kill step; see "Known issues".)

| DoD item | Where | Result |
| --- | --- | --- |
| Fixture spec → playable MP4 with captions and music, < 3 min on CPU | `make e2e-mock` (`scripts/e2e_mock.py`, against the Compose stack); `tests/e2e/test_generate_mock.py` (in-process stack on the Compose infrastructure) | pass: ~19–23 s for 41 nodes; 1080×1920, 30 fps, H.264 + AAC, 7.7 s, −14.1 LUFS integrated; frames show word-highlighted captions, the B-roll overlay, the mock avatar and the burned "MOCK PROVENANCE — NOT FOR DISTRIBUTION" and "AI-generated" labels |
| Second run is 100 % cache hits | same | pass: 41/41 nodes `cached`, zero GPU tasks, byte-identical render (same artifact) |
| Killing a worker mid-task completes after lease expiry | `make e2e-mock` step 3 (`docker compose kill worker-cpu`, restart); `tests/e2e` (in-process worker killed) | pass: an attempt ends `lease_expired`, the reaper requeues it as `infra_retry` with the same seed, the build reaches `ready` (~41 s after the kill with `lease_s: 30`) |
| Route pinning holds when router weights change | `packages/py/ce_build/tests/test_pinning_and_dirty.py::test_route_pinning_holds_when_router_weights_change` (+ `test_flipped_weights_would_choose_the_other_engine` proves the weights matter); `tests/invariants/test_i05_determinism.py` | pass |
| Production refuses `PROVENANCE_MODE=mock_dev` | `tests/invariants/test_i11_provenance_prod.py` (config and every service entry point: API, orchestrator, render worker, scheduler, GPU worker) | pass |
| Scheduler and router scoring tests | `apps/scheduler/tests/test_scheduler.py` (placement scoring, desired workers, SKIP LOCKED leasing, completion, retries, reaper, cancellation, fleet); `packages/py/ce_router/tests/test_router.py` (hard filters, scoring, determinism, pinning, fallbacks) | pass |
| Invariants I5, I11, I14 | `tests/invariants/test_i05_determinism.py`, `test_i11_provenance_prod.py`, `test_i14_import_lint.py` | pass |
| All tests green; `make dev` works in mock mode | full suite; `make dev` brings up 10 healthy services | pass |

**Mutation checks** (each break applied, the named tests run, then reverted): disabling route pinning → pinning tests fail; giving infrastructure retries a new seed → scheduler reaper test fails; removing the route from cache keys → cache-key completeness test fails; removing both production provenance checks → I11 tests fail. Removing only one of the two provenance checks is caught by the other (they overlap by design). Removing the workflow's explicit "skip dependents of failed nodes" step changes nothing observable — dependents of a failed node never become ready and are skipped when the DAG drains — so it is an equivalent mutant, not a test gap.

## What was built

| Area | Result | State |
| --- | --- | --- |
| `ce_contracts` | Capability catalog (51 capabilities incl. interface-only), request/result models, `Adapter`/`AdapterBase`/typed interfaces, `BehaviorTranslator`, `RunContext`, behavior-matrix schema, manifest with license closure, loader with recorded refusals, wire-form `BehaviorDirectives`, adapter contract suite (ADR 0032) | implemented and tested (Python 3.10 syntax checked) |
| Plugins | 19 mock plugins producing real media (FFmpeg/Pillow), two avatar translators and a voice translator, the ASS caption renderer, storage providers and the mock GPU provider as plugins (`docs/PLUGINS.md`) | implemented and tested (contract suite over every adapter) |
| `ce_policy`, `ce_router` | License evaluation of the closure against the operator profile; hard filters, scoring, pinning, fallbacks, route digests | implemented and tested |
| `ce_build` | Every §12.1 node kind, node keys, content-only cache keys (config digests, `impl_version`, seed basis), BuildManifest records, route and seed pinning per group, dirty analysis with impact | implemented and tested |
| `ce_exec` | Plan, per-node protocol, node output documents, request builders, in-process executors, cache self-heal, bookkeeping, events (ADR 0033) | implemented and tested |
| `ce_render` | Timeline, shot audio, assembly, ducking envelope, two-pass loudnorm verified with ebur128, compose filtergraph (base + overlays, titles, ASS, labels), encode, proxy | implemented and tested |
| `ce_worker`, `apps/gpu-worker` | Register, long-poll lease, heartbeat, presigned I/O with hash checks, cancellation, error classes, translator application, model-cache skeleton | implemented and tested (`worker-cpu`; GPU family images in Phase 8) |
| `apps/scheduler` | Queue with `SKIP LOCKED` and placement scoring, worker protocol, Temporal async completion, reaper, cancellation probe, fleet manager with budget cap, cost ledger, advisory-lock leader (ADR 0034) | implemented and tested (mock provider only) |
| `apps/orchestrator`, `apps/render-worker` | `GenerateVersionWorkflow`, `SceneBuildWorkflow`, `RenderWorkflow`, `AssetValidationWorkflow`, `DeletionWorkflow`; render queue worker | implemented and tested |
| API | Jobs (list, detail with attempts, cancel), manifests, renders (list, request, detail, download with `exportable`), jobs started as workflows; `ce video generate-fixture` | implemented and tested |
| Images, Compose, Makefile | Services image (FFmpeg pinned by base-image digest; ADR 0035), healthchecks on every long-running service, `make dev`, `make dev-down`, `make build-images`, `make e2e-mock` | implemented and tested |
| Observability | Prometheus metrics on every service; HTTP client logs quieted | implemented; metrics are exposed, dashboards are Phase 14 |
| Docs | ADRs 0032–0035, ADR 0031 superseded, DECISIONS D28–D34, `docs/PLUGINS.md`, `docs/ADAPTERS.md`, ENVIRONMENT_VARIABLES, INVARIANTS, API, README, WORLDS/CREATORS status corrections | written |

## Tests run and results

Full suite on this machine before the commit: **931 tests, all passing** (`CE_REQUIRE_INFRA=1 uv run pytest`, ~5 min). `make lint` (ruff, ruff format, ESLint, Prettier), `make typecheck` (mypy strict on 319 files, tsc), `make verify-spec` (0 errors) and `make verify-config` (dev and prod, 0 errors) pass.

New or extended in Phase 2:

| Suite | Tests | What it covers |
| --- | --- | --- |
| `packages/py/ce_contracts/tests` | 8 | catalog completeness, loader refusals (license, capability, translator, features, dimensions, duplicates), filters, license closure, matrix validation, wire-form sync with `CompiledBehavior` |
| `plugins/**/tests` | 89 | adapter contract suite over every installed adapter (incl. translator conformance), storage provider contract suites (S3 on SeaweedFS, local) |
| `packages/py/ce_build/tests` | 22 | graph shape, every node kind, determinism, content-only keys, seeds, chunking, pinning, dirty analysis, manifest |
| `packages/py/ce_router/tests`, `ce_policy/tests` | 16 + 13 | router filters, scoring, pinning, fallbacks; license policy |
| `packages/py/ce_render/tests`, `ce_exec/tests`, `ce_worker/tests` | 6 + 6 + 5 | timeline, ducking, loudness, compose; output documents, WER, take selection, behavior stubs; worker I/O, errors, translator, model cache |
| `apps/scheduler/tests` | 10 | placement scoring, desired workers, registration, leasing, completion, bad uploads, fatal failures, reaper, cancellation, fleet |
| `apps/orchestrator/tests` | 8 | workflows on the Temporal dev server: order and cache short-circuit, render queue, partial failure, retries vs non-retryable, bounded parallelism, plan failure, cancellation into activities, resume on another worker after a worker dies mid-activity, replay determinism |
| `apps/api/tests` | 73 (4 new) | jobs, manifests, renders, cancellation; OpenAPI examples now cover every router module |
| `tests/invariants` | I5 (4), I11 (6), I14 (3); I12 API cases for jobs and renders | |
| `tests/e2e` | 3 | render + all-cached rerun + SSE progress, killed worker, cancellation |

## Files changed

267 files since the Phase 1 report commit (`bf9a42f`), in three commits (`41b2602`, `8244e04`, and the Phase 2 completion commit). By area:

- **New packages implemented:** `packages/py/ce_contracts`, `ce_build`, `ce_router`, `ce_policy` (license), `ce_exec`, `ce_render`, `ce_worker`, `ce_gpu`; `ce_db` (`queue.py`, `execution.py`), `ce_config` (scheduler/build/timeline/ducking/placement schemas, `WORKER_TOKEN` check), `ce_obs` (metrics, log levels), `ce_storage` (moved behind plugins), `ce_testing` (`build.py`, `stack.py`).
- **Apps:** `apps/scheduler`, `apps/orchestrator`, `apps/render-worker`, `apps/gpu-worker` (all implemented), `apps/api` (`routers/jobs.py`, `jobs.py`, `video_cli.py`, workflow starts in assets/memory).
- **Plugins:** `plugins/mock/**` (19 plugins), `plugins/captions/ass_renderer`, `plugins/providers/{storage/s3,storage/local_fs,gpu/mock}`.
- **Config:** `config/default.yaml` (scheduler, build, render timeline, ducking), `config/env/test.yaml` (short leases), `config/gpu/pools.yaml`.
- **Infra:** `infra/docker/services.Dockerfile` (new), `infra/docker/api.Dockerfile`, `infra/compose/docker-compose.yml`, `Makefile`, `scripts/e2e_mock.py`.
- **Tests:** listed above, plus `tests/phase0` updates for the new services and ADRs.
- **Docs:** `docs/adr/0032`–`0035`, `docs/DECISIONS.md`, `docs/PLUGINS.md`, `docs/ADAPTERS.md`, `docs/ENVIRONMENT_VARIABLES.md`, `docs/INVARIANTS.md`, `docs/API.md`, `docs/ENVIRONMENT.md`, `docs/WORLDS.md`, `docs/CREATORS.md`, `README.md`, `TODO.md`, `ROADMAP.md`, `CHANGELOG.md`; `packages/ts/api-client` regenerated.

## Known issues and limitations

- **Behavior is a pass-through stub.** `behavior.resolve`, `behavior.compile_*`, `behavior.keyframe_state`, `behavior.observe` and `behavior.coverage` carry the authored acting plan through and claim no coverage (`realizations: []`). The compiler, translator coverage, observation and the triad are Phase 3.
- **QC measures but does not gate.** QC nodes record seeded mock scores; the take with the best score is selected, but there are no QC retries or the decision ladder until Phase 11.
- **Camera and realism post are pass-through** (no motion, lens, grain, overscan) until Phase 7.
- **Model cache fetchers** (`hf://`, `s3://`) are not registered, and **OOM escalation** to a larger VRAM class is not implemented — Phase 8. Real GPU providers and autoscaling are Phase 9; only the mock provider exists and the mock pool does not autoscale (D29).
- **Cost ledger** records each successful attempt's busy GPU seconds × the worker's hourly price (zero for the mock worker); pool overhead (idle time, cold starts) and failed attempts are not costed until Phase 9 actuals.
- **One development image** for all execution services (ADR 0035); per-service images are Phase 14. The image includes Debian's GPL FFmpeg build — ADR 0017's distribution review is still an owner decision (D27).
- **Plates and identity workflows** (`BuildWorldPlatesWorkflow`, identity pack, voice design, Creator Test) are Phase 10 per §40; Phase 1 docs that said "mocks in Phase 2" were corrected.
- **The first fresh-clone run of `make e2e-mock` failed its kill step**: the script killed `worker-cpu` as soon as any task was leased, and short mock tasks (TTS takes well under a second) sometimes finished before `docker compose kill` landed, so no lease expired. The script now waits for an avatar render (the slowest mock task), confirms after the kill that the worker still held that task, and retries with a fresh build otherwise. The in-process e2e test was never affected (it cuts the worker's connection before cancelling it).
- **`make e2e-mock` runs its client on the host** (`uv run`), against the Compose services; it needs `make bootstrap` first.
- **Generation height** is read literally (D32): a 9:16 frame at budget 1080 is 608×1080 and is upscaled in post; real translators map heights to engine-native sizes in Phase 8.
- **CI on GitHub** has still never run (no remote).
- **The build VM is ephemeral**; the repository leaves it only as the bundle delivered with this report.

## Next phase and next actions

**Phase 3 — Behavior backbone** (§40). In order:

1. Acting-model validators (states tile the scene, transitions need triggers, masking) on the existing `ce_core` models.
2. `ce_behavior.resolve`: the CBS resolver with the §10.6 resolution order, DNA bounds, habits from a fixture memory snapshot and the world behavior digest; replace `ce_exec.behavior_stub.resolve`.
3. The behavior compiler: realization methods with the `behavior_dimensions.yaml` preference order against the routed engine's matrix, plan-time and build-time passes, editorial proposals, abstract ProsodyPlan and VisualPlan, predicted coverage; replace the compile stubs.
4. Golden translator tests for `mock_avatar_global`, `mock_avatar_segment` and `mock_voice`; unsupported items reported, never dropped.
5. Observation: `behavior.observe` with the mock observer, triad comparison and the 12 viewer-level outcomes, `config/qc/behavior.yaml` wired to take ranking and mocked QC retries, `behavior_observations` rows, `model_behavior_profiles` aggregation.
6. Invariant tests I1 (model swap) and I4 (triad), the coverage-upgrade test, the CBS no-engine-fields lint, re-route flagging of `compiler_approximation` elements.
7. Extend `make e2e-mock` to produce and check a coverage report with requested, compiled and observed entries for every CBS item of the fixture spec.

## Post-audit corrections (2026-10, after Phase 14)

- **C1/C2** — a failing bookkeeping activity (or a render plan failure) failed the workflow without `complete_build`, leaving jobs `running` forever. **C5** — local activities never heartbeated (cancellation could not reach FFmpeg). **C6** — FFmpeg failures were retried 5×.
- **W1/W2** — Temporal RPC errors counted as "activity gone": results were dropped and every task cancelled on a Temporal blip. **W3** — a worker shutdown failed the node permanently. **W4** — worker URLs expired after 15 min. **W6** — a leader that lost its lock session kept leading. **W7** — `/complete` timeout and 5xx classification. **W12** — FFmpeg not killed on cancellation.
- **P1** — one task at a time per worker serialized every model node; **P4** — dependency-free video nodes (SFX) held back every scene.
- **A1** — a generation that could not start left its version `approved` forever. **A3** — concurrent render requests raced. **D1** — `make dev` never rebuilt the service images (the stale-orchestrator `PlanRequest.sources` failure). **D6** — the services image lacked plugins that native mode has.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-2`).
