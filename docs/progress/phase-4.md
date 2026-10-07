# Phase 4 — AI Director, intent, acting, memory retrieval, previz: progress report

**Date:** 2026-10-04 · **Status:** DoD passed · **Path taken:** Docker (Compose `core` + `mock-gpu`), 2 vCPUs, no GPU, fixture LLM. No money was spent, no paid GPU was provisioned and no hosted LLM was called.

## Definition of Done

> acceptance fixtures pass in fixture mode for all creation examples of §2, the human behavior examples of §15.5 and the trajectory example of §15.4; exact-script property tests (Unicode, punctuation, Turkish and Azerbaijani letters) pass; the invalid-JSON repair test, prompt-injection fixtures, the memory retrieval, contradiction and repetition tests [4], and the SSRF tests pass. (§40) Plus, per §37: invariant tests I7, I10 and I13.

Verified on a **fresh clone** at commit `536ea2b`: `make bootstrap`, `make build-images` (both images rebuilt from the clone), `make e2e-mock` (which runs `make dev` first), `make lint`, `make typecheck` and `CE_REQUIRE_INFRA=1 make test` (infrastructure tests may not skip) all exited 0 — **1247 tests passed, none skipped**. The fresh-clone `make e2e-mock`: the fixture spec built in 24.3 s with 25/25 coverage entries; 41/41 cache hits on the rerun with an identical render; text → plan → previz in 9.1 s (planner `llm`, measured timings, no blocking findings) → approve → ready render in 91.1 s in total; kill → lease expiry → same-seed retry → `ready` 40.7 s after the kill.

| DoD item | Where | Result |
| --- | --- | --- |
| §2 creation examples in fixture mode | `packages/py/ce_director/tests/test_acceptance_creation.py` (`-m acceptance`) | pass: every example plans a valid spec with full predicted coverage on both mock matrices — the idea explainer ("30-second TikTok … AI agents": reveal structure and intent-policy trace, after an invalid-JSON first answer that is repaired), the 45-second desk exact script (every byte kept, wording locked), the Azerbaijani exact script with the user's tags converted, the UGC review (matching creator in her bedroom, phone selfie camera, the testimonial guard's classifier runs and passes, uncertain claims reported), excited → skeptical → serious before the CTA, the brain-dump cold-email video (explicit constraints, aggressive open, examples as B-roll), the halfway realization that wins viewers over, and the calm-masking-surprise casting |
| §15.5 human behavior examples | `packages/py/ce_director/tests/test_acceptance_behavior.py` | pass: "realizes the viewer may disagree" (realization trigger at "see", glance-away gaze, punch-in at the trigger), "remembers something embarrassing" (memory stimulus, felt embarrassed / displayed amused with masking, small smile, pause), "pretends to stay calm while surprised" (her and his variants: suppressed reaction, low-intensity brow, brief double take tied to the trigger), "convince a skeptical audience" (rising confidence, open palms, counting on fingers), "confident after realizing she is correct" (sudden realization, posture and prosody change); CBS controls and coverage entries on both matrices, segment ≥ global |
| §15.4 trajectory | same file | pass: "starts excited, becomes skeptical, pauses, looks away, laughs slightly, then serious before the CTA" (the voice laughs where the face does), and the seconds-based seven-band request mapped to word-anchored states with reported drift (≤ 0.5 s) |
| Exact-script property tests | `packages/py/ce_voice/tests/test_tags_and_exact.py` | pass: Hypothesis over Unicode text with Turkish and Azerbaijani letters, punctuation and brackets — tags removed and everything else kept, any text reassembles exactly, extraction covers the script byte for byte, Unicode never normalized, offsets that drop or cut text refused |
| Invalid-JSON repair | `packages/py/ce_llm/tests/test_llm.py::test_invalid_json_is_repaired`; the `explain_ai_agents` fixture end to end | pass |
| Prompt-injection fixtures (I10) | `tests/invariants/test_i10_data_not_instructions.py`; `injection_attempt` fixture | pass: injected input and memory text reach prompts only as data; an obeyed injection cannot add fields or labels; delimiters cannot be closed from inside |
| Memory retrieval, contradiction, repetition [4] | `packages/py/ce_memory/tests` | pass: scope and status filters, budgets with pinned first, recency and keyword ranking, conflict precedence; canon and memory contradictions (blocking when pinned); hook, phrase, arc and visual-pattern repetition; persisted, immutable snapshots; usage events per video |
| SSRF tests | `packages/py/ce_research/tests/test_ssrf.py` | pass: non-global addresses in every form (mapped, NAT64, 6to4, Teredo, literals), mixed DNS answers, connection to the checked IP with Host and SNI, every redirect re-checked, scheme/port/shape policy, size and time limits, content types |
| I7, I10, I13 | `tests/invariants/test_i07_*`, `test_i10_*`, `test_i13_*`; `tests/e2e/test_plan_mock.py` | pass |
| Workflows: plan → previz → approve → generate | `apps/orchestrator/tests/test_workflows.py`, `apps/api/tests/test_planning_api.py`, `tests/e2e/test_plan_mock.py`, `make e2e-mock` step 3 | pass: text → `planned` → `previz_ready` with measured timings → `:approve` → `ready`, previz TTS and alignment reused from the cache |

## What was built

| Area | Result | State |
| --- | --- | --- |
| `ce_llm` | Provider registry and plugins (`fixture`, `openai_compatible`, `anthropic`), scenario replay, recorder, `structured()` with targeted repairs (model output wrapped as data), prompt library with versions and the `data` filter | implemented and tested |
| `ce_voice` | Acting-tag parser, exact-script extraction with byte-equality verification | implemented and tested |
| `ce_director` | Stages 1–11, vocabulary mapper (D49), intent policy engine with decisions, situational acting with the route preview, plan-time compile with `compiler_approximation` elements, plan report, template Director (D50), persistence (D51) | implemented and tested |
| `ce_memory` | Retriever, snapshots, repetition guard, contradiction checker, usage log | implemented and tested |
| `ce_policy` | Blocklists, testimonial guard (D57) | implemented and tested |
| `ce_research` | SSRF-guarded fetcher, extraction, chunking, BM25, dossiers (ADR 0038, D56) | implemented and tested |
| `ce_exec`, orchestrator | `run_plan`, `complete_previz`, previz planning (D52), usage events on `ready`, disclosure labels in the render; `PlanVideoWorkflow`, `PrevizWorkflow` | implemented and tested |
| API | Create video, previz, intent, replan, approve (D53–D55); `claims_summary`; I12 cases; OpenAPI and TS client | implemented and tested |
| Config | `config/director.yaml` (D58), `research:`, intent policies for trajectories and CTAs, testimonial settings | implemented and validated by the loader |
| Images and scripts | Services image carries prompts and fixtures; orchestrator installs LLM plugins; `make e2e-mock` step 3; e2e stacks drop their buckets | done |
| Docs | ADRs 0037–0038, DECISIONS D49–D58, `docs/DIRECTOR.md`, MEMORY, API, INVARIANTS, ENVIRONMENT_VARIABLES, README, TODO, ROADMAP, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `make lint` (ruff check + format, eslint, prettier) | pass |
| `make typecheck` (mypy over 404 files, tsc) | pass |
| `CE_REQUIRE_INFRA=1 make test` | 1247 passed, 0 skipped (8 min 21 s, fresh clone) |
| `make build-images` | api and services images rebuilt |
| `make e2e-mock` | pass (fresh clone: 24.3 s cold fixture build, coverage 25/25, 41/41 cached, text → previz 9.1 s → approved video 91.1 s, worker kill recovered in 40.7 s) |
| `scripts/gen_openapi.py --check`, `scripts/gen_schema.py --check`, `make verify-spec verify-config` | fresh / 0 errors |

New tests in this phase (199): `packages/py/ce_director/tests` (40), `packages/py/ce_research/tests` (73), `packages/py/ce_policy/tests/test_guards.py` (18), `packages/py/ce_memory/tests` (15), `packages/py/ce_llm/tests` (9), `packages/py/ce_voice/tests` (7 property and unit tests, Hypothesis with 200–300 examples each), invariants I7 (3), I10 (4), I13 (20), `apps/api/tests/test_planning_api.py` (4), plan/previz workflow tests (3), `tests/e2e/test_plan_mock.py` (1), plus additions to the tenancy, config and docs tests.

**Fixes found while verifying:** a memory infra test wrote videos into the seeded project and broke another test's assertion (now uses its own project); leaked per-test buckets had exhausted SeaweedFS's volumes (156 buckets), so the e2e `Stack` now empties and drops its buckets on teardown (D23); two variables shadowed in the config loader (mypy).

## Files changed

Added: `packages/py/ce_director/src/ce_director/{build,context,director,draft,intent_policy,models,runs,store,template,timing,vocabmap}.py` and `tests/`, `packages/py/ce_llm/src/ce_llm/{data,prompts,provider,recorder,structured}.py` and `tests/`, `packages/py/ce_memory/src/ce_memory/{contradictions,records,repetition,retrieval,store,text,usage}.py` and `tests/`, `packages/py/ce_policy/src/ce_policy/{blocklist,testimonial}.py`, `packages/py/ce_policy/tests/test_guards.py`, `packages/py/ce_research/src/ce_research/{documents,research,retrieval,ssrf}.py` and `tests/`, `packages/py/ce_voice/src/ce_voice/{exact,tags}.py` and `tests/`, `packages/py/ce_exec/src/ce_exec/planning.py`, `packages/py/ce_testing/src/ce_testing/director.py`, `plugins/providers/llm/{fixture,openai_compatible,anthropic}/`, `prompts/{system,interpret,strategy,script,fact_check,scenes,acting,testimonial_guard}/v1.md`, `eval/llm_fixtures/*.yaml` (14 scenarios), `scripts/fixtures/author_llm_fixtures.py`, `config/director.yaml`, `apps/api/src/ce_api/routers/planning.py`, `apps/api/tests/test_planning_api.py`, `tests/invariants/{test_i07_memory_snapshot,test_i10_data_not_instructions,test_i13_closed_vocab}.py`, `tests/e2e/test_plan_mock.py`, `docs/DIRECTOR.md`, `docs/adr/0037-*.md`, `docs/adr/0038-*.md`, `docs/progress/phase-4.md`.

Changed: `config/default.yaml` (`research:`), `config/intent_policies.yaml`, `config/policy/testimonials.yaml`, `ce_config` (schemas, loader checks), `ce_exec` (runtime previz planning and usage events, refs loader, executors' disclosure labels), orchestrator (models, activities, workflows, LLM plugin dependencies), API (app, jobs, videos router, dependencies), `ce_qc` (marked a Phase 11 skeleton), `ce_testing.stack`, package `__init__`s and `pyproject.toml`s, `infra/docker/services.Dockerfile`, `scripts/e2e_mock.py`, `Makefile`, `.prettierignore`, `pyproject.toml` (acceptance marker), `uv.lock`, the OpenAPI document and TS client, docs and tests listed above.

## Known issues and limitations

- **Fixture LLM only.** All plans in tests and `make e2e-mock` come from authored fixtures or the template Director; the OpenAI-compatible and Anthropic providers are tested against mocked HTTP only. Recording real answers needs a key and an owner decision on spend.
- **Whole-video replans.** `:replan` validates and records `scope` but replans everything; scene-scoped replans arrive with edits in Phase 6 (D54).
- **Research is per plan and keyword-only.** No persistent sources, embeddings or claim ledger until Phase 12; `sources` on create answers `501` (D55); pages that need JavaScript yield little text (ADR 0038).
- **Claim overrides live in the approval audit log** until the claim ledger (Phase 12) (D53).
- **Template stages.** If a model's output stays invalid after the repairs, that stage uses its template; the plan says so (assumption + warning finding) and previz review precedes any generation (D49).
- **Mock evidence.** Coverage and durations are measured on mock engines (TTS mock WPM ≠ a real voice); the 30-second example measures ~36 s in previz, within the ±25 % duration tolerance.
- **Vocabulary mapping is lexical**; embedding similarity arrives in Phase 12.
- **The testimonial guard's review UI and quote-consent flow** are Phase 13.

## Next phase and actions

**Phase 5 — Frontend MVP shell** (§40), started automatically after this report:

1. The Next.js app (`apps/web`) with auth (session cookie + CSRF) and navigation, on the generated TS client.
2. Dashboard and Projects.
3. The Create wizard with previz review: intent summary, performance preview, predicted coverage, memory used, repetition and contradiction reports, blocking findings with overrides, approve.
4. Video Studio v1: player, scene list, read-only performance lane, Simple coverage badges, job progress via SSE, versions list.
5. Creators and Worlds lists (read-only DNA), Jobs, developer spec and CBS viewers.
6. DoD: a Playwright e2e test in mock mode (create → previz → approve → progress → play) and the I9 coverage-badge contract test.

## Post-audit corrections (2026-10, after Phase 14)

- **A4** — version numbers were allocated MAX+1 without a lock (concurrent replans/edits collided). **A6** — more sources than the Director's `PlanRequest` accepts answered 500 instead of 422.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-4`).

## Product-audit corrections (2026-10-07)

- PLAN-FAIL (a plan the router could not route, e.g. a language no voice speaks, left its job `running` at 5 % forever: the workflow had no failure handler).

Found by the product-level audit; evidence, regression tests and fixes in [`PRODUCT_LOGIC_AUDIT_REPORT.md`](../PRODUCT_LOGIC_AUDIT_REPORT.md) (corrected in tag `phase-14-audit`).
