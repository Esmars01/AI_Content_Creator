# Phase 9 — Fleet and providers: progress report

**Date:** 2026-10-05 · **Status:** DoD passed on simulated providers. **No `RUNPOD_API_KEY` and no approved spend, so no paid provider was called; no GPU, so milestone M2 is not reached.** · **Path taken:** the same container as Phase 8 (4 vCPUs, no GPU), Docker Compose `core` + `mock-gpu`, a real Docker daemon for `local_docker` (GPUs off), simulated providers for the fleet. No money was spent.

## Definition of Done

> simulated-provider tests pass: scale up and down, budget hold, provider failure fallback; if `RUNPOD_API_KEY` is present: provision → smoke task → stop, with costs recorded. Never exceed `SMOKE_SPEND_CAP_USD`, and ask before any further spend; milestone M2 is reached if a GPU is available: one golden video renders end to end on smoke-promoted routes, with its coverage report and costs recorded. (§40)

| DoD item | Where | Result |
| --- | --- | --- |
| Scale up and down (simulated) | `apps/scheduler/tests/test_fleet.py` (scale-up with enrollment and scale-down with fleet costs; static workers; timeouts; stop/restart; variants), `tests/e2e/test_fleet_mock.py` | pass — and end to end: a build's queued work provisions a host on the mock provider, the host enrolls with its one-time token, runs every GPU task of the build, and is terminated when idle with its lifetime in `fleet_costs` |
| Budget hold (simulated) | `test_budget_holds_low_priority_work_alerts_once_and_releases`, `test_a_video_over_its_budget_holds_all_its_work`, `test_a_provider_budget_stops_its_growth` | pass: low-priority work held while the projection exceeds `BUDGET_DAILY_USD`, never leased, released when the budget allows; growth held; one alert per cooldown (notification + `budget.alert`); a video cap holds all its work |
| Provider failure fallback (simulated) | `test_provider_failure_falls_back_to_the_next_class_and_provider`, `test_driver_filter_skips_an_offer_below_the_pool_minimum` | pass: no capacity → injected errors → not offered → provisioned on the third-party stub, in that order; refused provisions leave no rows; old drivers are skipped |
| `RUNPOD_API_KEY` present: provision → smoke → stop | — | **not applicable: no key, and spending needs the owner's approval.** The RunPod providers are tested against a fake whose request checks come from RunPod's published OpenAPI document; the paid path refuses without `allow_paid` before any request |
| M2 if a GPU is available | — | **not reached: no GPU** |

## What was built

| Area | Result | State |
| --- | --- | --- |
| `local_docker` provider | Docker Engine API over the unix socket; family images, NVIDIA device request, model-cache bind mount, labels; capacity per configured class | implemented; lifecycle tested on a real Docker daemon with GPUs off; **the GPU device request is untested** |
| `runpod_pod`, `runpod_serverless` providers | REST API v1 client (capacity errors → `NoCapacityError`), pod bodies and endpoint scaling, price captured from `costPerHr`, `allow_paid` and price-ceiling guards | implemented and tested against a schema-checked fake; **never called live** |
| `example_cloud` | a third-party stub package (own distribution, entry point, manifest) over an in-memory vendor API | implemented and tested; proves the provider-plugin path |
| Fleet manager (ADR 0054) | providers from rows (`config`, `credentials_ref`), enrollment token per host, provider/class/region fallback, driver filter, variants, provisioning timeouts, idle terminate/stop + restart, spend projection, holds, provider caps, alerts, fleet costs | implemented and tested (simulated providers) |
| Database | migration 0002; `ce_db.fleet` (spend report, holds, fleet costs, estimate statistics) | implemented and tested (upgrade/downgrade) |
| Metrics | cold start, model load, model fetch, lease wait, fleet spend, provisions, held tasks | implemented; exercised by the tests |
| Estimates | work units in task payloads, measured p50 seconds per unit → dispatcher `est_seconds` | implemented; statistics tested |
| Model cache | `flock` per download, merged manifest, eviction grace (shared network volumes) | implemented and tested (two caches on one root); `flock` on provider volumes [RV] |
| Enrollment | admin-issued one-time tokens for self-managed hosts; fleet tokens for provisioned hosts | implemented and tested |
| API | `/v1/gpu/pools|workers|offers`; admin providers, provision, stop, enroll, queue; scheduler internal fleet endpoints | implemented and tested (in-process scheduler) |
| Web | GPU page | implemented; logic covered by Vitest |
| Docs | ADR 0054, D103–D108, GPU_SETUP, PLUGINS, API, ENVIRONMENT_VARIABLES, INVARIANTS, ROADMAP, TODO, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `ruff check .`, `ruff format --check .`, `mypy` (672 files) | pass |
| `eslint`, `prettier --check .`, `tsc` (web, api client) | pass |
| `uv run pytest --ignore=tests/e2e` (infra up, CPU assets present) | 1676 passed, 1 skipped (owner fixture clips), 1 failed → fixed and re-run green: a Phase 8 router test assumed no RunPod plugin was installed; it now states that premise explicitly |
| `pytest tests/e2e` (Compose infrastructure) | 11 passed, 1 failed → fixed: the new fleet e2e read the cost row between the provider's terminate and the database commit; it now waits for the commit (passed twice in a row afterwards) |
| `make e2e-mock` (rebuilt images) | pass: fixture build, warm rerun all cache hits, text → previz → approve → video, edit → apply → compare → restore, worker kill recovered 58 s after the kill |
| `make test-e2e` (Playwright, Google Chrome 154) | 2 passed (4.1 min) |
| Vitest (`apps/web`) | 57 passed |
| GPU endpoints on the Compose stack | `/v1/gpu/pools` and `/v1/gpu/offers` answered through `SCHEDULER_INTERNAL_URL` (mock pool, mock offers) |

New tests: fleet on simulated providers (13, including the stopped-worker regression), GPU API with an in-process scheduler (4), RunPod providers against a schema-checked fake (6), `local_docker` (3; the lifecycle on the real daemon), example_cloud (1), shared model cache (1), the fleet end to end (1), GPU page logic (Vitest, 7). Updated: scheduler tests (registration, leasing), migrations (77 tables, downgrade to 0001), I12 table and route coverage, phase-0 ADR range.

A bug found by the end-to-end test and fixed: a worker the fleet had stopped kept a cached token, and its next lease set its row back to `idle`. The scheduler now locks the worker row at lease, refuses stopped or failed workers with 401 and drops their cached identity; completions and failures never revive them.

## What is untested, and why

- **RunPod live** — no `RUNPOD_API_KEY`, and provisioning paid GPUs needs the owner's explicit approval (§41). The placeholder prices in the RunPod manifests are not RunPod's prices [RV].
- **`local_docker` with GPUs** — no GPU; the NVIDIA device request is only checked in the container body.
- **Milestone M2** — no GPU.
- **`flock` on network volumes** — depends on each provider's filesystem [RV].

## Important decisions

ADR 0054 (the scheduler is the fleet authority); D103 (provider rows, credentials references, paid approval), D104 (video/project caps hold all their work), D105 (`fleet_costs` for idle overhead), D106 (admin provision by plugin key), D107 (pools/offers from the scheduler; `SCHEDULER_INTERNAL_URL`), D108 (queue endpoint with holds and spend).

## Known issues and limitations

- Failed attempts' GPU time is not billed to an org; it appears as the worker's idle overhead in `fleet_costs`.
- The router treats a pool as routable when its providers are installed and the pool is enabled; whether a provider row is configured is known only to the scheduler. Paid pools stay `enabled: false` until the owner enables them.
- A worker whose token is refused at lease (stopped by the fleet) exits its loop after a failed re-registration; on a provider host that instance is already being terminated.

## Final Phase 9 state and what Phase 10 inherits

The fleet can create, enroll, use, bill and stop hosts on any provider plugin; Phase 10's studio workflows (identity packs, plates, voice design, Creator Test) dispatch their model calls to the same scheduler and therefore run on fleet hosts once real engines are routable.
