# Final Audit and Fix Report — AI Creator Engine

**Date:** 2026-10-06 · **Workspace:** `AI_Content_Creator` (canonical: `bundles/`, `docs/`, `repo/creator-engine/`)
· **Base:** workspace commit `58c131e` (= Phase 14 checkpoint `64816c4` sources) · **Result:** tag `phase-14-audit`
in `bundles/creator-engine-phase14.bundle` · **Companion:** [`GPU_READINESS_REPORT.md`](GPU_READINESS_REPORT.md)

> **Follow-up (2026-10-07):** a product-level audit on top of this one, with black-box user journeys and
> artifact checks, found and fixed 47 more defects. That includes 4 P1s: an edit proposal applied to another
> video, creators whose voice or look could never change, a plan that hung forever, and designed voices that
> could not plan. See [`PRODUCT_LOGIC_AUDIT_REPORT.md`](PRODUCT_LOGIC_AUDIT_REPORT.md). Tag `phase-14-audit`
> now points at the commit that includes that work, and this report's text is unchanged below.

> Every measurement below was taken on this audit's host: 4 vCPU, 15 GiB, **no GPU**, Docker for the
> infrastructure, the services run as host processes (native mode). Nothing here was validated on a GPU, with a
> paid provider or with a hosted LLM.

## 1. Executive summary

The reconstructed system is substantial (≈127 k lines of Python, 13 k of TypeScript, 1 870 tests) and its
architecture is coherent; the happy path in mock mode worked before this audit. The audit found that much of
its **failure, restart and concurrency behaviour was wrong in ways the existing tests could not see**, and
that the workspace layout itself broke the test suite:

- **The workspace as delivered could not run its own test suite.** `pytest` aborted at collection (0 tests
  ran) because tests read documents that live only in the phase bundles; with collection errors ignored, 12
  tests failed and 2 security tests silently skipped. Fixed without duplicating documentation (B0).
- **Signal B/E root cause (live progress):** the SSE endpoint blocked on Valkey for 15 s on a client whose
  socket timeout is 5 s, so **every quiet event stream crashed after 5 s** (reproduced: `TimeoutError` at
  5.0 s). Reconnects skipped events, and the player depended on events alone, so it could wait long after
  the database said `ready`. Fixed on the server and in the browser (S1–S7).
- **Jobs that never finish:** several single-activity failures left a generation job `running` and its
  version `generating` forever, with nothing to reconcile them (C1, C2, A1).
- **GPU-path control plane:** a Temporal blip dropped GPU results or cancelled every GPU task, a deploy
  failed users' nodes, fleet workers could never lease GPU work, two scheduler leaders could run at once, and
  the Compose image lacked every GPU adapter (W1–W9, D6) — all fixed and tested on CPU stand-ins.
- **Signal A root cause (stale images):** `make dev` never rebuilt the service images, which carry a
  non-editable copy of the code (D1). Fixed.
- **Signal C/D (slow mock generation):** measured, not guessed. On this host a 30-second video builds in
  ≈110–140 s after previz; the critical path is genuine CPU render post (≈175 s of FFmpeg across the two
  render slots, saturating 4 vCPU), and model nodes were additionally serialized behind a single
  one-task-at-a-time worker (up to 7× wall time vs work time). Worker concurrency (P1) and scene/side-node
  overlap (P4) remove the queueing; the 11-minute Compose run could **not** be reproduced here (service images
  cannot be built in this sandbox) — see section 5.
- **Frontend:** the app was desktop-only (fixed 224 px sidebar, fixed grids), failed requests rendered as
  empty states ("No creators."), a Studio hook ran an endless refetch loop, and UI copy still announced
  phases that shipped long ago. Fixed (section 6).
- **Bundles:** all 14 verify and reconstruct; their inconsistencies were investigated and explained
  (section 18); the bundles were regenerated with honest, unchanged history plus audit notes, and the Phase 14
  bundle carries the corrections. **No Phase 13 exists.**
- **Validation (section 21):** from a fresh clone of the regenerated Phase 14 bundle on fresh volumes:
  1 890 Python tests pass, 0 fail (14 skip for CPU-engine assets); lint, typecheck, spec and config checks
  and 90 web tests pass; the demo flow passes end to end; 8 of 9 Playwright specs pass, and the ninth stops only at
  H.264 playback, which this Chromium cannot decode. The clean run found two more defects (W-FE3, and B2 in
  the audit's own new test); both are fixed.

**Overall status:** the mock environment is now considerably more trustworthy, and the project is ready to
*begin* real GPU testing (GPU readiness report, section 6). It is **not** GPU-validated and **not**
production-ready: see sections 19–20.

## 2. Overall system status

| Area | Before | After |
| --- | --- | --- |
| Test suite in the workspace | aborts at collection; 1 745 pass / 12 fail / 2 error when forced | clean clone: 1 890 pass / 0 fail / 14 skip (CPU assets); 90 web tests pass |
| Live progress (SSE) | stream crashes every 5 s of silence; events lost on reconnect | stable stream; resumable; UI reconciles with the API |
| Stuck jobs | single activity failure ⇒ job `running` forever | every build workflow reaches a final state |
| GPU control plane | 9 defects that would surface only with real GPU work | fixed, tested on stand-ins |
| Service images | never rebuilt by `make dev`; no GPU adapters inside | rebuilt on every start; all plugins installed |
| Frontend | desktop-only, failures shown as empty, refetch loop | responsive shell and pages, explicit error states, a11y fixes |
| Mock generation (30 s video, 4 vCPU) | previz 7–8 s, generate 108–133 s | previz 7–8 s, generate ≈109 s (CPU-bound; queueing removed) |

## 3. Architecture findings

- **Sound:** pinned, content-addressed build graph; tenant scoping by composite keys (no IDOR found in any
  router); the idempotency layer; the SSRF-guarded fetcher; deterministic workflow code (no
  time/random/I/O in workflows); lease claiming with `FOR UPDATE SKIP LOCKED` and zombie-completion rejection.
- **Weak points found:** durable state and transient delivery were mixed — the UI treated SSE as the source
  of truth, the scheduler treated "Temporal did not answer" as "the activity is gone", and the API had no path
  out of a workflow that never started. The fixes consistently make the **database the source of truth and
  every transient channel a fast path with a fallback**.
- **Single-task worker and render slots** made the mock build a serial pipeline; the render queue is the true
  CPU bottleneck (section 5).
- **Workspace layout:** the sources live in `repo/creator-engine/` without the engine's own `docs/`; tools and
  tests assumed `docs/` beside the sources, and the repository's CI (`repo/creator-engine/.github/`) is not
  where GitHub looks in this workspace (root `.github/`), so **no CI runs for the workspace** (not changed:
  documented, section 19).

## 4. Bugs found and fixed

Severity: **H** high (data loss, stuck work, security), **M** medium, **L** low. Phase = where the defect was
introduced (from `git log -S` over the history). All fixes are in `repo/creator-engine/`.

### 4.1 Workspace and test infrastructure

| ID | Sev | Symptom → root cause | Fix | Regression evidence |
| --- | --- | --- | --- | --- |
| B0 | H | `pytest` collected nothing (2 collection errors abort the run); `make verify-spec`/`make test` fail. Tests and scripts read `docs/MASTER_BUILD_PROMPT.md`, ADRs, OPERATIONS, API, MODELS… beside the sources; in the workspace those exist only in the phase bundles | `ce_testing.docs` resolves documents from `CE_DOCS_DIR`, then `repo/creator-engine/docs`, then the workspace's outer `docs/` (the spec there is `MASTER_BUILD_PROMPT_v2_private.md`, identical except erratum E1's rewording of I3). Checks whose document is bundle-only **skip with that reason** and run again with `CE_DOCS_DIR=<bundle checkout>/docs`. `gen_openapi`/`gen_models_doc` skip absent doc targets. No documentation tree was duplicated. | full suite runs; ce_core spec-example tests now run against the outer spec |
| B1 | M | `tests/phase0/test_no_secrets.py` (credential scan, `.env` ignored) skipped "not a git checkout": it tested `.git` beside the sources, which the workspace's subdirectory lacks | detect the work tree with `git rev-parse --is-inside-work-tree` | both tests run and pass in the workspace |
| B2 | L | *Introduced by this audit, found by its clean-environment run:* the new A4 regression test added a video to the seeded project and left it there, so `test_read_videos_and_versions` failed whenever it ran later in the same session (order-dependent) | the test creates a project of its own | the two files run in that order: fails before, passes after |

### 4.2 Real-time progress (SSE, Valkey) — Signals B and E

| ID | Sev | Phase | Symptom → root cause | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| S1 | H | 1/2 | Every SSE stream died after 5 s without events (the "XREAD timeout" in the E2E logs): `XREAD BLOCK 15000` on a redis-py client whose per-read socket timeout is 5 s raises `TimeoutError` (reproduced: raised at 5.0 s) | `EventBus.max_block_ms` keeps the block under the socket timeout; the stream survives Valkey timeouts/disconnects (keepalive, 1 s back-off, resume from the cursor) | `test_idle_stream_outlives_the_redis_socket_timeout` (real uvicorn+Valkey, 6.5 s idle), `test_valkey_errors_keep_the_stream_open_and_resume_from_the_cursor`, `test_blocking_read_stays_below_the_socket_timeout` |
| S2 | H | 1 | A reconnect before the first event restarted at the tail, losing what was published meanwhile (the browser sends `Last-Event-ID` only after an event with an `id`) | the `connected` frame carries the cursor as `id` | `test_fresh_connection_sends_its_cursor_as_event_id` |
| S10 | M | 1 | A malformed `Last-Event-ID` went into XREAD (error) → reconnect loop with the same header | `stream.gap` with `reason: invalid_last_event_id`, start at the tail | `test_malformed_last_event_id_reports_a_gap_and_keeps_streaming` |
| A9 | M | 1 | A stream authorized once kept delivering org events after logout, member removal or key revocation | streams end after `api.sse_max_stream_s` (900 s); the reconnect re-authenticates and resumes | `test_stream_ends_after_its_lifetime` |
| S3 | H | 5 | The player learned of renders only from SSE events: after the DB said `ready`, a missed event left it on "Rendering…" | renders are polled while a final render is expected (building, and ~60 s after the build ended); clear "No render was produced" afterwards | Playwright `create-to-play` (player ≤ 15 s after `ready`) |
| S4 | H | 5 | A closed EventSource (502 while the API restarts, a 401) was never recreated; nothing re-synced after reconnects or sleeping tabs | reconnect with capped back-off; refetch active queries on reopen and when a tab returns after > 30 s | `reconnectDelay` unit test |
| S5 | M | 5 | Every finished node invalidated ~13 queries under the version (storyboard, takes, renders… with fresh presigned images), delaying the requests the player needs | running build: refresh only the version row; terminal: everything under it | `events.test.ts` |
| S6 | M | 5 | Presigned playback URL (15 min) cached for 10 min and never refreshed; an expired URL broke playback for good | refresh one minute before `expires_at`; on a media error fetch a fresh URL once and resume at the same time | `refreshBefore` unit test |
| S7 | M | 11 | QC report, critiques and consistency were keyed outside the version and never refreshed after `ready` | keyed under the version; the studio refreshes them on state change | `events.test.ts` |
| W-FE1 | M | 11 | The critique panel invalidated `["edits", id]`, a key no query uses (the edit list never refreshed) | `keys.edits(id)` | — |
| W-FE2 | H | 10 | `useStudioJob` (8 Studio panels) depended on an inline array: once a job finished, every render invalidated again and every refetch re-rendered — **an endless refetch loop** | invalidate once per finished job (refs) | `common.test.tsx` (old code: 6 invalidations, new: 1) |
| W-FE3 | M | 10 | Approving a world version (Plates tab) refreshed the world but not the version the panel shows: it stayed "Draft" with the Approve button. The W-FE2 loop had hidden it; found by the world-studio Playwright spec on the clean-environment run | refresh the version too | `world-panels.test.tsx` (fails on the old code); world-studio E2E |

### 4.3 Orchestration and workflows

| ID | Sev | Phase | Symptom → root cause | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| C1 | H | 2 | If `fail_node`, the QC gate or `accept_outputs` failed for good, the exception escaped the DAG; the workflow failed **without `complete_build`**: job `running`, version `generating` forever | a bookkeeping failure fails only that node or shot gate (dependents skip); Generate/Previz/Render workflows catch any exception and still complete | `test_a_failing_bookkeeping_activity_still_closes_the_build` (fails on the old code with `WorkflowFailureError`) |
| C2 | H | 2 | `RenderWorkflow` did not handle a `plan_build` failure (e.g. unknown preset): render job never closed | same pattern as the other workflows | `test_a_render_whose_plan_fails_still_closes_its_job` (fails on old code) |
| C3 | M | 11 | An infrastructure failure inside the QC ladder kept the best earlier attempt, but its node row stayed `failed` in a finished version | `accept_outputs` marks the accepted node `succeeded` | existing QC-gate e2e tests |
| C5 | H | 2 | Local activities never heartbeated: cancelling a build could not reach a running FFmpeg (it wrote results after the cancel); a dead render worker was noticed after 30 min | `run_local_node` heartbeats every 10 s; 60 s heartbeat timeout | workflow cancellation tests |
| C6 | M | 2 | Deterministic FFmpeg failures were retried 5× with back-off | `FFmpegError` is non-retryable | — |
| P4 | M | 2 | A video-level node with no dependencies that no scene needs (SFX) ran **before** the scenes, holding every scene back | such nodes form `PlanResult.side` and run alongside the scenes; empty for older histories (replay-safe) | `test_side_nodes_run_alongside_the_scenes`, `test_nodes_no_scene_needs_run_alongside_the_scenes_not_before_them` |

### 4.4 Scheduler, workers, fleet

| ID | Sev | Phase | Symptom → root cause | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| W1 | H | 2 | Every gRPC error mapped to `Gone`: a result reported while Temporal was briefly down was dropped after the task row was already terminal; the node hung 2 h | `Unavailable` vs `Gone`; undeliverable results are stored on the task and redelivered by the leader (`completions` loop, `ce_gpu_completions_deferred_total`) | `test_a_temporal_outage_delays_results_instead_of_losing_them`, `test_only_permanent_rpc_errors_mean_the_activity_is_gone` |
| W2 | H | 2 | The cancellation probe cancelled **every** live task on a Temporal blip | an unavailable Temporal skips the probe cycle | `test_an_unavailable_temporal_cancels_no_task` |
| W3 | H | 2 | SIGTERM (deploy, scale-down, preemption) reported the running task `cancelled` = permanent node failure | shutdown → `retryable` (same seed, another worker) | `test_a_worker_shutdown_is_an_infrastructure_retry` |
| W4 | H | 2 | Worker inputs/upload slots presigned once at lease with the 15-min browser TTL: long tasks failed every upload | worker URLs signed for ≥ model node timeout + 10 min | — |
| W5 | H | 9 | Fleet-provisioned workers got no `WORKER_VRAM_GB`, advertised 0 GB, never leased | scheduler falls back to the instance's recorded VRAM; worker detects VRAM via `nvidia-smi` | — |
| W6 | H | 2 | A leader never checked its advisory-lock session: after a Postgres restart two replicas ran the leader loops (double provisioning) | liveness check per cycle; step down | `test_a_leader_that_lost_its_lock_connection_steps_down` (old code: two leaders) |
| W7 | M | 2 | `/complete` read timeout 90 s although the scheduler verifies outputs first; any 5xx classed `fatal` | 900 s read timeout for `/complete`; 5xx → `retryable` | `test_error_classes` |
| W8 | M | 8 | Model-weight hashing blocked the worker's event loop (and its heartbeats) | hashing, removal, size in threads | model-cache tests |
| W9 | H | 9 | The reaper failed stale fleet workers but never terminated their paid instances | the fleet tick terminates them once | `test_a_worker_the_reaper_failed_has_its_instance_terminated` |
| W12 | M | 2/7 | A cancelled task's FFmpeg kept running (mock adapters, `ce_render._run`) | kill on cancellation | — |
| P1 | H | 2 | One task at a time per worker; a single `worker-cpu`: every model node of a build waited in one serial queue | `WORKER_CONCURRENCY` lanes with a per-adapter load lock; busy/idle derived from live tasks; worker-cpu runs 2 (measured, section 5); GPU workers stay at 1 | `test_a_worker_with_concurrency_runs_tasks_side_by_side_and_loads_once` |

### 4.5 API and database

| ID | Sev | Phase | Symptom → root cause | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| A1 | H | 2 | When the generate workflow could not start (Temporal down) the job failed but the version stayed `approved`, from which **no route moves on**; the API also kept every workflow handle for the life of the process and awaited them on shutdown | `approved → failed` is now a legal transition; the stranded version fails and `:resume` works; handles are tracked only in tests | `test_a_generation_that_cannot_start_fails_the_version_so_it_can_resume` |
| A2 | H | 6 | Applying an edit twice while its job ran (another tab, a retry) created two derived versions and two builds | apply/reject refused while an apply job for that proposal is in flight (under the proposal lock) | extended apply test |
| A3 | M | 2 | Concurrent extra-render requests raced on the version's render rows (duplicates break that preset) | version lock + refuse while a render job runs | extended render test |
| A4 | M | 4/6 | Version numbers by MAX+1 without a lock: a replan and an edit apply collided on `uq_video_versions_video_id_number` | `next_version_number` locks the video row | `test_concurrent_new_versions_of_one_video_get_distinct_numbers` (without the lock: `UniqueViolationError`) |
| A6 | M | 4/12 | > 50 sources → HTTP 500 (the Director's `PlanRequest` is stricter than the API body) | `max_length=50` on the body (OpenAPI regenerated); any `PlanRequest` mismatch → 422 | planning API test |
| A7 | M | 12 | A retried export (same key) returned the stored answer with expired presigned URLs | downloads re-signed on replay | export API test |
| A8 | H | 1 | Invitation tokens (`POST /v1/invitations/{token}:accept`) were written to the request log | the log records the route template, and the raw path only where it carries no token | — |
| A13 | M | 1 | A removed member's API keys came back to life on re-invitation | removal revokes their keys | `test_removing_a_member_revokes_their_api_keys` |
| A12 | L | 4 | An unknown voice or wardrobe version in the cast was accepted (202) and failed later in the plan job | 422 with the cast path at request time | planning API test |

### 4.6 Docker, Compose, native mode — Signal A

| ID | Sev | Phase | Symptom → root cause | Fix |
| --- | --- | --- | --- | --- |
| D1 | H | 2 | Stale containers ran old code against a newer API (the `PlanRequest.sources` failure): `make dev` ran `up` without `--build`, and the images install the code non-editable | `make dev` → `up --build` (layer cache keeps an unchanged rebuild quick) |
| D6 | H | 2/8 | The services image installed 22 fewer plugins than native mode (every GPU adapter, `captions_llm`): Compose routed and accepted workers differently | all plugins installed; `test_service_image_plugins.py` fails on any omission (on the old Dockerfile it lists the 22) |
| D4 | M | 14 | `make dev-native` left `make dev` containers on the same Temporal queues and ports; the launcher reported "ready" for the container | stop the Compose app services first; refuse when a port already answers; check liveness before readiness |
| D5 | M | 14 | Native mode never rebuilt the web app (`.next` reused) | always `pnpm build` (incremental) |
| D12 | M | 0 | Postgres (dev password), Valkey (no auth), Temporal, SeaweedFS, scheduler published on all interfaces | published on `CE_BIND_ADDRESS`, default `127.0.0.1` |
| D14 | L | 7 | The API container got the host path `./.cache/models` as `MODEL_CACHE_DIR` | `/models` as in the other services |

## 5. Performance findings (measured; mock mode, 4 vCPU, native mode)

Method: `scripts`-style driver through the HTTP API (the demo brief "Create a 30-second TikTok explaining why
most people misunderstand AI agents", draft tier) plus the Temporal histories of the generate workflow and its
scene children (per-activity queue wait and run time) and `job_attempts` (wall vs worker busy time).

| Run | previz ready | approve → ready | Notes |
| --- | --- | --- | --- |
| before #1 (as shipped) | 8.1 s | 132.7 s | |
| before #2 (as shipped) | 6.9 s | 108.0 s | `post.realism` 13 × = 110.6 s run, 168 s waiting for a render slot; `avatar.render` 53.8 s wall for 8.1 s of work; `post.expression` 37.2 s wall for 0.9 s |
| worker concurrency 3 | 7.8 s | 140.3 s | **worse**: model work finished sooner, FFmpeg oversubscribed the CPU (camera 25 → 60 s) |
| worker concurrency 2 (shipped default) | 7.0 s | 108.9 s | `avatar.render` 34.7 s wall (was 55.6), `post.expression` 15.1 s (was 37.6); critical path unchanged |

Findings:

1. **The critical path is real CPU render post, not mock latency.** No sleeps or simulated latency exist in
   the mock path. `post.realism` (3-D LUT, lens correction, vignette, grain, 8 Mbit/s encode at 1080×1920)
   costs ≈ 1.6× real time per clip; `lut3d` alone is half of it (0.7 s colour conversion + interpolation per
   4 s clip). Two realism encodes saturate 4 vCPU (2 concurrent: 9.5 s vs 12.8 s serial; 3 concurrent: 14.3 s
   vs 19.2 s), so more render slots cannot help on this host; `-filter_threads` changes nothing.
2. **Model nodes were pure queueing** behind one worker running one task at a time (P1). Fixed; on this
   host it shortens model-node wall time by 40–60 % but not the build, because render post dominates.
3. **SFX blocked every scene** (P4). Fixed.
4. **Rejected change (no measured benefit):** a faster x264 preset for the post intermediates halved the
   isolated camera encode (2.27 → 1.22 s, SSIM 0.997) but changed nothing in situ (camera node time is
   dominated by its scaling/rotation filters and contention) while costing 1.8× intermediate bytes. Reverted.
5. **Not done, by decision:** cheaper realism (nearest-neighbour LUT, smaller frames) would make builds
   faster but change the product's look for every tier; that is a product decision, not a bug fix.
6. **The 11-minute Compose run and the 420 s E2E wait** could not be reproduced: the service images cannot be
   built in this sandbox (Docker Hub rate limits and `pkg-containers.githubusercontent.com` 403 for the
   pinned `uv` image). The measured mechanisms that inflate it — the 5 s SSE crash loop delaying the UI (S1–S4),
   serialized model nodes (P1), and, in Compose, possibly fewer CPUs per host — are fixed or explained; the
   Playwright spec now fails if the UI lags the backend by more than 15 s.

## 6. Frontend improvements

- **Responsive shell:** below `md` the sidebar becomes a drawer (Menu button, Escape and backdrop close it),
  header wraps and truncates, page padding adapts; a **skip-to-content** link; nav grouped as Make / Library /
  Quality / Operations so operator surfaces (Jobs, GPU, Developer) sit apart from the creative flow.
- **Responsive pages:** the studio, Create wizard, previz review, compare, dashboard, projects, creator
  detail, storyboard and Creator Studio grids collapse on small screens; wide tables scroll inside their card
  instead of widening the page; the developer viewer no longer forces 28 rem.
- **Honest states:** a shared `LoadError` (reason + Retry) on the dashboard, creators, worlds, jobs and project
  pages; creator/world detail pages distinguish "does not exist" (404) from "could not load"; danger alerts use
  `role="alert"` (errors are announced), others `role="status"`.
- **Player:** final-render polling, "Finishing the render…" / "No render was produced" states, expired-URL
  recovery (S3, S6).
- **Stale copy removed:** "arrives in Phase 9/10/11" texts on the dashboard, creators and worlds pages
  (those phases shipped); the dashboard now links to the GPU and rating pages.
- **Correctness:** live-update reconciliation (S4, S5, S7), the Studio refetch loop (W-FE2) and the stale
  world-version status it had hidden (W-FE3), the dead `edits` key (W-FE1).

## 7. Backend fixes

A1, A2, A3, A4, A6, A7, A8, A9, A12, A13 (section 4.5) and the SSE endpoint (S1, S2, S10).

## 8. Orchestration fixes

C1, C2, C3, C5, C6, P4 (section 4.3).

## 9. Worker and scheduler fixes

W1–W9, W12, P1 (section 4.4).

## 10. Temporal fixes

Completion classification and durable redelivery (W1), probe behaviour under Temporal outages (W2),
heartbeats for local activities (C5), non-retryable FFmpeg errors (C6), every workflow reaches its final
bookkeeping (C1, C2); `PlanResult.side` defaults to empty so in-flight histories replay unchanged.

## 11. Redis / SSE fixes

S1, S2, S10, A9 on the server; S3–S7 in the browser. Valkey remains a delivery channel only; the database is
the source of truth for every outcome the UI waits for.

## 12. Storage and render fixes

W4 (worker URL lifetime), W12 (FFmpeg killed on cancellation), S6 (browser URL refresh), A7 (re-signed export
downloads). The E2E spec now checks the media bytes (206 range response, `video/mp4`, `ftyp` box).

## 13. Database and migration fixes

A4 (locked version numbering), A1 (state machine: `approved → failed`). No migration was added or changed;
the fresh-database migration path is exercised by every infrastructure test run (section 21).

## 14. Security findings

Fixed: A8 (tokens in logs), A9 (streams outlive revoked sessions), A13 (API keys survive removal), D12
(unauthenticated dev infrastructure on all interfaces), B1 (credential scan was skipped). Checked and sound:
tenant isolation (composite keys, no IDOR found), role checks on mutating routes, idempotency, SSRF guard,
local-storage path handling, login rate limit. Open: billing trusts worker-reported busy time (GPU report
R7); the CSP allows inline scripts (Phase 14 carry-over).

## 15. Test improvements

New regression tests for every fix marked in section 4 (Python: API/SSE, workflows, scheduler, worker,
fleet, database concurrency, partition, events; TypeScript: events, Studio job hook, status messages), the
image-plugin guard, and a stricter Playwright create-to-play spec (backend-ready → player ≤ 15 s, final
render, media bytes, bounded `play()`, SSE reconnect budget). Several new tests were run against the old code
to prove they catch the defect (C1, C2, A4, W-FE2, W-FE3, D6, W6).

## 16. Docker and infrastructure fixes

D1, D4, D5, D6, D12, D14 (section 4.6); `CE_BIND_ADDRESS` and `WORKER_CPU_CONCURRENCY` documented in
`.env.example`.

## 17. Documentation changes

- New: this report and `GPU_READINESS_REPORT.md` (workspace `docs/`).
- Phase reports: a "Post-audit corrections (2026-10)" section on every affected phase report in
  `docs/progress/` (section 18); the historical text above it is unchanged. `phase-0.md` restored to
  `docs/progress/` from the history (it was the only phase report missing there).
- `SETUP.md`, `DEPLOYMENT.md`, `README.md`: images rebuilt on every `make dev`, `CE_BIND_ADDRESS`,
  worker concurrency, and the corrected statement of which object-store endpoint remote workers need.
- `docs/MANIFEST.txt` and `docs/SHA256SUMS.txt` regenerated for the new bundles and documents.
- In-code documentation of every behavioural change (docstrings next to each fix).

## 18. Phase-by-phase impact

| Phase | Defects introduced there and fixed now | Implementation corrected | Docs updated | Bundle |
| --- | --- | --- | --- | --- |
| 0 | B1 (secret scan skip in the workspace), D12 (ports on all interfaces) | in the final code | progress note | regenerated (tags, audit note; history unchanged) |
| 1 | S1 (SSE endpoint), S2, S10, A8, A9, A13 | in the final code | progress note | regenerated |
| 2 | C1, C2, C5, C6, P1, P4, W1, W2, W3, W4, W6, W7, W12, A1, A3, D1, D6 | in the final code | progress note | regenerated |
| 3 | none found | — | — | regenerated (tags, audit note) |
| 4 | A4 (planning store), A6 (body/PlanRequest mismatch), A12 | in the final code | progress note | regenerated |
| 5 | S3, S4, S5, S6, desktop-only layout, alert roles | in the final code | progress note | regenerated |
| 6 | A2, A4 (derived versions) | in the final code | progress note | regenerated |
| 7 | W12 (mock FFmpeg), D14 | in the final code | progress note | regenerated |
| 8 | W8, D6 (GPU adapters absent from the services image) | in the final code | progress note | regenerated |
| 9 | W5, W9 | in the final code | progress note | regenerated |
| 10 | W-FE2 (Studio refetch loop), W-FE3, stale "arrives in Phase 10" copy | in the final code | progress note | regenerated |
| 11 | C3, S7, W-FE1 | in the final code | progress note | regenerated |
| 12 | A7, A6 (the stricter `PlanRequest` limit) | in the final code | progress note | regenerated |
| 13 | **does not exist** (intentionally skipped, ADR 0050) | — | — | **none** |
| 14 | D4, D5 (native mode) | in the final code | progress note | regenerated with the corrections (tag `phase-14-audit`) |

**Why earlier bundles keep their historical commits.** Each phase bundle records what that phase shipped.
Rewriting those commits to contain later fixes would fabricate history that never existed (and would need
re-validating thirteen alternative code bases). The corrections live once, on top of the Phase 14 history, in
`bundles/creator-engine-phase14.bundle` (`refs/heads/main` and tag `phase-14-audit`); every phase bundle
carries a `refs/notes/audit` note on its phase commit naming the defects introduced there and the commit that
fixes them.

**Bundle integrity investigation (before regeneration).**
- All 14 bundles `git bundle verify` as complete histories, `fsck` clean; lineage strictly linear by ancestry.
- Bundles 0–7 had no tags (tags were introduced in the Phase 8 reconstruction, commit `7675c10`); bundles 2–7
  carried `refs/remotes/origin/{HEAD,main}` at the Phase 1 commit — phases 2–7 were built in one clone of
  the Phase 1 bundle and bundled with `--all`. Harmless for `git clone`, misleading for `--mirror`. The
  regenerated bundles carry `main`, `HEAD` and tags `phase-0` … `phase-N` instead.
- The Phase 14 tree contained a copy of `bundles/`: phases 0–7 and 12 identical to the outer bundles, but
  **its `creator-engine-phase8.bundle` is a different artifact** — the Phase 8 *WIP* snapshot (`e490262`,
  branch `phase8-wip` `f3825f6`) — under the same file name as the completed Phase 8 bundle (`6f5f230`).
  `f3825f6` exists in no outer bundle. The audit commit removes the stale in-tree copies (they stay in history)
  so the outer `bundles/` is the only bundle location, as the workspace intends.
- Two root commits (`01a50d0` spec commit, `f3c7938` archive), merged in `7675c10` — pre-existing.
- The outer `docs/progress/` lacked `phase-0.md` (and `README.md`, `PHASE_8_HANDOFF.md`); they exist in the
  bundles. The outer `phase-14.md` is the version of commit `3f2d1e4` (with the checkpoint verification),
  which no bundle contains (a bundle cannot contain the commit that adds it). The audit commit restores that
  text into the history, labelled as such; `phase-0.md` is added to the outer `docs/progress/`.
- `MANIFEST.txt`/`SHA256SUMS.txt` covered only the Phase 12 and 14 bundles; they now cover all 14.

## 19. Known remaining limitations

| Class | Item |
| --- | --- |
| D (needs external infrastructure) | Any GPU model or family image; paid GPU providers; hosted LLM; real watermark adapters (production startup refuses without them, I11); Hugging Face model downloads (CPU-engine assets: their tests skip) |
| B (fixed, environment-limited) | Compose service images could not be built here: D1, D6 are verified by configuration, a guard test and Compose `config`, not by a container run |
| B | Browser playback: Playwright's Chromium cannot decode H.264 and Google Chrome is not installable here; the E2E verifies the media bytes and stops at `play()` with an explicit message |
| C (intentionally deferred) | Keyboard shortcuts, DOM caption overlay, Assets page, variants/remixes (ROADMAP) |
| E (remaining known issues) | VRAM accounting and model unloading (GPU R3), OOM escalation vs fleet (R4), thread-based engines not cancellable (R5), one-time fleet tokens (R6), worker-reported billing (R7), `dispatch_gpu` timeout includes queue time (R8), scheduler presigns with the internal S3 endpoint (R10) |
| E | API (identified by code review, not reproduced): asset deletion can leave dangling references in arrays/JSON (A5); `:reingest` and upload `:complete` have no in-flight guard (A10); two concurrent caption translations of one version each derive a version without the other's language (A11) |
| E | Orchestration (identified by code review, not reproduced): a QC attempt-seed retry overwrites the cache entry of the attempt that may win (C4); a retried `begin_node` can add a duplicate attempt row (C9); job progress may step back under concurrent node completions (cosmetic) |
| E | Infrastructure: no build stamp shared by API and services (D2; mitigated by D1); migrations run from both host and image (D3, harmless when images are rebuilt); `--frozen` image builds vs re-locking CI (D7); healthchecks test liveness, not Temporal readiness (D10); the web CSP is fixed at image build (D11); dependency layers rebuild on any source change (D15); duplicated connection settings in `.env` (D9) |
| E | The repository CI (`repo/creator-engine/.github/workflows`) does not run for the workspace (GitHub reads the root `.github/`) |
| F (needs further investigation) | The 11-minute Compose mock generation (section 5, item 6) |

## 20. Remaining risks

- **GPU path** — see the GPU readiness report (R1–R10). None blocks the first smoke run; R3–R5 will bite
  as soon as two large models share a GPU.
- **Production** — blocked by design until production-validated watermark adapters exist (I11); billing
  integrity (R7) and the open API races (A5, A10, A11) should be fixed before real tenants.
- **Operations** — no CI for the workspace; image builds not reproduced in this audit.

## 21. Validation results

All runs on the audit host (4 vCPU, 15 GiB, no GPU; Docker for the infrastructure only; services as host
processes). The figures below are copied from the run logs, which are not committed.

### 21.1 Test suite

| Run | Tree | Result | Time |
| --- | --- | --- | --- |
| Baseline, as delivered (workspace) | `58c131e` | **aborted at collection, 0 tests ran**; forced with `--continue-on-collection-errors`: 1 745 passed, 12 failed, 2 errors, 16 skipped (11 failures + 2 errors from the docs layout, B0; 1 from the audit's own worktree lacking `pnpm install`; 2 security tests skipped "not a git checkout", B1) | 39:52 |
| After the fixes (workspace, outer `docs/`) | before `2a9bec0` | 1 821 passed, 1 failed, 80 skipped. The failure was a defect in a new SSE test (it read the response stream twice), fixed in `2a9bec0`. Skips: 66 for documents that live only in the phase bundles (the workspace layout, B0), 14 for CPU-engine assets and owner fixture clips | 39:44 |
| Clean environment, run 1 (fresh clone of the regenerated Phase 14 bundle) | `phase-14-audit` (first regeneration) | 1 889 passed, **1 failed**, 14 skipped. The failure was B2 (test isolation, introduced by this audit), fixed in `77bbc8b` | 37:28 |
| Clean environment, final (fresh clone of the bundle regenerated from the final sources, `77bbc8b`) | `phase-14-audit` | **1 890 passed, 0 failed, 14 skipped**; 1 warning (Temporal SDK: a cancel arrived for an activity that had already finished, which is benign) | 36:39 |

`CE_REQUIRE_INFRA=1` throughout, so no infrastructure test could skip silently. In a bundle checkout, `docs/` sits
beside the sources, so the document checks that skip in the workspace run there (none of the 14 remaining skips
is about documents). The bundle was regenerated once more after this section was written; that changed only
`docs/`. On a clone of that bundle the document checks (`tests/phase0`) and the bundle validation were run
again, and both passed. The 14 skips need `make fetch-cpu-assets` (Hugging Face and GitHub downloads, about
0.57 GB) or owner-supplied analyzer fixture clips.

| Check (clean clone) | Result |
| --- | --- |
| `make lint` (ruff, ruff format, ESLint, Prettier) | pass |
| `make typecheck` (mypy, tsc for the API client and the web app) | pass |
| `make verify-spec` | 0 errors, 0 warnings (43 sections, 14 phases, 14 invariants, 148 API paths) |
| `make verify-config` | dev and prod: 114 files, 0 errors, 0 warnings |
| `make test-web` (Vitest) | 18 files, 90 tests pass |
| `docker compose config` (default, core, core + mock-gpu, obs) | valid; all 9 published ports bound to `127.0.0.1` |

### 21.2 Clean-environment run

A fresh `git clone` of `bundles/creator-engine-phase14.bundle` into an empty directory, `.env` copied from
`.env.example`, `make infra-reset` (all Docker volumes deleted), then the documented steps. Not a fresh
machine: the uv and pnpm caches were warm, and the host's Node is 22, so Node 24.21.0 LTS was downloaded from
nodejs.org (SHA-256 verified) and put first on `PATH`. `make bootstrap` itself stopped at its prerequisite
check, because `uv run` puts the interpreter's directory first on `PATH` and on this host that directory
(`/usr/local/bin`) also holds Node 22. The bootstrap steps were run one by one instead (prerequisite check:
pass; `uv sync`; `pnpm install --frozen-lockfile`; pre-commit hooks). This is specific to this host, not a
project defect.

| Step | Result |
| --- | --- |
| `make dev-native` (infra up, migrations from an empty database, seed, web build, six services) | all six ready in 50 s |
| `make demo` (the whole mock flow through the HTTP API) | all 18 steps pass in 214.6 s: plan 2 s, previz 5 s, build to `ready` 131 s (first build on cold caches), final MP4 20.3 MB, edit applied (version 2), German captions, packaging; the mock export is refused (409, mock provenance) |
| Second generation, measured (`measure.py`) | previz ready 5.9 s, approve → ready **110.7 s** (workspace: 108.9 s); `post.realism` 13 runs, 108 s of FFmpeg, 195 s waiting for a render slot: the critical path of section 5, unchanged |
| Playwright, first run (9 specs) | 7 pass. `world-studio` failed: **found W-FE3** (fixed, section 4). `create-to-play` passed every step up to playback (backend `ready` → player within 15 s → final render → media bytes 206 with an `ftyp` box) and stopped at `play()`: this Chromium cannot decode H.264 (an environment limit) |
| Playwright after the W-FE3 fix | 8 pass, including `world-studio` and the 4 responsive viewports; `create-to-play` stops at the same H.264 limit |

Compose service images (`make dev`) still could not be built here (section 5, item 6); the clean run used
native mode, which runs the same code.

### 21.3 Bundles

`validate_bundles` (a script kept with the audit's working notes) ran on `bundles/` after regeneration: **all
222 checks pass**. For each of the 14 bundles it checks:
- `git bundle verify` in an empty repository (a complete history, no prerequisites);
- the bundle clones, and `git fsck --full` reports nothing;
- `HEAD` is the original phase checkpoint commit (Phase 14: `HEAD` = `phase-14-audit`, with `phase-14` at
  `64816c4` as its ancestor);
- tags `phase-0` … `phase-N` point at the original checkpoint commits;
- the commits up to the phase checkpoint are identical to those in the original bundle;
- no `phase-13` ref and no remote-tracking refs;
- the audit note is present.

For Phase 14 it also checks that the final sources equal the workspace's `repo/creator-engine/`, that the
reports are in `docs/` and that the in-tree `bundles/` is gone. There is no Phase 13 bundle. Checksums are in
`docs/SHA256SUMS.txt` (`cd docs && sha256sum -c SHA256SUMS.txt`), with descriptions in `docs/MANIFEST.txt`.

### 21.4 What is and is not validated

- **Validated (mock mode, CPU):** the whole suite; the demo flow; generation timings; the browser flows up to
  H.264 playback; responsive layout at four widths; the fixes in section 4, each with a regression test;
  reconstruction of all 14 bundles.
- **Not validated:** any GPU, GPU adapter, model download, paid provider or hosted LLM (see
  `GPU_READINESS_REPORT.md`); the Compose service images; H.264 playback in a real Google Chrome; the
  11-minute Compose generation (section 5, item 6).
