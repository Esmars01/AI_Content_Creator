# Phase 10 — Creator Studio and World Studio: progress report

**Date:** 2026-10-05 · **Status:** DoD passed in mock mode. **No GPU was available, so no real GPU run exists and nothing is recorded in `docs/GPU_VALIDATION.md`; every studio output in this phase was made by a mock engine and is labelled `mock`.** · **Path taken:** the same container as Phases 8–9 (4 vCPUs, no GPU), Docker Compose `core` + `mock-gpu`, real CPU engines where they apply (Kokoro, faster-whisper, MediaPipe, DINOv2, AuraFace, DNSMOS), fixture LLM. No money was spent.

## Definition of Done

> e2e tests in mock mode: creator → test → approve; world → plates → approve → used in a video; the world versioning tests pass; real GPU runs are recorded in GPU_VALIDATION when available. (§40)

| DoD item | Where | Result |
| --- | --- | --- |
| creator → test → approve (mock) | `tests/e2e/test_studio_mock.py::test_creator_identity_voice_wardrobe_then_test_then_approve` | pass: draft creator → identity-pack candidates → canonical face → expansions with identity scores and the VLM age check → review → voice design → candidate → draft voice version → test bench → approve → wardrobe references → Creator Test (a real build of the fixed script through `GenerateVersionWorkflow`) → scorecard and baseline → approve |
| world → plates → approve → used in a video (mock) | `tests/e2e/test_studio_mock.py::test_world_plates_approve_then_used_in_a_video`, Playwright `apps/web/e2e/world-studio.spec.ts` | pass: a new world version, plate candidates, one canonical plate per permitted position, fingerprints, approval, then a generated video whose BuildManifest uses the approved plates; the same flow in the browser |
| World versioning tests | `tests/invariants/test_i06_override_isolation.py` (incl. `test_world_studio_promotion_is_an_explicit_new_draft`), `apps/api/tests/test_worlds_api.py`, `apps/api/tests/test_studio_api.py` | pass: approved versions immutable, overrides stay scene-local until explicitly promoted into a new draft, diffs, plate choices only on drafts |
| Real GPU runs in GPU_VALIDATION | — | **not applicable: no GPU.** The studio jobs dispatch every GPU family through the scheduler (ADR 0055), so they run unchanged on fleet hosts once real engines are routed |

## What was built

| Area | Result | State |
| --- | --- | --- |
| Studio jobs (ADR 0055) | one deterministic loop for `identity_pack`, `wardrobe_refs`, `voice_design`, `voice_test`, `world_plates`, `creator_test`; model calls routed like build nodes, dispatched to the scheduler (migration 0003: studio nodes without a video version); outputs the user picks from become org assets | implemented and tested (mock engines) |
| Identity pack | candidates, canonical face, angle/expression expansions conditioned on it, identity scores, VLM apparent-age check (adult margin), review incl. an approver's age review | implemented; mock-only results |
| Voices | design → candidates → selection → draft version; test bench (WER, CER, WPM, speech quality, speaker similarity); lexicon; WPM calibrated only from real engines; approval | implemented; real Kokoro TTS used where routable |
| Creator Test | the fixed ~20 s script (`ce_creator.test_script`), the test video as a real build, the scorecard (`ce_creator.scorecard`: every metric with method, `mock` flag or `not_measured` reason), ratings, history, baseline row | implemented and tested |
| World Studio | plate candidates per position × time of day × weather, choices with fingerprints (`ce_world.plates`: colour temperature, luminance, left/right ratio), approval, version diffs, override promotion as a new draft, continuity report | implemented and tested |
| Consent | data model and endpoints; start/submit refuse while `digital_twins_enabled` is off (V1) | implemented and tested |
| API | identity pack, wardrobe references, voices, Creator Tests, baselines, consistency read, plates, world continuity, artifact download, appearances, consents; I12 cases for every new id route | implemented and tested |
| Web | Creator Studio (Overview, DNA editor, Memory with pin/forget/supersede/resolve, Appearance, Voice, Wardrobe, Creator Test, Consent) and World Studio (DNA with floor plan, Plates, Versions, Continuity) | implemented; logic covered by Vitest, the world flow by Playwright |
| Docs | ADR 0055, D109–D115, CREATORS, WORLDS, INVARIANTS (I6 complete), API, ROADMAP, TODO, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `ruff check .`, `ruff format --check .`, `mypy` (686 files) | pass |
| `eslint`, `prettier --check .`, `tsc` (web) | pass |
| `uv run pytest --ignore=tests/e2e` | 1691 passed, 1 skipped (owner fixture clips), 1 error → fixed: a helper named `test_world_binding` in `ce_exec.creator_test` was collected by pytest; renamed, its package re-run green (21 passed) |
| `pytest tests/e2e` on the committed Phase 10 tree (`2ae8ab0`, separate worktree, Compose stack) | 13 passed, 1 deselected (`test_cpu_real`: in the fresh worktree's virtual environment the Kokoro phonemizer's eSpeak data path did not resolve — an environment difference, not code); `test_cpu_real.py` passed in the main checkout (1 passed, 68 s) |
| Playwright (`make test-e2e`, Google Chrome) | 3 passed (login/plan/edit flows and World Studio) |
| Vitest (`apps/web`) | 63 passed |

A first e2e run was invalidated by my own edits: Temporal's workflow sandbox re-imports workflow modules from disk, so Phase 11 work-in-progress in the same checkout leaked into running workflows. Phase 10 was then committed and its e2e run from a separate worktree; that is the result above.

## What is untested, and why

- **Every studio job on real GPU engines** (image generation and editing, VLM, voice design, GPU TTS, embeddings on GPU) — no GPU.
- **Real-face identity and age checks** — the VLM and face embeddings are mocks here; mock values never count as measurements of a creator.
- **Consent verification** — V1 (flag off).

## Important decisions

ADR 0055 (studio jobs); D109 (`execution_nodes.version_id` nullable), D110 (age review on identity packs), D111 (wardrobe reference generation endpoint), D112 (studio reads), D113 (consent flows off until V1), D114 (Creator Test baselines), D115 (world continuity report).

## Known issues and limitations

- Rolling baselines across videos, consistency charts and the behavior history tab need `ConsistencyWorkflow` (Phase 11).
- World proposals from planning when no approved world matches (D69) are not built.
- The floor plan is drawn from the DNA; editing is by sections (no drag editor).

## Final Phase 10 state and what Phase 11 inherits

Creators and worlds can be designed, tested and approved end to end on mock engines; the Creator Test and the plates produce the baselines and fingerprints that Phase 11's QC gate, world continuity checks and consistency reports compare against.

## Post-audit corrections (2026-10, after Phase 14)

- `useStudioJob` (8 Studio panels) re-invalidated its queries on every render once a job finished — an endless refetch loop. The creators/worlds pages still said the studios "arrive in Phase 10".

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-10`).
