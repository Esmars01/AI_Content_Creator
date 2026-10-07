# Phase 11 — QC gate, Performance QA, continuity, Creative Director, benchmarks: progress report

**Date:** 2026-10-05 · **Status:** DoD passed in mock mode. **No GPU was available: no real engine was gated, judged, benchmarked or calibrated; every result in this phase comes from mock engines (and the real CPU analyzers where they apply) and is labelled so.** · **Path taken:** the same container as Phases 8–10 (4 vCPUs, no GPU), Docker Compose `core` + `mock-gpu`, real CPU engines where routable, fixture LLM. No money was spent. The container restarted once during the phase (the Docker daemon was restarted and the stack brought back with `make infra-up`).

## Definition of Done

> failure-injection tests prove that only failed nodes rerun and that budgets cap retries; behavior QC actions follow the outcome matrix; the creator and environment continuity suites pass; critique proposals apply cleanly. (§40)

| DoD item | Where | Result |
| --- | --- | --- |
| Only failed nodes rerun | `tests/e2e/test_qc_gate_mock.py::test_a_failed_take_reruns_only_its_own_nodes_then_passes` (QC failures injected with `MOCK_QC_FAIL_NODES`) | pass: both takes of the talking shot fail their first attempt; only the best-scoring take's avatar node gets a new attempt (`qc_retry`, attempt seed `hash(base, 1)`), its observation and judgement re-run, nothing else gets an attempt; the version ends `ready`; the BuildManifest pins the accepted attempt's seed and artifact, the losing attempt is `qc_rejected` |
| Budgets cap retries | `…::test_budgets_cap_the_retries_and_the_version_is_flagged_with_its_best_attempt`; `packages/py/ce_qc/tests/test_ladder.py` (count and USD budgets per node and per version) | pass: with failures that never stop, exactly `retries_per_node` re-runs happen, the ladder notes the spent budget, the version ends `needs_review` with its best attempt |
| The ladder's fallback rung | `…::test_the_fallback_route_runs_after_the_seed_retry_in_the_final_tier` | pass: seed retry, then the route's fallback avatar engine (`reason: fallback`, other adapter, recorded in the manifest), version `ready` |
| Behavior QC follows the outcome matrix | `packages/py/ce_behavior/tests/test_triad.py` (matrix of priority × level × verdict × reliability, render defects, audio targets) | pass: 11 matrix rows, the render-defect rule and voice targets |
| Creator continuity suite | `packages/py/ce_qc/tests/test_consistency.py`; `tests/e2e/test_qc_jobs_mock.py::test_a_built_version_gets_a_consistency_report` | pass: features, rolling baselines, bands and deviations; a finished build starts `ConsistencyWorkflow` and writes a report for every dimension plus a rolling baseline |
| Environment continuity suite | `packages/py/ce_world/tests/test_continuity.py`; `qc.world` / `qc.continuity` in every fixture build (`tests/e2e`) | pass: element expectations and left/right relations, scoring of answered questions only, background continuity per binding |
| Critique proposals apply cleanly | `tests/e2e/test_qc_jobs_mock.py::test_a_critique_finding_applies_cleanly`; `apps/api/tests/test_qc_api.py` | pass: `CritiqueWorkflow` writes scores and findings; a finding's operations become a `from_critique_finding` proposal that validates and applies into a derived version |

## What was built

| Area | Result | State |
| --- | --- | --- |
| Per-node checks (ADR 0056) | thresholds keyed by metric adapter (unthresholded adapters report only), advisory lip sync and beta languages, the VLM judge (structured, versioned prompts; critical defects gate `final`) | implemented and tested (mock) |
| Decision ladder | per-shot gate in the build workflow; attempt seed → fallback route → cheaper fix (proposed) → review; per-node / per-version budgets in count and USD; deferred BuildManifest rows; `qc_rejected` attempts leave the cache | implemented and tested |
| Performance QA | take-level retries through the ladder; viewer-level failures and render defects flag review; audio items retry the voice (D119) | implemented and tested |
| World continuity | VLM element/relation check per shot, background continuity per video, cross-video world score in consistency; world QC rows in the QC report | implemented and tested (mock VLM) |
| Consistency | `ConsistencyWorkflow` after every build, rolling baselines, bands, deviations, the human rating queue | implemented and tested |
| QC report UI | shot gates with ladder history, take verdicts, judge defects, world rows, the requested → compiled → observed triad | implemented; Vitest + Playwright |
| Creative Director | `CritiqueWorkflow` (rules critic + VLM pass), findings → edit proposals; `AutonomousSuggestWorkflow` refuses (flag off) | implemented and tested |
| Benchmarks | `eval/cases.yaml` (extends the smoke set), `BenchmarkWorkflow` on the fleet, checks, measurements, blind pairs, verdict, `bench_passed`, promotion requires it | implemented and tested on mock engines |
| `audio_emotion` | emotion2vec+ base adapter, sandbox, non-commercial license block (FunASR model license read at a pinned commit), CPU stand-in, `funasr` asr variant | implemented; contract tests on the stand-in |
| Calibration | human ratings paired with automatic verdicts per check and analyzer revision → precision/recall/F1 → reliability | implemented and tested (API) |
| Mix | look-ahead limiter instead of loudnorm's dynamic fallback (D120) | implemented; render goldens pass |
| Docs | ADR 0056, D116–D127, `docs/QC.md`, `docs/CONSISTENCY.md`, BEHAVIOR, WORLDS, CREATORS, PLUGINS, GPU_VALIDATION, ENVIRONMENT_VARIABLES, README, ROADMAP, TODO, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `ruff check .`, `ruff format --check .`, `mypy` (714 files) | pass |
| `eslint`, `prettier --check .`, `tsc` (web) | pass |
| `uv run pytest --ignore=tests/e2e` | 1736 passed, 1 skipped (owner fixture clips), 9 failed → fixed and re-run green: the orchestrator workflow tests' fake activity set lacked the three new activities (7), the new `audio_emotion` adapter had no smoke case (1, a case and a `classes` check were added), and one studio-helper test that passed on re-run (110 passed in the affected suites; 665 passed in phase-0, invariants, QC, world, behavior and API suites after the last changes) |
| `pytest tests/e2e` (Compose infrastructure, main checkout) | 18 passed (incl. the real-CPU engines run and the 4 QC gate tests); `tests/e2e/test_qc_jobs_mock.py` added afterwards: 3 failed first (a signature-phrase type error in the consistency stage, a missing idempotency key in the test, registry rows missing in the e2e database) → fixed, 3 passed |
| `make e2e-mock` (rebuilt images) | pass: fixture build, cached rerun, text → previz → approve → video, edit → apply → compare → restore, worker kill recovered 58 s after the kill |
| `make test-e2e` (Playwright, Google Chrome) | 4 passed (incl. the QC report on the played video and the Models / Ratings pages) |
| Vitest (`apps/web`) | 69 passed |

**Found and fixed by the new gate:** the default fixture's emphasis was judged *not observed* on the final mix, which would have flagged every mock build. Two causes: loudnorm fell back to its dynamic mode (an AGC that flattened word emphasis) — replaced by a limiter (D120) — and the mock TTS emphasis did not survive the mic compressor's release after the previous word; the mock now performs emphasis with a short breath and more gain. Also fixed: the exact-script loop's BuildManifest pinned the first (rejected) attempt.

## What is untested, and why

- **Every real engine under the gate, the judge, the benchmark and calibration** — no GPU; real VLMs, GPU metrics and avatar engines are `untested_on_gpu`. Thresholds for UVQ and UTMOS are deliberately unset (report-only).
- **`audio_emotion` on its weights** — no GPU run, and the license keeps it in the sandbox.
- **Calibration with enough ratings** — the API path is tested with a handful of ratings; real calibration needs ≥ 20 rated items per check.

## Important decisions

ADR 0056; D116 (per-shot ladder and budget split), D117 (cheaper fix proposed), D118 (judge without crops/triage), D119 (audio retry targets), D120 (limiter instead of dynamic loudnorm), D121 (consistency trigger), D122 (bands without a spread), D123 (rules critic), D124 (benchmark verdict and promotion), D125 (bench profiles as measurements), D126 (`audio_emotion` sandbox), D127 (calibration storage).

## Known issues and limitations

- Promotion now requires a passed benchmark; without a GPU no real engine can be benchmarked here, so nothing new can be promoted.
- The critic is rule-based; an LLM critic is a follow-up (D123).
- Bench behavior profiles store measurements, not success rates (D125).
- VLM window questions for behavior proxies stay `NOT_MEASURABLE` in judgement until a real VLM is routable.
- Abandoned consistency children of builds run after the build returns; a stopped test stack can leave them pending in Temporal.

## Final Phase 11 state and what Phase 12 inherits

Builds gate themselves, retry only what failed within budgets and end with their best attempt and visible flags; every finished video gets a consistency report; critiques turn into edit proposals; engines are promoted on blind-rated benchmarks. Phase 12's memory loop can read the consistency reports, accepted critique proposals and selected-take signatures as write paths (§18.4).

## Post-audit corrections (2026-10, after Phase 14)

- **C3** — after an infrastructure failure inside the QC ladder the accepted node's row stayed `failed`. **S7** — the QC report, critiques and consistency were never refreshed after `ready`. The critique panel invalidated a query key nothing used.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-11`).

## Product-audit corrections (2026-10-07)

- EXTRA-CONSIST (the consistency check could not be started from the UI).

Found by the product-level audit; evidence, regression tests and fixes in [`PRODUCT_LOGIC_AUDIT_REPORT.md`](../PRODUCT_LOGIC_AUDIT_REPORT.md) (corrected in tag `phase-14-audit`).
