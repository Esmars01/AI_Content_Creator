# Phase 6 — Incremental editing: progress report

**Date:** 2026-10-04 · **Status:** DoD passed · **Path taken:** Docker (Compose `core` + `mock-gpu`), 2 vCPUs, no GPU, fixture LLM. No money was spent, no paid GPU was provisioned and no hosted LLM was called.

## Definition of Done

> exact dirty-set tests pass for behavior-only edits (voice locked and unlocked), every World DNA change in §19.5, accent, camera and wardrobe changes, and pacing (editorial vs re-performance); environment lock tests pass and locked-path edits are refused with explanations; restore creates a new version; the edit acceptance fixtures for every §2 edit example pass; a re-route auto-proposes removing stale `compiler_approximation` elements; e2e covers "make him more skeptical" and a compare. (§40)

Verified on a **fresh clone** of the committed bundle at commit `398433f`: `make bootstrap`, `make build-images` (api, services and web rebuilt from the clone), `make e2e-mock` (which runs `make dev`; now five steps including the edit), `make test-e2e` (both Playwright specs), `make lint`, `make typecheck` and `CE_REQUIRE_INFRA=1 make test` all exited 0 — **1386 pytest tests and 50 Vitest tests passed, none skipped**.

| DoD item | Where | Result |
| --- | --- | --- |
| Exact dirty sets: behavior-only edits, voice locked and unlocked | `packages/py/ce_behavior/tests/test_dirty_sets.py` | pass: explicit expected sets (no subsets); under a voice lock no TTS/ASR/alignment node runs and the compiled voice is reused by content; unlocked, only the edited segment's voice re-runs |
| Exact dirty sets: every §19.5 World DNA change | same file | pass: lighting, acoustics (never the plate), element state (visible, invisible, practical light), camera position, time/weather, add / hide / move, `continuity_ref`, a new world version (plate, acoustics, behavior digest, a change only out of view) |
| Accent, camera, wardrobe changes | same file + `packages/py/ce_director/tests/test_acceptance_edits.py` | pass: accent (voice version) re-runs voice and what consumes it; handheld and profile changes only camera post (and what the profile feeds); wardrobe re-renders keyframe and performance of the scenes in scope |
| Pacing: editorial vs re-performance | same file | pass: `cut_cadence` changes only `post.camera` (cadence punch-ins); `target_wpm_delta` re-performs voice and performance; under a voice lock pacing changes nothing |
| Environment lock tests; locked-path edits refused with explanations | `packages/py/ce_core/tests/test_edit_core.py`, `tests/invariants/test_i08_locks.py`, `apps/api/tests/test_edits_api.py` | pass: world-lock refusals in scope and passes out of scope; every lock group refuses a patch to its paths with a `locked` issue naming the group; regenerate of a blocked component is a synchronous 409 naming the lock; failed proposals show their issues |
| Restore creates a new version | `tests/invariants/test_i03_video_versions.py`, `apps/api/tests/test_edits_api.py`, `tests/e2e/test_edit_mock.py`, `make e2e-mock` | pass: a new current version (v3) with the restored content; its build is 100 % cache hits with the identical render |
| Edit acceptance fixtures for every §2 edit example | `packages/py/ce_director/tests/test_acceptance_edits.py` (fixture mode) | pass: "make the first 3 seconds more aggressive", "make him more skeptical" (voice locked and unlocked), "make her smile less", "change the room to a modern office", "make the camera slightly handheld", "change the outfit but keep the face", "keep everything the same but change the accent" — operation types and dirty sets |
| A re-route auto-proposes removing stale `compiler_approximation` elements | `tests/e2e/test_behavior_mock.py`, `tests/invariants/test_i01_model_swap.py` | pass: after the operator disables the global avatar engine, the build flags `approximations_stale` and records a computed proposal (typed `remove_shot`, patch, impact, coverage delta with the current engine's declared control) |
| e2e "make him more skeptical" + compare | `tests/e2e/test_edit_mock.py`, `make e2e-mock` step 4, `apps/web/e2e/edit-and-compare.spec.ts` | pass: propose → apply through the workflows on the real workers; only the reveal's behavior and performance ran, the hook was 16/16 cache hits, the voice lock kept the audio; compare shows 8 spec differences in acting/intent and CBS differences in `scn_reveal` only. Through the UI: proposal card (operations, impact, voice lock, coverage delta) → Apply → v2 plays → compare page with both players and the reveal's spec and CBS differences (1.0 min) |

## What was built

| Area | Result | State |
| --- | --- | --- |
| Edit model (`ce_core.edit`) | 38-operation `EditOperation` union with slots, SpecPatch (`patch_from_diff`, key-addressed), lock checks with scope expansion and regenerate refusals (I8), anchor rebase, structured spec diff, operation → patch translation (state splitting, world remapping, deterministic regenerate seeds, lock-removal guard) | implemented and tested |
| Edit stage (`ce_director.edit`) | Reference resolution (selection, first/last N seconds, scene n, him/her/cast, emphasis, span start, event anchors), record queries, the `edit` prompt with fixtures for every §2 example, labeled template planner (incl. acting regeneration, D68) | implemented and tested |
| Proposals (`ce_director.proposal`) | `compute_proposal`: validation, impact (regenerate / keep / cascade / `no_visible_effect`), predicted coverage delta (`blocked_by_lock`), alternatives with estimates, predicted report for the derived version | implemented and tested |
| Dirty propagation (ADR 0041) | Generation digests, `BehaviorEvaluator`, output-change propagation, per-position plate views, content-pinned voice under a lock (reported as cache hits, D72), pacing editorial vs re-performance, handheld drift, lip-sync patch builds, continuity by content | implemented and tested |
| Workflows and jobs | `ProposeEditWorkflow`, `ApplyEditWorkflow` (derived builds as child workflows), `propose_edit` / `apply_edit` activities, recompute on apply (D76), derived plan report and planned routes (D71), typed re-route re-proposals | implemented and tested |
| Versions (`ce_db.versions`) | One derived-version factory for edits, wrappers, restore, branch, duplicate (D70); generate vs previz by approval | implemented and tested |
| API (ADR 0040, D67, D73–D75) | edits (create, list, get, apply, reject), locks, scene/shot regenerate, re-route, take select, takes, resume, branch, restore, duplicate, compare, estimates, vocabulary, `:variants`/`:remix` 501 | implemented and tested (API, I12 for every new route) |
| Studio UI | NL edit panel and proposal cards, Advanced performance (states, events) and intent editors, locks, shots and takes, versions tree (restore, branch, resume), compare page with synchronized players | implemented and tested (Vitest, Playwright) |
| Docs | ADRs 0040–0041, DECISIONS D66–D77, `docs/EDITING.md`, API, FRONTEND, INVARIANTS (I3, I6, I8), README, TODO, ROADMAP, CHANGELOG | written |

## Tests run and results

Development tree (this environment, the stack running Phase 6 images):

| Command | Result |
| --- | --- |
| `make lint` (ruff, eslint, prettier) | pass |
| `make typecheck` (mypy 433 files, tsc for the API client and the web app) | pass |
| `CE_REQUIRE_INFRA=1 uv run pytest` | 1383 tests; all pass (the one failure in the first full run was the ADR-count test before the ADR files were written; re-run green) |
| Vitest (`apps/web`) | 50 passed (9 files) |
| `make build-images` + `make dev` | api, services and web rebuilt; stack healthy |
| `make e2e-mock` (with `--no-kill`) | pass: cold fixture 22.8 s, 41/41 cached rerun, text → previz 9.5 s → ready, **edit step: proposal, apply, reveal re-performed, hook 16/16 cached, compare (acting/intent, CBS `scn_reveal`), restore all cached** |
| Playwright `edit-and-compare.spec.ts` (Google Chrome) | 1 passed (1.0 min) |
| `scripts/gen_openapi.py --check`, `scripts/gen_schema.py --check` | fresh |

New tests: `ce_core` edit core (20) and translation (16), `ce_behavior` dirty sets (29), `ce_director` edit acceptance (11), API edits (15), orchestrator edit workflows (3), invariants I3 video versions (12), I6 (2), I8 (17), I12 cases for 18 new routes, `tests/e2e/test_edit_mock.py` (1), `make e2e-mock` step 4, Vitest editing helpers and proposal card (12), Playwright `edit-and-compare.spec.ts`; updated: `tests/e2e/test_behavior_mock.py` and `tests/invariants/test_i01_model_swap.py` (typed re-proposal), `ce_build` pinning tests (evaluate hook semantics).

## Fresh-clone verification

| Command (fresh clone of `creator-engine-phase6.bundle`, commit `398433f`) | Result |
| --- | --- |
| `make bootstrap` | pass |
| `make build-images` | api, services and web rebuilt from the clone |
| `make e2e-mock` | pass: cold fixture build 23.2 s, coverage 25/25, 41/41 cached rerun with the identical render, text → previz 8.7 s → approved video ready, **edit: proposal (set_acting + add_behavior_event, voice lock keeps the audio) → apply → derived version ready with the reveal re-performed and the hook 16/16 cache hits → compare (8 spec differences in acting/intent, CBS differs in `scn_reveal` only) → restore v3 entirely from cache hits**, worker kill recovered 39.7 s after the kill |
| `make test-e2e` (Playwright, Google Chrome 154 via `CE_E2E_CHROME`) | 2 passed (3.1 min): create → previz → approve → progress → play (2.1 min); make him more skeptical → proposal → apply → compare (56.4 s) |
| `make lint` / `make typecheck` | pass / pass |
| `CE_REQUIRE_INFRA=1 make test` | 1386 pytest passed, 0 skipped (10 min 22 s) + 50 Vitest passed |

During the first attempt the disk allowance ran out (Docker build cache had grown to 22.6 GB) and SeaweedFS refused writes; `docker builder prune` freed 17.8 GB and the run was resumed from `make e2e-mock` on the same clone.

## Files changed

Added: `packages/py/ce_core/src/ce_core/edit/` (`ops`, `patch`, `locks`, `rebase`, `diff`, `translate`), `ce_director/{edit,proposal}.py`, `ce_behavior/{nodes,evaluate}.py`, `ce_build/world.py`, `ce_db/versions.py`, `ce_exec/{editing,parents}.py`, `ce_testing/edits.py`, `apps/api/src/ce_api/routers/edits.py`, `prompts/edit/v1.md`, `eval/llm_fixtures/edit_*.yaml` (7), `apps/web/src/lib/edits.ts`, `apps/web/src/components/{edit-panel,editors,studio-panels,player}.tsx`, `apps/web/src/app/(app)/videos/[videoId]/compare/page.tsx`, `apps/web/e2e/edit-and-compare.spec.ts`, `scripts/e2e_video.py`, the JSON schemas `edit_operations` and `spec_patch`, tests listed above, `docs/EDITING.md`, ADRs 0040–0041, this report.

Changed: `ce_core` (CBS continuity state/position), `ce_build` (graph, dirty, kinds, manifest, refs), `ce_behavior` (directives, resolve, approximations), `ce_exec` (runtime, executors, behavior, requests, refs loader, planning), `ce_render` (camera post drift), `ce_memory` (`propose_item`), `ce_testing` (fixtures, seed, build), `apps/orchestrator` (models, activities, workflows), `apps/api` (app, jobs, video CLI), `apps/web` (studio page, plan panel, queries, store), `packages/ts/api-client` (OpenAPI, schemas, types), `scripts/{e2e_mock,gen_schema}.py`, docs (API, FRONTEND, INVARIANTS, DECISIONS, README, TODO, ROADMAP, CHANGELOG), `tests/phase0`.

## Known issues and limitations

- **No range drag on the Performance lane** (D77): the Edit panel takes scenes and a time range in seconds.
- **Scene-scoped replan** (`:replan` with `scope`) is recorded only; scene-level changes go through edits.
- **World proposals from edits** are World Studio work (Phase 10): an unmatched world query fails with `no_matching_world` (D69).
- **Acting regeneration without an LLM** keeps the acting plan and re-renders the performance with new seeds, labeled in the proposal (D68).
- **Static group cleanliness probes; patch of a patched avatar** may re-render the avatar (ADR 0041).
- **Estimates** come from the planned routes' recorded costs (D74); they are as good as the router's cost tables (mock engines cost $0).
- **Fixture versions have no plan report** (`ce video generate-fixture` submits specs directly), so their studio shows no Performance lane; planned and edited versions have one.
- **Variants and remixes** answer 501 (V1).

## Next phase and actions

**Phase 7 — Real CPU pipeline: voice, captions, post, analyzers, provenance** (milestone M1), §40:

1. CPU TTS (Kokoro, dev only) and CPU ASR (faster-whisper) behind `CPU_REAL_ENGINES` with coarse alignment; language normalizers, the exact-script verification loop, ProsodyPlan translators for the CPU TTS.
2. Captions: caption styles, safe zones, RTL with the Arabic golden test, SRT/VTT.
3. The full audio mix chain with per-scene world acoustics; camera post and realism post; reframing through the face capability.
4. Screen analysis (scene detection, PP-OCR, the VLM mock, zoom planning).
5. Real CPU analyzers (MediaPipe face/body, prosody features, image embedding, AuraFace, DNSMOS, speaker embedding if CPU-feasible) with smoke tests; `behavior.observe` and the CPU parts of world QC become real; proxy calibration on the fixture clips.
6. Real provenance: C2PA with a test certificate, VideoSeal/AudioSeal adapters on CPU if feasible (otherwise `mock_dev` in dev with an ADR); production still refuses mock provenance.
7. DoD: render golden tests (−14 ± 1 LUFS, true peak ≤ −1 dBTP, captions, aspect variants, Arabic captions, C2PA valid with an untrusted dev root), the exact-script loop with CPU TTS, analyzer tests on fixture clips.

Licensing and downloads are checked against the license policy (non-commercial weights never default); nothing paid is provisioned.

## Post-audit corrections (2026-10, after Phase 14)

- **A2** — applying an edit twice while its job ran created two derived versions and two builds. **A4** — derived version numbers (MAX+1 without a lock).

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-6`).

## Product-audit corrections (2026-10-07)

- EDIT-D1 (a proposal made on one video showed in another's Studio and Apply changed the first), EDIT-D16 (an incomplete time range silently edited the whole video), NL-02 ('make him more skeptical' with nothing selected failed), OUT-ASPECT (a platform or aspect edit did not change the render), BREAK-LONG; duplicate and scene reorder were unreachable (EXTRA-DUP, SCENE-ORDER).

Found by the product-level audit; evidence, regression tests and fixes in [`PRODUCT_LOGIC_AUDIT_REPORT.md`](../PRODUCT_LOGIC_AUDIT_REPORT.md) (corrected in tag `phase-14-audit`).
