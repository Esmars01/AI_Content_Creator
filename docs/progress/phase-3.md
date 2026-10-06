# Phase 3 — Behavior backbone: progress report

**Date:** 2026-10-04 · **Status:** DoD passed · **Path taken:** Docker (Compose `core` + `mock-gpu`), 2 vCPUs, no GPU. No money was spent and no paid GPU was provisioned.

## Definition of Done

> the behavior suites marked [3] in §37 are green; `make e2e-mock` produces a coverage report with requested, compiled and observed entries for every CBS item of the fixture spec. (§40) Plus, per §37: invariant tests I1 and I4.

Verified on a **fresh clone** at commit `9047180`: `make bootstrap`, `make build-images` (both images rebuilt from the clone), `make e2e-mock` (which runs `make dev` first), `make lint`, `make typecheck` and `CE_REQUIRE_INFRA=1 make test` (infrastructure tests may not skip) all exited 0 — **1048 tests passed, none skipped**. The fresh-clone `make e2e-mock`: built in 22.1 s; 25/25 CBS items with requested, compiled and observed entries and 25 viewer-level rows; 41/41 cache hits on the rerun with an identical render; kill → lease expiry → same-seed retry → `ready` 39.9 s after the kill.

| DoD item (§37 suite) | Where | Result |
| --- | --- | --- |
| Situational acting compilation [3] — each acting-chain link compiles into CBS fields and then into methods for both mock matrices | `packages/py/ce_behavior/tests/test_situational.py` | pass: every link (situation … reaction, editorial response) lands in the CBS; a 25-row table pins the level and method of every item for the global-prompt and the segment-control mock; plan-time proposals (split at the realization, cutaway over the look-away) for the global mock only; build time never invents elements; compiler rules (global prompt needs full coverage, unreliable → APPROXIMATED, mode-gated editorial methods, the CBS never changes, downgrades vs the planned route) |
| Emotional trajectory [3, 4] — states tile; transitions need triggers; masking; seconds → states with drift | `packages/py/ce_behavior/tests/test_trajectory.py` | pass: tiling gaps, intensity jumps, turns between incompatible emotions without a trigger and triggers outside their state are errors; masking with felt = displayed is rejected; strategies that express nothing of the emotion warn; confidence accumulates over the trajectory; the §15.4 seven-band request maps to tiling word spans with per-edge drift (≤ 0.2 s here), and impossible requests report large drift or refuse |
| CanonicalBehaviorSpec [3] — schema; deterministic resolution; resolution order (§10.6); digest stability; no engine fields | `packages/py/ce_behavior/tests/test_cbs.py` | pass: the §15.6 example (profile, affordances, confidences 0.7/0.85, prosody directives); 25 requested controls with priority/confidence/reliability rules; round trip and envelope assembled on read; deterministic and order-independent; digest ignores ids and intent notes, tracks behavior, identical across engine routes; memory shift limit, ranges narrowed inside DNA only, DNA clamps unless out of character, avoidance union and caps, user tags win; schema and document lint |
| Adapter behavior translation [3, 8] — golden translations; unsupported reported, never dropped | `plugins/mock/tests/test_translator_golden.py`; contract suite (`test_mock_contract.py`) | pass: golden files for `mock_global_v1`, `mock_segment_v1`, `mock_voice_v2` (both segments); encoded ∪ unsupported = the compiled realizations, disjoint, every unsupported item has a reason |
| Requested vs compiled vs observed [3, 7, 11] — injected failures give the right per-take verdicts and viewer-level outcomes (all 12); editorial items judged on `approximation_executed`; retry targets follow the method table | `packages/py/ce_behavior/tests/test_triad.py`; `plugins/mock/tests/test_mock_tracks_triad.py`; `tests/e2e/test_behavior_mock.py` | pass: per-proxy verdicts (CONFIRMED/PARTIAL/NOT_OBSERVED/CONTRADICTED/NOT_MEASURABLE/NOT_APPLICABLE) on synthetic tracks; real mock media — the segment mock renders the compiled directives, the mock observer measures, items dropped by `fail_items` are NOT_OBSERVED and performed ones CONFIRMED, VLM-only items NOT_MEASURABLE; all 12 outcomes, only `*_CONFIRMED` delivered; an executed cutaway passes, an unexecuted one is a render defect; 13 method → re-run cases; QA decisions and take scores. (Real CPU analyzers on fixture clips: Phase 7; QC ladder: Phase 11) |
| Model swap (I1) [3] | `tests/invariants/test_i01_model_swap.py` | pass: two router configurations, nothing persisted; identical spec content, CBS content and DNA digests and identical resolve/voice cache keys; routes, compiled outputs and coverage differ; segment ≥ global item by item; the cutaway planned for the global route is flagged for re-proposal; schema lint over every model-independent schema |
| Coverage upgrade [3] | `tests/invariants/test_i01_model_swap.py::test_enabling_the_segment_engine_upgrades_coverage_without_a_director_change` | pass: enabling `mock_avatar_segment` (final routing profile) moves the avatar route and upgrades ≥ 10 items with no downgrade and no spec or CBS change |
| A re-route flags `compiler_approximation` elements | `tests/invariants/test_i01_model_swap.py`; `tests/e2e/test_behavior_mock.py::test_a_reroute_flags_the_stale_approximation_and_upgrades_coverage` | pass: library level, and end to end — `approximations_stale` and one open edit proposal (`remove_shot sht_2`, coverage delta naming the segment mock's parametric gaze); the spec is unchanged |
| I4 | `tests/invariants/test_i04_triad.py` | pass: every requested control has a level and a method for both engines at plan and build time, including items nothing renders; every HONORED realization is a native method the routed engine declares at the requested precision (removing a declared control removes the claim) |
| `make e2e-mock`: requested, compiled and observed entries for every CBS item | `scripts/e2e_mock.py` (writes `.data/e2e/coverage.json`) | pass: 25/25 entries complete, 25 viewer-level rows, coverage summary recorded |

**Mutation checks** (each break applied, the named suite run, then reverted — all killed): native methods without the precision check (I4 suite); `UNSUPPORTED` + observed mapped to `UNSUPPORTED` (triad); memory shift without the limit (CBS); stale-approximation detection disabled (I1); state cutaways accepted at any overlap (situational); low-reliability failures allowed to retry (triad); a translator version bump (golden); turns without triggers accepted (trajectory); declared inability scored neutral (I1 coverage upgrade).

## What was built

| Area | Result | State |
| --- | --- | --- |
| `ce_behavior.resolve` | CBS resolver: §10.6 order, DNA bounds and avoidance caps, memory habits and ranges, world affordances (D36), trajectory with absolute confidence, events, prosody directives, continuity, constraints, `requested_controls` and provenance (D35, D37, D38) | implemented and tested |
| `ce_behavior.compiler`, `directives`, `plan` | Realization methods by preference order, plan-time proposals and build-time realizations (D39), ProsodyPlan/VisualPlan, predicted coverage, downgrades, the wire directives, whole-version compile without persistence | implemented and tested |
| Translators (`plugins/mock`) | Global prompt, segment list and voice parameters; encoded/unsupported for every realization; mock avatars perform encoded items with per-method failure rates | implemented and tested (golden files) |
| `ce_behavior.observe`, `judge`, `coverage`, `qa` | Measurement and stitching, proxy verdicts (D41), viewer-level coverage, 12 outcomes, QA decisions and retry targets (D40), take scores (D44) | implemented and tested |
| `ce_behavior.profiles`, `approximations`, `lint`, `acting` | Profile aggregation (D42), stale approximations (D45), I1 lint, trajectory and seconds-to-states | implemented and tested |
| `ce_core` validator | Turns need triggers, triggers inside their state, strategy-fit warning | implemented and tested |
| `ce_exec` | Behavior nodes replacing the stubs; records (`qc_reports`, observations, coverage summary, profiles, `coverage.updated`); stale-approximation proposal at planning; planned routes passed to the build | implemented and tested |
| `ce_router` | Declared inability scores 0 (D43) | implemented and tested |
| API | `GET /v1/versions/{id}/behavior`, `/coverage`, `GET /v1/takes/{id}/observations` (D47); OpenAPI, TS client | implemented and tested (incl. I12 cases) |
| Docs | ADR 0036, DECISIONS D35–D48, `docs/BEHAVIOR.md`, INVARIANTS (I1, I4), API, ADAPTERS, PLUGINS, VIDEOSPEC, README, TODO, ROADMAP, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `make lint` (ruff check + format, eslint, prettier) | pass |
| `make typecheck` (mypy over 347 files, tsc) | pass |
| `CE_REQUIRE_INFRA=1 make test` | 1048 passed, 0 skipped (5 min 49 s, fresh clone) |
| `make build-images` | api and services images rebuilt |
| `make e2e-mock` | pass (fresh clone: 22.1 s cold build; coverage 25/25; 41/41 cached; worker kill recovered in 39.9 s) |
| `scripts/gen_openapi.py --check`, `scripts/gen_schema.py --check`, `make verify-spec verify-config` | fresh / 0 errors |

New tests in this phase: `packages/py/ce_behavior/tests/` (5 files, 75 tests), `plugins/mock/tests/test_translator_golden.py` and `test_mock_tracks_triad.py`, `tests/invariants/test_i01_model_swap.py` and `test_i04_triad.py`, `tests/e2e/test_behavior_mock.py`, `apps/api/tests/test_behavior_api.py`, plus additions to the router, exec, tenancy and docs tests.

**The fixture coverage report** (`make e2e-mock`, global-prompt mock): 9 HONORED, 7 APPROXIMATED, 9 UNSUPPORTED; outcomes — 6 HONORED_CONFIRMED (prosody rate ×2, energy ×2, emphasis, pause), 1 HONORED_PARTIAL (emphasis on "not."), 1 APPROXIMATED_NOT_OBSERVED (the look-away under its executed cutaway — expected), 3 UNSUPPORTED_OBSERVED (emergent gaze and gesture), 3 UNSUPPORTED, 11 NOT_MEASURABLE (visual and vocal emotion, posture, camera awareness, reaction, pitch — analyzers that do not run yet); QA decision `warn` (the partial emphasis), recorded with `executed: false`. With the segment mock the same spec compiles to 20 HONORED.

## Files changed

Added: `packages/py/ce_behavior/src/ce_behavior/{acting,approximations,compiler,coverage,directives,inputs,judge,lint,observe,plan,profiles,qa,resolve,scene}.py`, `packages/py/ce_behavior/tests/*`, `packages/py/ce_exec/src/ce_exec/{behavior,behavior_records}.py`, `packages/py/ce_testing/src/ce_testing/behavior.py`, `apps/api/src/ce_api/routers/behavior.py`, `apps/api/tests/test_behavior_api.py`, `plugins/mock/tests/{test_translator_golden,test_mock_tracks_triad}.py`, `plugins/mock/tests/golden/*.json`, `tests/invariants/{test_i01_model_swap,test_i04_triad}.py`, `tests/e2e/test_behavior_mock.py`, `docs/BEHAVIOR.md`, `docs/adr/0036-behavior-engine-implementation.md`, `docs/progress/phase-3.md`.

Removed: `packages/py/ce_exec/src/ce_exec/behavior_stub.py`.

Changed: `config/default.yaml` (`behavior:`), `ce_config` (schemas, loader checks), `ce_core` (CBS `RequestedControl.span`/`duration_ms`, `ProsodyDirective.delivery`, optional situation; observed `take_sha256`; acting validators), `ce_build` (graph params and deps for the behavior nodes, kinds and impl versions), `ce_contracts` (translator coverage check), `ce_router` (declared inability, `unreliable_dimensions`), `ce_exec` (executors, requests, runtime bookkeeping and planning), mock plugins (avatar adapter and translators, voice translator, behavior track, manifests), `ce_testing` (fixture route digest), API app and dependencies, JSON schemas and TS client, `scripts/e2e_mock.py`, `.prettierignore`, `uv.lock`, docs and tests listed above.

## Known issues and limitations

- **Mock evidence.** The mock observer measures the mock engines' own behavior tracks (§16.7); coverage numbers say the pipeline works, not that a real engine performs. Real CPU analyzers arrive in Phase 7, benchmark profiles in Phase 8.
- **NOT_MEASURABLE items.** Visual and vocal emotion (VLM window, audio emotion), pitch, posture and camera awareness without a body/pose proxy, and reactions stay NOT_MEASURABLE until their analyzers run (D41). They never fail QA.
- **QA decisions are recorded, not executed** (D40); take ranking uses pass/fail QC metrics until they are calibrated (D44).
- **Measured profiles are recorded but not loaded into the live router**; that needs a per-version snapshot to keep rebuilds deterministic (D42, Phase 8).
- **Coverage upgrade needs the final routing profile.** In `draft` the latency weight (0.3) outweighs any behavior difference (0.2) between the two mocks, so enabling the segment mock upgrades final renders, not drafts (D43). That follows the configured weights.
- **Stale-approximation proposals are data.** The proposal row and the flag exist; applying or dismissing it (and clearing the flag) is the Phase 6 edit pipeline (D45).
- **Plan-time proposals are not applied yet.** `ce_behavior.plan` proposes splits, punch-ins and cutaways; Director stage 11 writes them into the spec in Phase 4.
- **Energy judgement is relative.** Without an absolute creator baseline in the audio, energy is judged against the rest of the speech; on the Compose mix the global-prompt build confirmed it, the in-process mix showed PARTIAL in one run — both honest readings of a small (≈ 8 %) requested difference.

## Next phase and actions

**Phase 4 — AI Director, intent, acting, memory retrieval, previz** (§40), started automatically after this report:

1. `ce_llm` providers (hosted, OpenAI-compatible, fixture replay with recorder) and prompt templates; I10 data-as-data wrapping.
2. Director stages 1–11: tag parser and exact-script extraction (byte equality), duration fitting with seeded WPM, intent policies, situational acting (states, triggers, masking, events), world binding, plan-time routing on the CBS's requested dimensions, plan-time compile applying `ce_behavior.plan` proposals with `derived_from: compiler_approximation`, predicted coverage, the `plan_report`.
3. Memory retrieval with budgets and snapshot pinning (I7), contradiction checks, repetition guard; research with the SSRF-guarded fetcher.
4. The previz gate and `PlanVideoWorkflow`; `POST /v1/projects/{id}/videos`, `GET /v1/versions/{id}/previz`, `:replan`, `:approve`.
5. Acceptance fixtures: the section-2 creation examples and the §15.5 situational examples in fixture mode; invariants I7, I10, I13.
