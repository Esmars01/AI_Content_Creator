# Phase 14 — Hardening, operations and documentation: progress report

**Date:** 2026-10-06 · **Status:** DoD passed in mock mode. **No GPU, no paid provider, no hosted LLM: every
measurement below is from mock engines and simulated workers on one 4-vCPU host, and is labelled so.** ·
**Path taken:** the Phase 12 container (4 vCPUs, 15 GiB, no GPU) with Docker Compose infrastructure; the service
images cannot be built here (the Debian package mirror answers 403), so the services ran as host processes through
the new native mode, with the observability profile in containers. No money was spent. Phase 13 was intentionally
skipped by the product owner (ADR 0050); nothing was built or reported under that number.

## Definition of Done

> a fresh clone reaches a mock-mode demo from the README alone. (§40) — plus, from the Phase 14 brief:
> observability profile and dashboards; a realistic scheduler load test with measured numbers only; an executed
> security review that fixes real issues; backup/restore and deployment docs; the full documentation set; a
> reproducible mock-mode demo; the checkpoint bundle verified and reconstructed; fresh-clone verification.

| DoD item | Evidence | Result |
| --- | --- | --- |
| Fresh clone → demo from the README alone | `git clone -b claude/phase12-14-work` from GitHub into an empty directory, then only the README's commands: `make bootstrap`, `make infra-up`, `make dev-native`, `make demo` | pass (after fixing `make infra-up`, which built service images — see Findings): the demo's 18 steps passed in 152 s |
| Whole test suite on the final code | the same fresh clone, `CE_REQUIRE_INFRA=1 uv run pytest`, `make lint`, `make typecheck`, `make verify-spec`, `make verify-config`, `make test-web` | lint, typecheck (762 files), spec and config verification and the web tests (82) pass. pytest: 1867 passed, 14 skipped, **1 failed** — an order-dependent failure caused by this phase (see Findings 9), fixed; the re-run on the checkpoint commit is recorded under "Checkpoint" |
| Observability profile and dashboards | `infra/observability/`, `make obs-native` next to the native services | pass: 7 scrape targets up; 15 alert rules loaded and evaluating; all 49 dashboard queries execute (14 return nothing because nothing of that kind happened: no failures, holds or provisioning); Grafana provisions the 5 dashboards and 4 datasources (all healthy); traces from the API, scheduler, orchestrator and render worker in Tempo; container logs in Loki; `promtool`, `amtool`, `otelcol validate` and `loki -verify-config` pass |
| Scheduler load test | `docs/LOAD_TEST.md`, `docs/load-test/*.json` | pass: 3000 tasks × 2 settings and 6000 tasks × 80 workers with retryable/OOM/fatal failures, crashes, zombies, corrupt uploads, duplicate completions, cancellations, budget holds and provider fallback — no correctness problem; starvation found and fixed |
| Security review | `docs/SECURITY.md` | executed: pip-audit (144 packages) and pnpm audit (366) clean; four real issues fixed (web headers, login rate-limit client address, scheduler metrics exposure, no API metrics); a route-permission test over 107 mutating routes |
| Backup and restore | `scripts/backup.py`, `docs/OPERATIONS.md#backup-and-restore` | two drills: 32 rows/6 objects and 19,578 rows/2,575 objects (1.17 GB) backed up, restored into a new database and buckets, verified; a corrupted backup and a non-empty target were refused |
| Deployment and operations docs | `docs/DEPLOYMENT.md`, `docs/OPERATIONS.md` (runbook per alert) | written; the alert → runbook links are tested |
| Documentation set | README, ARCHITECTURE, SETUP, DEVELOPMENT, TESTING, TROUBLESHOOTING, BEHAVIOR, INTENT, CREATORS, MEMORY, WORLDS, CONSISTENCY, VOCABULARIES, INVARIANTS, GPU_VALIDATION, DEPLOYMENT, OPERATIONS, OBSERVABILITY, SECURITY, POLICY_AND_COMPLIANCE, LOAD_TEST, DEMO | written or reviewed against the code; gaps between spec and code are stated in each |
| Demo | `scripts/demo.py`, `make demo`, `docs/DEMO.md` | passes end to end (four runs in this session plus the fresh clone's) |
| Checkpoint bundle | `bundles/creator-engine-phase14.bundle` | see "Checkpoint" below |

## What was built

- **Observability** (ADR 0060): API request metrics per route template and a server span per request (continuing an incoming `traceparent`); spans for build nodes; tracing configured in every service; worker task and render-time metrics recorded; `ce_db.ops_metrics` turning rows into `ce_ops_*` gauges (QC verdicts by adapter × language, retries and OOMs, coverage outcomes, NOT_MEASURABLE by dimension, world QC, consistency, memory conflicts, spend and rendered minutes); metrics only on side ports. `infra/observability/`: Prometheus (Compose and native), 15 alert rules with severities and runbook links, Alertmanager (no external receiver by default), Loki, Tempo, the OpenTelemetry collector, Grafana with five dashboards generated from `scripts/gen_dashboards.py`; the Compose `observability` profile pinned by digest.
- **Fair-share leasing**: per-organization candidates through a lateral, `SKIP LOCKED` lookup on a new partial index (migration 0005) and a live-lease share penalty; defaults `fair_share: 0.5`, `per_org_candidates: 10`.
- **Native mode** (`scripts/dev_native.py`, `make dev-native`, `make obs-native`), **backup/restore/verify** (`scripts/backup.py`, `make backup`), **load test** (`scripts/load_test_scheduler.py`, `make load-test`), **demo** (`scripts/demo.py`, `make demo`).
- **Security fixes**: web security headers with a CSP that allows the browser's presigned uploads; the gateway forwards only the nearest `X-Forwarded-For` hop and Compose's API trusts only its network; the scheduler's metrics moved off the worker API; route-permission test.
- **Bug fixes found while hardening**: the delivered true peak (re-encode at a higher bitrate, attenuate as a last resort); creator-consistency features that were never measured; Phase 12 omissions (stale MODELS.md, missing OpenAPI examples, a schema-name collision); `make infra-up` building service images; a misleading GPU example in the OpenAPI document; stale "Phase 13" docstrings.
- **Docs**: the set listed above, ADR 0060, D141–D148, CHANGELOG, ROADMAP, TODO, ENVIRONMENT.

## Tests run and results

- Targeted runs after each change (all pass): scheduler and database (173), API, gpu-worker, exec, QC, worker, observability, policy and config (321 + 1 skipped), render, exec and config (83 + 1 skipped), `tests/e2e/test_generate_mock.py` (3), web unit tests (82), Phase 0 (125), lint and typecheck (762 source files).
- New tests: `apps/api/tests/test_route_permissions.py`, `apps/api/tests/test_metrics_api.py` (route-template metrics, no public `/metrics`, server spans continuing a trace), `apps/scheduler/tests/test_ops_metrics.py` (row-derived gauges; no `/metrics` on the worker API), `apps/scheduler/tests/test_fair_share.py` (the global window starves a late organization, the defaults serve it at once), `tests/phase0/test_observability.py` (fresh dashboards, defined metrics, runbook links, matching scrape configs), `packages/py/ce_render/tests/test_true_peak.py`, `packages/py/ce_exec/tests/test_consistency_features.py`, `apps/web/src/lib/security-headers.test.ts`, the gateway's client-address test.
- Playwright on the pre-installed Chromium against the native stack: `phase12.spec.ts`, `edit-and-compare.spec.ts`, `qc-pages.spec.ts` and `world-studio.spec.ts` pass; `create-to-play.spec.ts` passes create → previz → approve → progress and fails at playback because Chromium cannot decode H.264 (the spec says so; Google Chrome could not be downloaded here).
- The fresh clone (commit `763b9f9`): `CE_REQUIRE_INFRA=1 uv run pytest` — 1867 passed, 14 skipped (CPU-engine assets), 1 failed in 27 min 3 s. The failure (`ce_obs` `test_logs_inside_a_span_carry_its_trace_ids`) came from this phase: the GPU worker's entry point now configured tracing before its startup checks, and the I11 test that runs it in production mode left a global tracer provider behind for later tests. Fixed by configuring tracing only after the startup checks (GPU worker and scheduler); the reproducing subset (1115 tests) then passed. The full re-run on the checkpoint commit is under "Checkpoint".

## Findings and fixes (in the order they were found)

1. Two Phase 12 omissions caught by the full suite (stale `docs/MODELS.md`; OpenAPI examples and a schema-name collision) — fixed before the Phase 12 checkpoint.
2. Consistency features read keys no producer writes (found by the documentation review) — fixed, with tests built from the producers' real shapes.
3. No API metrics; the scheduler's metrics on the worker API; no spans anywhere — fixed.
4. No web security headers; the login rate limit keyed by a shared or client-chosen address — fixed.
5. Starvation behind another organization's backlog (load test) — fixed by fair share; the first implementation halved throughput and was replaced by one within 7 %.
6. A delivered render's true peak at +1.0 dBTP from a −2.5 dBTP mix (the demo; AAC at 192 kbit/s) — fixed in `render.final`.
7. Tempo 3 rejected the `compactor` block; deprecated collector component names — fixed and the stack re-verified.
8. `make infra-up` built the service images (the fresh-clone verification) — fixed.
9. Configuring tracing before the startup checks leaked a global tracer provider into later tests (the fresh-clone suite) — fixed.

## What is untested, and why

- Any GPU engine, any paid GPU provider, any hosted LLM: none available; nothing claims otherwise (`docs/GPU_VALIDATION.md`).
- The Compose service images: they cannot be built here; the services were verified as host processes. The images were built and run in earlier phases (Phase 7 report).
- Video playback in a browser: Chromium lacks H.264; Google Chrome is not downloadable here.
- CPU-engine assets: Hugging Face is unreachable; their 14 tests skip with a reason (they passed in Phase 7 on a host that had the assets).
- Several scheduler replicas under load, and the worker API's HTTP layer under load.
- Production startup: impossible by design until production-validated watermark adapters exist (I11).

## Important decisions

ADR 0060; D141–D148 in `docs/DECISIONS.md`.

## Known issues and limitations

Carried in `TODO.md` (Phase 14, "Carried forward"): the trace context stops at the Temporal boundary; no likeness check against `protected_persons`; retention policies are not applied by a job; no rate limits beyond logins; the CSP allows inline scripts; `face_size_ratio` is unmeasured; Loki streams carry container ids rather than service names; no Kubernetes manifests; the load test covers one scheduler replica and simulated workers.

## Final state

Phases 0–12 and 14 complete in mock mode; Phase 13 intentionally skipped. Milestone M1 reached; M2 (the first GPU-validated video) not reached — it needs a GPU and the owner's approval to spend.

## Checkpoint

`bundles/creator-engine-phase14.bundle` on branch `claude/phase12-14-work`, added by the commit after the checkpoint. It holds `HEAD`, `refs/heads/main` and the tag `phase-14` at the Phase 14 checkpoint `64816c42ee3f590bf21423cef3202b70ae358604`, the tag `phase-12` at `755bdcb5af05470fc99da650aef0b44d95a7222e`, and the tags `phase-8` … `phase-11` and `phase-11-implementation` of the earlier checkpoints.

Verified on 2026-10-06:
- `git bundle verify`: "The bundle records a complete history".
- Reconstruction: `git clone creator-engine-phase14.bundle` into an empty directory — `HEAD` is `64816c4`, its tree is identical to the checkpoint's, the `phase-12` tag's tree is identical to `755bdcb`'s, `git fsck --full` is clean, and the embedded `bundles/creator-engine-phase12.bundle` still verifies.
- Fresh clone of the checkpoint from GitHub (`git clone -b claude/phase12-14-work`, commit `64816c4`), following only the README: `make bootstrap`, `make infra-up`, `make dev-native` and `make demo` all exit 0 (the demo's 18 steps in 179 s); `make lint`, `make typecheck` (762 source files), `make verify-spec`, `make verify-config` and `make test-web` (82 tests) exit 0; `CE_REQUIRE_INFRA=1 uv run pytest`: **1868 passed, 14 skipped, 0 failed** in 27 min 18 s. The 14 skips need the CPU-engine assets, which cannot be downloaded here (Hugging Face is unreachable).

## Post-audit corrections (2026-10, after Phase 14)

- **D4/D5** — `make dev-native` left `make dev` containers on the same queues and ports (and reported them ready), and never rebuilt the web app.
- The checkpoint verification text of commit `3f2d1e4` (the "Checkpoint" section above, present only in this workspace copy) is restored into the history by the audit commits.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-14`).
