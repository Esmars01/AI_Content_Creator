# TODO

Near-term tasks (rule 9). Phases, milestones and the V1/V2/Experimental items are in `ROADMAP.md`. Section numbers (§) refer to `docs/MASTER_BUILD_PROMPT.md`.

Legend: `[x]` done and tested · `[ ]` open · *(deferred)* consciously postponed, with the reason.

## Phase 0 — Environment and bootstrap

- [x] Inspect the environment; write `docs/ENVIRONMENT.md` (`scripts/inspect_env.sh`, `make inspect-env`).
- [x] Verify FFmpeg/libass has HarfBuzz and FriBidi — by rendering an Arabic caption (`tests/phase0/test_toolchain.py`).
- [x] Verify the spec's internal consistency (`scripts/verify_spec.py`, 0 errors); record and fix errata (`docs/SPEC_ERRATA.md`, E1).
- [x] Repository layout of §8: 24 Python packages, 5 Python apps, `apps/web` and `packages/ts/api-client` placeholders, plugin categories, config tree, `prompts/`, `eval/`, `infra/`.
- [x] uv workspace (Python 3.12; `ce_contracts`/`ce_worker` at ≥ 3.10) and pnpm workspace (Node 24).
- [x] Makefile with every §36 target; later-phase targets fail loudly with their phase.
- [x] pre-commit (ruff, ruff-format, mypy, eslint, prettier, verify-spec), CI skeleton, PR template with the rule-20 checklist.
- [x] `.env.example` (every §35 variable, dev-safe) and `docs/ENVIRONMENT_VARIABLES.md`.
- [x] Compose `core` infrastructure with healthchecks: Postgres 18 + pgvector, Valkey, Temporal dev server + UI, SeaweedFS (ADRs 0028, 0029).
- [x] Infrastructure smoke tests: pgvector query, Streams replay, Temporal workflow with the Pydantic converter, S3 put/get/presigned URL, UI over HTTP.
- [x] `make fetch-cpu-assets` (checksum-verified, manifest-driven; empty until Phase 7).
- [x] ADRs 0001–0027 (§6) plus 0028–0029; `docs/DECISIONS.md` with the deviations log; `docs/INVARIANTS.md`.
- [x] `TODO.md`, `ROADMAP.md`, `README.md`, `CHANGELOG.md`, `docs/progress/phase-0.md`.
- [ ] Persist the repository outside the ephemeral build VM (git remote or the owner's machine) — needs the owner's choice.
- *(deferred)* Native fallback stack (local Postgres, `temporal server start-dev` binary, local-filesystem storage) with `make infra-up-native`. Not needed here because Docker works; build it when a machine without Docker appears, or with Phase 1's local `StorageProvider`.
- *(deferred)* CI has not run on GitHub yet (no remote). Run it once the repository is pushed.

## Phase 1 — Domain core, vocabularies, persistence, API skeleton — done (2026-10-03)

Report: `docs/progress/phase-1.md`.

**`ce_core` models (§10, §11, §15–§19)**
- [x] IDs (UUIDv7), prefixed spec keys (§10.5; `prd_`/`as_` D13), enums and status vocabularies (§10.4), error types.
- [x] `SpecPath`: parser, resolver, glob (`[*]` only in lock/vocab patterns), RFC 6901 display rendering.
- [x] Tokenizer and word anchors (Unicode, Turkish/Azerbaijani letters; hypothesis tests).
- [x] VideoSpec: intent, script segments and annotations, scenes, cast with overrides, world bindings, shots and layers, acting plan, products, audio, render, provenance (§11, §14, §15.3).
- [x] CBS (envelope + content, content digest), CompiledBehavior, ObservedBehavior, BehaviorCoverageReport, PlanReport.
- [x] CreatorDNA, AppearanceDNA, VoiceDNA, WardrobeSpec, WorldDNA, MemoryItem, MemorySnapshot.
- [x] Validators (references, tolerances, vocabulary, DNA and world bounds); JSON Schema export (`make gen-schema`); TS types (`make gen-schema`, `make gen-client`).
- [x] Schema snapshot and round-trip tests (VideoSpec, CBS, World DNA, intent).

**Vocabularies and config (§15.2, §35)**
- [x] `config/vocab/*` at `vocab_version 2026.10.1`; loader, validator, lint.
- [x] `ce_config`: layered loader, schemas for every §8 YAML, per-file `config_digest`.
- [x] `ce config validate` (Typer CLI) in pre-commit and CI.
- [x] Production startup checks: dev `SECRET_KEY`, `COOKIE_SECURE`, mock provenance, `MOCK_GPU`, fixture LLM, `local_fs` storage, argon2 floor (DECISIONS D6).

**Persistence (§29)**
- [x] `ce_db`: SQLAlchemy 2.1 async models for all 75 tables; one Alembic migration; up/down test (`make migrate`).
- [x] Org-scoped repositories (I12) and the cross-tenant tests over every tenant table.
- [x] Immutability triggers for approved versioned rows and the `video_versions` identity columns (§12.8, erratum E1) (I3).
- [x] Projection tables rebuilt from spec + manifest (I2 test).
- [x] `ce_storage`: S3 and local-filesystem providers with a shared contract suite (30 cases each; S3 against SeaweedFS). ADR 0030.
- [x] `ce_obs`: structlog JSON (stdlib and uvicorn routed through it), bound job context, trace ids, OpenTelemetry with OTLP/HTTP export and W3C propagation, secret redaction.

**Seed and API**
- [x] `ce seed dev` / `make seed` (idempotent; password from `CE_SEED_ADMIN_PASSWORD` or generated once; placeholder media uploaded); `ce storage init`.
- [x] `apps/api`: `AuthProvider` + password implementation (argon2id, Postgres sessions, CSRF, API keys, login rate limit), members and invitations, projects, videos and versions (read), creators/appearances/wardrobes/worlds/memory with drafts and approve (recorded results only), voices (read), multipart upload with validation, problem+json, OpenAPI, SSE over Valkey Streams. Additions and choices: DECISIONS D25.
- [x] API tests (httpx), tenancy tests, idempotency.
- [x] Control-plane Dockerfile (`make build-images`); `api` and `api-migrate` in the Compose `core` profile (DECISIONS D3).
- [x] World DNA validation and world-versioning tests (drafts editable, approved immutable, promotion, diffs).
- [x] Invariant tests I2, I12 (repositories + API), I3 (identity rows + API; video-version identity columns).
- [x] `docs/progress/phase-1.md`, docs (VIDEOSPEC, WORLDS, CREATORS, MEMORY, API), TODO/ROADMAP update.

**Carried forward**
- [x] Move `asset_validation` and memory `deletion` from in-process tasks into `AssetValidationWorkflow` / `DeletionWorkflow` (ADR 0031) — done in Phase 2.
- [x] Package the built-in storage providers as plugins once the loader exists (ADR 0030) — done in Phase 2.
- [x] Prometheus metrics in `ce_obs` — done in Phase 2 (API, scheduler, orchestrator, render worker, workers).
- [ ] Review the API image's FFmpeg licensing before distributing it (ADR 0017, DECISIONS D27).
- *(deferred)* OIDC `AuthProvider` — V1 (§40).

## Phase 2 — Execution backbone in mock mode — done (2026-10-04)

Report: `docs/progress/phase-2.md`.

**Contracts and plugins (§23, §24)**
- [x] `ce_contracts`: `Adapter`/`AdapterBase` with `run()`, `BehaviorTranslator`, `RunContext`, request/result models for every capability, closed feature sets, behavior-matrix schema, manifest schema with license closure, wire-form `BehaviorDirectives` (Python ≥ 3.10; ADR 0032).
- [x] Plugin loader (`creator_engine.plugins` entry points) with manifest validation and recorded refusals; storage providers and the mock GPU provider packaged as plugins (ADR 0030).
- [x] Mock plugins for every capability producing real media (§37): image, video/B-roll, `mock_avatar_global` and `mock_avatar_segment` with `behavior_track.json`, TTS (+ translator), ASR/align, music, SFX, lip-sync, post, expression, `IdentityTrainer`, embeddings, vision, research, observer, QC, effects, provenance, caption translation; ASS caption renderer.
- [x] Adapter contract suite with translator conformance, run over every installed adapter.

**Build graph, policy, routing (§12, §22, §23)**
- [x] `ce_build`: every §12.1 node kind (incl. `world.plate`, `voice.prepare`, `behavior.keyframe_state`, `behavior.observe`, `qc.world`, `behavior.coverage`, `post.expression`), node keys, content-only cache keys with config digests and `impl_version`, BuildManifest records, route and seed pinning, dirty analysis with impact.
- [x] `ce_policy.license.evaluate` with the operator profile; `ce_router` hard filters, scoring, pinning, fallbacks.

**Execution (§9, §25, §27)**
- [x] `ce_worker`: register, lease long-poll, heartbeat, presigned I/O with hash checks, cancellation, translator application, model-cache skeleton (LRU, pinning, sha256 checks).
- [x] `apps/scheduler`: `gpu_tasks` with `SKIP LOCKED` leasing and placement scoring, priorities, lease reaper (same-seed `infra_retry`), Temporal async completion, cancellation probe, mock GPUProvider, fleet manager with budget cap, cost ledger, advisory-lock leader (ADR 0034).
- [x] `ce_exec` (ADR 0033) and `apps/orchestrator`: `GenerateVersionWorkflow`, `SceneBuildWorkflow`, `RenderWorkflow`, `AssetValidationWorkflow`, `DeletionWorkflow`; behavior nodes as pass-through stubs over the authored acting plan.
- [x] `apps/render-worker`: FFmpeg assembly (base and overlay tracks, titles, ASS captions with word highlight, ducked music, two-pass loudnorm verified with ebur128, encode, 540p proxy), `PROVENANCE_MODE=mock_dev` burned label, visible AI label (`auto` rule D31), SSE progress (`node.updated`, `job.updated`, `render.ready`).
- [x] API: `GET /v1/jobs`, `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}:cancel`, `GET /v1/versions/{id}/manifest`, `GET|POST /v1/versions/{id}/renders`, `GET /v1/renders/{id}`, `GET /v1/renders/{id}/download`; `ce video generate-fixture`.
- [x] Services image (FFmpeg pinned through the base-image digest; ADR 0035); orchestrator, scheduler, render-worker in Compose `core`, `worker-cpu` in `mock-gpu`; `make dev`, `make build-images`, `make e2e-mock`.

**DoD and tests**
- [x] `make e2e-mock`: fixture spec → 1080×1920 H.264/AAC MP4 with captions, music and labels at −14 LUFS in ~22 s on 2 vCPUs; second run 41/41 cache hits with an identical render; killed `worker-cpu` → lease expiry → same-seed retry → build completes.
- [x] Route-pinning test (router weights change, clean nodes keep routes); production refuses `PROVENANCE_MODE=mock_dev`; scheduler and router scoring tests.
- [x] Invariant tests I5, I11, I14; workflow tests on the Temporal dev server (ordering, cache short-circuit, render queue, partial failure, retries, cancellation, plan failure, resume on another worker, replay determinism).

**Carried forward**
- [x] Behavior nodes are stubs: the compiler, translators' coverage, observation and the triad are Phase 3 — done in Phase 3.
- [x] QC records measurements but does not gate or retry — the QC gate and decision ladder execute since Phase 11 (ADR 0056).
- [x] Camera motion, lens, grain and realism post are pass-through (Phase 7); overscan margin likewise — done in Phase 7 (ADR 0048).
- [x] Model-cache fetchers (`hf://`, `url://` with mandatory sha256, `s3://`) and OOM escalation to a larger VRAM class — done in Phase 8; real GPU providers and autoscaling — Phase 9.
- [x] Pool overhead: a stopped worker's provisioned, busy and idle time is recorded in `fleet_costs` (Phase 9, D105); failed attempts' GPU time is still not billed to the org (their time shows up as worker idle overhead).
- [ ] Per-service images instead of the shared services image — Phase 14 (ADR 0035).
- [ ] FFmpeg distribution review for the images (ADR 0017, D27) — owner decision before distributing.

## Phase 3 — Behavior backbone — done (2026-10-04)

Report: `docs/progress/phase-3.md`. ADR 0036, DECISIONS D35–D48, `docs/BEHAVIOR.md`.

- [x] Acting-model validators: states tile, intensity jumps and turns between incompatible emotions need triggers, triggers sit inside their state, masking needs felt ≠ displayed, strategies that express nothing of the emotion warn; the emotional trajectory and the seconds-to-states mapping with reported drift (`ce_behavior.acting`).
- [x] `behavior.resolve`: CBS resolver with the §10.6 resolution order, DNA bounds and avoidance caps, habits from the pinned memory snapshot, the world behavior digest and affordances; `requested_controls` with priorities, planner confidence and observation reliability (D35–D38).
- [x] Behavior compiler: realization methods and preference order, plan-time proposals (splits, punch-ins, cutaways over events) and build-time realizations from existing elements (D39), ProsodyPlan and VisualPlan, predicted coverage, coverage downgrades against the planned route; whole-version planning without persistence (`ce_behavior.plan`).
- [x] Translators for the mock engines with golden files; every realization encoded or reported unsupported with a reason (contract suite + `test_translator_golden.py`).
- [x] `behavior.observe` (measurement only, chunk stitching, behavior signature); judgement with implemented proxies (D41); the 12 viewer-level outcomes; `config/qc/behavior.yaml` decisions recorded (D40) and the behavior score in take ranking (D44); `behavior_observations` (take and viewer level); `model_behavior_profiles` from mock data (D42).
- [x] Router: a declared inability scores 0 for requested dimensions (D43).
- [x] Re-route: planning flags `approximations_stale` and records an open edit proposal for stale `compiler_approximation` elements (D45).
- [x] API: `GET /v1/versions/{id}/behavior`, `GET /v1/versions/{id}/coverage`, `GET /v1/takes/{id}/observations` (D47); I12 cases; OpenAPI and TS client regenerated.
- [x] Invariant tests I1 (model swap, schema lint, coverage upgrade, re-route flag) and I4 (completeness, honest HONORED, triad); behavior suites; e2e behavior tests on the stack.
- [x] `make e2e-mock` reports requested, compiled and observed entries for every CBS item of the fixture spec (25/25), with matching viewer-level rows.

**Carried forward**
- [x] Executing the Performance QA ladder (retries with new seeds, fallback routes, `needs_review`) — done in Phase 11 (ADR 0056).
- [x] Load measured profiles into the live router catalog with a per-version profile snapshot, so rebuilds stay deterministic — done in Phase 8 (registry overlay + per-version snapshots, ADR 0053).
- [ ] VLM window proxies and the audio-emotion classifier in judgement: the `audio_emotion` adapter exists in the sandbox (Phase 11, D126) and the VLM judge runs per take, but window questions and emotion proxies are still NOT_MEASURABLE in judgement until a real VLM / a licensed emotion model is routable. Pitch tracks are real since Phase 7 (`prosody_features`).
- [ ] Real CPU analyzers on licensed fixture clips (`FIXTURE_CLIPS_DIR`): the analyzers and the fixture test exist since Phase 7; the clips must come from the owner (consent or a permissive license, §16.7) — **owner input**.
- [ ] Applying the stale-approximation proposal (typed `EditOperation`, propose/apply) — Phase 6.
- [x] Director stage 11 applies plan-time proposals to the spec with `derived_from: compiler_approximation` — done in Phase 4.
- [x] Acceptance fixtures of §15.5 (the six situational examples) through the Director — done in Phase 4.

## Phase 4 — AI Director, intent, acting, memory retrieval, previz — done (2026-10-04)

Report: `docs/progress/phase-4.md`. ADR 0037, ADR 0038, DECISIONS D49–D58, `docs/DIRECTOR.md`, `docs/MEMORY.md`.

- [x] `ce_llm`: `LLMProvider` registry with the `fixture`, `openai_compatible` and `anthropic` provider plugins, fixture replay by scenario key, the recorder, `structured()` with targeted repairs (the repair turn carries model output as data), versioned prompt templates with the `data` filter (I10).
- [x] `ce_voice`: the acting-tag parser and exact-script extraction with byte-equality checks; property tests (Unicode, punctuation, Turkish and Azerbaijani letters).
- [x] Director stages 1–11 (`ce_director`): interpret, context, research, strategy, script (duration fitting with seeded WPM), fact check, scenes and world binding, situational acting with the stage-8 route preview, intent policies with traceability, finalize (validation, blocklists, testimonial guard, CBS, plan-time routing and compile with `compiler_approximation` elements, predicted coverage, the plan report); `director_runs` for every stage; nearest-token mapping with assumptions (I13); the template Director for dev input (§37).
- [x] Memory: `MemoryRetriever` (structured + keyword) into immutable snapshots, the repetition guard (keyword and n-gram), the contradiction checker (canon and persona memory), usage events when a version reaches `ready`.
- [x] Safety and research: blocklists and the testimonial guard (rules + LLM classifier with fixtures, D57); in-memory research on pasted text and URLs through the SSRF-guarded fetcher with keyword retrieval (ADR 0038, D56).
- [x] Workflows: `PlanVideoWorkflow` → `PrevizWorkflow` (cached subset of the build, measured timings) → `previz_ready` → `:approve` → `GenerateVersionWorkflow` (D51–D53).
- [x] API: `POST /v1/projects/{id}/videos`, `GET /v1/versions/{id}/previz`, `GET /v1/versions/{id}/intent`, `POST /v1/versions/{id}:replan`, `POST /v1/versions/{id}:approve`, `claims_summary` from the fact check (D54, D55); I12 cases; OpenAPI and TS client regenerated.
- [x] Acceptance fixtures for §2's creation examples, §15.5's behavior examples and §15.4's trajectory; the invalid-JSON repair test; prompt-injection fixtures; invariant tests I7, I10, I13.
- [x] `tests/e2e/test_plan_mock.py` and `make e2e-mock` step 3: text → plan → previz → approve → ready video, with previz outputs reused from the cache.

**Carried forward**
- [ ] Scene-scoped `:replan` (`scope`) and propose/apply edits — Phase 6.
- [x] Persistent research sources (`sources` on create, `POST /v1/projects/{id}/sources`), embeddings and the claim ledger with `POST /v1/claims/{id}:override` — done in Phase 12.
- *(unscheduled)* Testimonial-guard review UI and the quote-consent flow — planned for Phase 13, which the owner removed (ADR 0050).
- [x] Director stage 12 — done in Phase 12 as packaging (§13 stage 12); the critique is Phase 11's Creative Director.
- [ ] Real LLM providers in CI (recording fixtures needs a key and owner approval for spend) — owner decision.
- [x] Embedding similarity in the vocabulary mapper and the repetition guard — done in Phase 12 (same-model vectors only; keyword fallback while no real `embed.text` model is registered).
- [x] The previz screen — done in Phase 5 (previz audio playback on its own: with the player in Phase 6).

## Phase 5 — Frontend MVP shell — done (2026-10-04)

Report: `docs/progress/phase-5.md`. ADR 0039, DECISIONS D59–D65, `docs/FRONTEND.md`.

- [x] `apps/web`: Next.js 16 App Router, React 19, TypeScript strict, Tailwind CSS 4, shadcn-style components, TanStack Query, Zustand, `openapi-fetch` on the generated client; same-origin `/api/*` gateway with cookies, CSRF and SSE (D60).
- [x] Auth (login, session guard, sign out) and the §31 navigation (later sections labelled with their phase, D62).
- [x] Dashboard (recent videos with states, live running jobs, memory conflicts, honest placeholders) and Projects.
- [x] Create: the ten-step stepper from `GET /v1/create-options`, then the previz review (script chips, storyboard, intent summary, Performance timeline, predicted coverage, memory, repetition / contradiction / fact-check findings, duration and cost, Approve with overrides, Regenerate plan).
- [x] Video Studio v1: player, job progress over SSE, versions list, read-only Performance lane, scenes with world and Simple coverage badges, intent, coverage table (Advanced), creator and world.
- [x] Creators and Worlds (read-only DNA), Jobs (list and detail), Developer viewers (VideoSpec, CBS, plan report, BuildManifest).
- [x] API additions: create options, storyboard, version state on videos, event timings in the plan report (D59).
- [x] Vitest (badge contract, lane, SSE invalidations, client, create form, gateway); I9 invariant test; Playwright create → previz → approve → progress → play (D61); `web` in Compose `core` (D65); CI jobs for web unit and e2e tests.

**Carried forward**
- [x] NL edit panel, proposal cards, locks, takes gallery, versions tree with compare — done in Phase 6. Timeline tracks beyond the Performance lane: later phases.
- [ ] DOM caption overlay in the player (the captions list/download endpoints exist since Phase 7).
- [ ] Keyboard shortcuts in the studios (§31).
- [ ] Server-side user preferences (Simple/Advanced) instead of browser storage (D63).
- [x] Creator Studio and World Studio editing, Creator Memory UI — done in Phase 10; GPU page — Phase 9. Assets, Templates, Models and Settings pages — their phases.
- [ ] Trust `X-Forwarded-For` from the web service for login rate limiting in deployments (`FORWARDED_ALLOW_IPS`), ADR 0039.

## Phase 6 — Incremental editing — done (2026-10-04)

Report: `docs/progress/phase-6.md`. ADRs 0040–0041, DECISIONS D66–D77, `docs/EDITING.md`.

- [x] `ce_core.edit`: the 38-operation `EditOperation` union with slots, SpecPatch (key-addressed set/add/remove), lock checks with scope expansion (I8), regenerate refusals, anchor rebase, the structured spec diff, operation → patch translation.
- [x] Reference resolution (`@selection`, `@first_Ns`/`@last_Ns`, `@scene_n`, `@him`/`@her`/`@cast`, `@emphasis`, `@span_start`, event anchors) and record queries; the `edit` stage with fixtures for every §2 edit example and a labeled template planner (D66).
- [x] `compute_proposal`: patch, validation (schema, references, vocabulary, locks, blocklist), impact with cascade and `no_visible_effect`, predicted coverage delta (`blocked_by_lock`), alternatives (editorial only, lip-sync patch, full re-performance) with estimates.
- [x] Behavior-aware dirty propagation: generation digests, the behavior evaluator, output-change propagation, per-position plate views (§19.5), the voice lock's content-pinned outputs (D72), pacing editorial vs re-performance, lip-sync patch builds (ADR 0041); exact dirty-set tests.
- [x] `ProposeEditWorkflow` / `ApplyEditWorkflow` and their activities; wrappers auto-apply (D67); acting regeneration as a Director edit (D68); the derived-version factory with planned routes and a derived plan report (D71); recompute on apply (D76); re-route re-proposals with typed operations (I1).
- [x] API: edits (create, list, get, apply, reject), locks, scene/shot regenerate (synchronous 409 naming the lock), re-route, take selection, takes gallery, resume, branch, restore, duplicate (D70), compare (D75), estimates (D74), vocabulary (D73), `:variants`/`:remix` 501.
- [x] Studio: NL edit panel and proposal cards, Advanced performance (states, events) and intent editors, locks, shots and takes, versions tree (restore, branch, resume), compare page with synchronized players.
- [x] Tests: edit core and translation units, dirty sets, edit acceptance fixtures, API (15), workflows (3), invariants I3 (video versions), I6 (scene overrides), I8, I12 cases for every new route, e2e on the stack (`tests/e2e/test_edit_mock.py`, `make e2e-mock` step 4), Vitest (editing helpers, proposal card), Playwright `edit-and-compare.spec.ts`.

**Carried forward**
- [ ] Range selection by dragging on the Performance lane (D77); the Edit panel takes scenes and a time range in seconds.
- [ ] Scene-scoped replan (`:replan` with `scope` is recorded only).
- [ ] World proposals from planning and edits when no approved world matches (D69, §19.2: a draft world bound to the version, `needs_world_approval`) — not built in Phase 10; carried to Phase 11/12 follow-ups.
- [x] Applying critique findings as edit proposals (`from_critique_finding`) — done in Phase 11; templates (`spec-templates:apply`) — done in Phase 12.
- [ ] Group cleanliness probes are static; a patch of an already patched avatar may re-render the avatar (ADR 0041).
- [ ] Variants and remixes (`:variants`, `:remix` answer 501) — V1.

## Phase 7 — Real CPU pipeline: voice, captions, post, analyzers, provenance — done (2026-10-04)

Report: `docs/progress/phase-7.md`. ADRs 0042–0049, DECISIONS D78–D89.

- [x] `CPU_REAL_ENGINES` (auto/on/off), CPU engine assets in manifests, `make fetch-cpu-assets` (14 pinned, checksummed, license-checked files), startup checks, real beats mock, `prefer_mock`, the `cpu_local` pool (ADR 0042).
- [x] Voice: Kokoro CPU TTS (dev only, ADR 0043) with the `kokoro_v1` translator; faster-whisper transcription, coarse alignment and LID; normalizers for 9 languages; the exact-script loop with WER/CER, attempt-seed retries and `needs_review` (ADR 0044).
- [x] Captions: bundled Noto fonts with glyph-coverage fallback, RTL (Arabic golden), safe zones, ASS/SRT/VTT recorded as `captions` rows; `GET /v1/versions/{id}/captions`, `GET /v1/captions/{id}/download`.
- [x] Post: seeded camera motion, focus hunts, exposure drift, motion blur, subject-aware reframing through `face.detect` with the blurred-fill fallback, realism post, per-scene world acoustics (mic chain, IR, de-ess, room tone), true-peak codec headroom (ADR 0048).
- [x] Analyzers: MediaPipe face and body, prosody features, DINOv2 image embedding, AuraFace, DNSMOS, PP-OCR (all `smoke_passed` on real weights); real `behavior.observe`; CPU world QC (identity, lighting).
- [x] Calibration: `ce_behavior.calibration`, storage in `model_behavior_profiles` (ADR 0045), `make calibrate-analyzers`; speech-rate proxies calibrated on synthetic speech (`eval/calibration/speech-v1.json`).
- [x] Provenance: C2PA signing with a generated dev CA, `provenance.verify`, `GET /v1/renders/{id}/verify` (ADR 0046); watermarks measured on CPU and kept `mock_dev` in dev (ADR 0047).
- [x] Screen recordings: `ScreenAnalysisWorkflow` (scene changes, keyframes, PP-OCR, text and pixel diffs, dead time, mock VLM summary), `GET /v1/assets/{id}/screen-analysis`, zoom/speed/bubble planning for screen shots added by edits, real `screen.prepare` and the webcam bubble (ADR 0049).
- [x] Tests: render goldens (loudness, true peak, captions, aspect variants, Arabic, C2PA), exact-script e2e (mock misreads), real-engine e2e, screen e2e, analyzer tests, planner tests, API tests for verify/captions/screen analysis, I12 cases.

**Carried forward**
- [x] VideoSeal and AudioSeal adapters in the `post` GPU family — done in Phase 8 (sandbox, `untested_on_gpu`; D96).
- [x] Speaker-verification embedding (`voice.embed`) with a license-verified checkpoint — done in Phase 8 (`speaker_ecapa`, sandbox).
- [x] PP-OCRv6 once a verified ONNX export exists — done in Phase 8 (D99).
- [ ] The Director creating screen shots from a plan request (a screen-asset field in the create body; tutorial modes with screen-only base tracks) — needs a product decision on the §30 body (D86).
- [ ] Reaction clips: the source range and `pip`/`split` layout (the asset is reused as is).
- [ ] Visual proxy calibration on owner-supplied fixture clips (`FIXTURE_CLIPS_DIR`) — **owner input**.
- [ ] Assets page with the screen-recording analysis view (§31) — with the Assets UI.
- [x] Captions translation review (`:translate`, `:approve`) — done in Phase 12.


## Phase 8 — Real GPU adapters (sandbox until validated) — done (2026-10-04)

Report: `docs/progress/phase-8.md` (historical handoff of the interrupted session: `docs/progress/PHASE_8_HANDOFF.md`). ADRs 0050–0053, DECISIONS D90–D102.

- [x] `ce_plugin_kit` (ADR 0051): `EngineAdapter` / backend split, `test_backend` stand-ins, `ModelPaths`, media/speech/align/translate/gpu helpers.
- [x] 28 GPU adapters for every "8" row of §39.7, all `sandbox` / `untested_on_gpu`, with license blocks for the model and every dependency at pinned revisions, behavior matrices, translators with golden tests, `estimate()`, `voice.prepare` conditioning for every TTS adapter; contract suite on CPU stand-ins.
- [x] Manifest additions: `test_backend`, dependency `role/source/files/sha256`, license `text_sha256/evidence`, `use_restrictions` and `copyleft` conditions, `size_gb` measured at the pins (HF API).
- [x] Model cache fetchers (`hf`, `url` with mandatory sha256 pins, `s3`), subset-aware shared cache dirs, `ensure_plugin_models` → `model_paths`, `CE_ADAPTER_DEFAULTS`; scheduler OOM escalation to the next VRAM class.
- [x] Registry in the database (`ce_db.registry`): sync, overlay (per-adapter aggregation over its models), promote, disable, record validation, knob calibrations; orchestrator sync at startup; TTL refresh; per-version registry snapshots (I5, ADR 0053).
- [x] API: `GET /v1/models`, `GET /v1/models/{id}` (evidence, knob curves, benchmarks); admin `:promote`, `:disable`, `:calibrate`, `/validations`, `/calibrations`, `/benchmarks`; rule-5 guards (stand-in reports, wrong adapter or kind, inconsistent verdicts, hand-set `bench_passed` refused); tests; OpenAPI and TS client regenerated.
- [x] Calibration: `ce_behavior.knobs`, `ce_exec.calibration`, `CalibrationWorkflow` + `POST /v1/admin/models/{id}:calibrate` (runs on `cpu_model` engines; GPU families get `needs_gpu_host`), tested end to end on the mock avatar through Temporal.
- [x] `eval/smoke` golden set `smoke-v1` (cases for every capability, sha256-pinned synthetic media incl. Kokoro and eSpeak NG speech, behavior fixtures) and the smoke-promotion flow (README).
- [x] Smoke/bench/calibrate harness (`ce_worker.validation`, `scripts/{smoke,bench,calibrate}/run.py`, `make smoke-gpu|bench|calibrate PLUGIN=…`, `--record`); dry runs on stand-ins for all 28 adapters; real runs on the CPU engines (`smoke_passed`).
- [x] GPU family Dockerfiles generated from the manifests (`infra/docker/families.yaml`, `scripts/gen_worker_dockerfiles.py`, 9 families, 21 variants), lint-checked; `python -m ce_worker` entrypoint for family images (ADR 0052).
- [x] `scripts/verify_licenses.py` (offline in tests; `--online`: 101 blocks, 89 evidence checks re-verified at the pins, 0 mismatches); Phase 7 branch-URL license blocks re-pinned (D100); `make verify-licenses`.
- [x] PP-OCRv6 small through RapidOCR (D99).
- [x] I11 production check requires production-routable watermarkers; I12 platform routes; 3 handoff test failures and 15 mypy errors fixed.
- [x] Phase 13 removed from the plan (ADR 0050, SPEC_ERRATA P1); `docs/MASTER_BUILD_PROMPT.md` = v2_private + E1.
- [x] Docs: MODELS (generated), GPU_VALIDATION, GPU_SETUP, MODEL_INSTALLATION, ADAPTERS, PLUGINS, ENVIRONMENT, INVARIANTS, DECISIONS, eval/smoke README.

**Carried forward**
- [ ] Run every GPU adapter's smoke on a GPU host and record it (docs/GPU_VALIDATION.md) — needs a GPU (owner: local host or approved RunPod spend; Phase 9 providers).
- [ ] Build the family images with their CUDA stacks and confirm the [RV] torch pins — needs a GPU host / more disk than this session's allowance.
- [ ] RIFE 4.25 weights mirror with a sha256 pin (D95) and the SyncNet weights' license (D94) — **owner input**.
- [ ] MuseTalk weights' CreativeML OpenRAIL-M use restrictions reviewed before promotion — **owner input**.
- [x] Bench-based promotion (`bench_passed`) — done in Phase 11 (benchmark runner, blind pairwise rating; promotion requires it).
- [ ] Dispatching `CalibrationWorkflow` renders of GPU engines to the fleet (today: `scripts/calibrate` on a host) — not built in Phase 11; needs a GPU to validate.

## Phase 9 — Fleet and providers — done (2026-10-04), live provider runs pending

Report: `docs/progress/phase-9.md`. ADR 0054, DECISIONS D103–D108.

- [x] GPU providers: `local_docker` (Docker Engine API, NVIDIA device request), `runpod_pod` and `runpod_serverless` (REST API v1; request shapes checked against RunPod's OpenAPI document; paid → inert until `allow_paid`), `example_cloud` (third-party stub through its own entry point).
- [x] Fleet manager: providers from `gpu_providers` rows (`config`, `credentials_ref`) over manifest defaults; one-time enrollment token per provisioned host; provider → class → region fallback (no capacity, errors, driver filter); image variants (`config/gpu/variants.yaml`); provisioning timeouts; idle terminate/stop with restart; static workers never stopped.
- [x] Budgets: spend report and projection, `budget_daily` / `budget_video` / `budget_project` holds (leasing skips held tasks), provider daily caps, `budget_alert` notifications and `budget.alert` events with a cooldown.
- [x] Cost actuals: `fleet_costs` (append-only) per stopped worker period; idle overhead separated from org billing (D105).
- [x] Metrics: cold start, model load and fetch, lease wait, fleet spend, provisions by result, held tasks; measured seconds per work unit feed the dispatcher's backlog estimate.
- [x] Model cache on shared network volumes (`flock` per download, merged manifest, eviction grace).
- [x] Worker enrollment for self-managed hosts (`POST /v1/admin/gpu/workers:enroll`).
- [x] API: `GET /v1/gpu/pools|workers|offers`; admin providers (create, list, PATCH with noted paid approval), provision, stop, enroll, queue; scheduler internal fleet endpoints; I12 route coverage; TS client regenerated.
- [x] Web: GPU page (pools, workers, offers; admin queue and spend, provision/enroll, providers).
- [x] Migration 0002 (fleet columns, holds, provider config, enrollment link, `fleet_costs`).
- [x] Fixed: a worker the fleet stopped could revive its row through its next lease (the scheduler now refuses it).

**Carried forward**
- [ ] A live RunPod run (provision → smoke task → stop, costs recorded, under `SMOKE_SPEND_CAP_USD`) — needs `RUNPOD_API_KEY` and the owner's spending approval.
- [ ] `local_docker` with a real GPU (the NVIDIA device request is untested) — needs a GPU host.
- [ ] Milestone M2 (a GPU-validated golden video on smoke-promoted routes) — needs a GPU.
- [ ] `flock` semantics on each provider's network volume [RV].
- [ ] Failed attempts' GPU time is not billed to orgs (it appears as fleet idle overhead).
- [ ] Vast.ai and SkyPilot providers — V1.

## Phase 10 — Creator Studio and World Studio — done (2026-10-05) in mock mode

Report: `docs/progress/phase-10.md`. ADR 0055, DECISIONS D109–D115.

- [x] Studio jobs (ADR 0055): one loop, model calls routed and dispatched to the scheduler like build nodes (migration 0003), outputs as assets, drafts only.
- [x] Identity packs: candidates → canonical face → angles and expressions conditioned on it → identity scores → VLM apparent-age check → review (incl. an approver's age review) → approval.
- [x] Wardrobe references conditioned on the canonical face, scored.
- [x] Voices: design → candidates → select → draft version; test bench (WER, CER, WPM, speech quality, speaker similarity); lexicon editor; WPM calibration from real engines only; approval.
- [x] Creator Test: the fixed ~20 s script (`ce_creator.test_script`), the test video through `GenerateVersionWorkflow`, the scorecard (`ce_creator.scorecard`), ratings, history, baselines.
- [x] World Studio: plate candidates, choices with fingerprints (`ce_world.plates`), approval, version diffs, override promotion as a new draft (I6 test), continuity report.
- [x] Consent data model and endpoints; flows off until V1 (D113).
- [x] Web: Creator Studio tabs (Overview, DNA editor with sections, Memory with pin/forget/supersede/resolve, Appearance, Voice, Wardrobe, Creator Test, Consent) and World Studio tabs (DNA with floor plan, Plates, Versions, Continuity).
- [x] DoD e2e in mock mode: creator → test → approve; world → plates → approve → used in a video (`tests/e2e/test_studio_mock.py`); World Studio in the browser (Playwright).

**Carried forward**
- [ ] Real engines for every studio job (image, image edit, VLM, voice design, TTS, embeddings) — the code paths are engine-independent; real runs need GPU hosts (Phase 8/9 adapters, no GPU here).
- [ ] Creator Test VLM critique and identity scores on real engines; lip-sync stays advisory until its license is resolved.
- [x] Rolling baselines across videos and the consistency charts — `ConsistencyWorkflow` and Creator Studio → Consistency (Phase 11).
- [ ] Behavior history tab (coverage and observation outcomes across videos): the consistency grid covers behavior patterns per video; an outcome-by-outcome history view is not built.
- [ ] Consent verification (face/voice match, phrase check) and envelope encryption of consent media — V1.
- [ ] A visual floor-plan editor (drag elements); today the plan is drawn from the DNA and edited as JSON sections.

## Phase 11 — QC gate, Performance QA, continuity, Creative Director, benchmarks — done (2026-10-05) in mock mode

Report: `docs/progress/phase-11.md`. ADR 0056, DECISIONS D116–D127, `docs/QC.md`, `docs/CONSISTENCY.md`.

- [x] Per-node checks with thresholds keyed by metric adapter; the VLM judge (structured prompts, versioned); render QC flags.
- [x] The decision ladder per shot with per-node and per-version budgets in count and USD; only the failing take's nodes re-run; fallback routes; cheaper fixes proposed; best attempt kept on review; deferred BuildManifest rows.
- [x] Performance QA wired to the gate (take level) and to review flags (viewer level); the outcome matrix test.
- [x] World continuity: VLM element/relation check, background continuity across shots, cross-video world score in consistency.
- [x] `ConsistencyWorkflow` after every build, rolling baselines, the human rating queue, Creator Studio → Consistency.
- [x] QC report UI with the requested/compiled/observed triad.
- [x] Creative Director critique → findings → edit proposals (apply cleanly); `AutonomousSuggestWorkflow` stub (flag off).
- [x] The golden evaluation set, the benchmark runner, blind pairwise rating, bench profiles (measurements), bench-based promotion.
- [x] `audio_emotion` adapter in the sandbox.
- [x] Calibration of low-reliability checks (behavior proxies, world checks) against human ratings.

**Carried forward**
- [ ] Real-engine thresholds, judge prompts and benchmarks on GPU hosts — no GPU here; every threshold for real adapters other than DNSMOS/SyncNet is unset (report-only).
- [ ] VLM judge crops of faces and hands and a cheaper triage tier (D118).
- [ ] VLM window questions for behavior proxies (still NOT_MEASURABLE in judgement) and emotion proxies from `audio_emotion` once licensed.
- [ ] An LLM critic over the same evidence (D123).
- [ ] Bench success rates from judged behavior fixtures (D125).
- [ ] Executing a cheaper fix (lip-sync patch) automatically — today it is proposed (D117).
- [ ] `ConsistencyWorkflow` face/hand size and camera habits need landmarks in the observation summary; they are often not measured.

## Phase 12 — Memory loop, research, templates, brand, packaging and exports — done (2026-10-05) in mock mode

Report: `docs/progress/phase-12.md`. ADRs 0057–0059, DECISIONS D128–D140, `docs/MEMORY.md`, `docs/DIRECTOR.md`, `docs/API.md`.

- [x] `MemoryUpdateWorkflow` (ADR 0057): approved → persona proposals; ready → hook embedding and observation habits (merged per video, thresholds, never from mock evidence); exported → activation and the usage event; edit_applied → preference proposals; embed → item vectors; conflict links and notifications; keep both with creator-version scopes.
- [x] Embeddings: `embed.text` in-process with the model recorded on every vector; retrieval, the repetition guard (hooks) and research retrieval use cosine only within one model, else keyword paths; migration 0004 (model columns, HNSW on hook embeddings).
- [x] Research (ADR 0058): `ResearchIngestWorkflow` (SSRF-guarded URLs with ingestion limits, PDF/DOCX/HTML/text/SRT/VTT uploads, notes), facts with spans, hybrid retrieval, claim checks in code with detected claims, closed book = user's own sources, the claim ledger with overrides, export blocking; research and claims API.
- [x] Spec templates (slots, strict bodies, compose with conflicts, apply as edit proposals, immutable versions) and brand kits (logo, colors, fonts, caption style; project default; logo burned by `render.final`, content digest in the cache key) (ADR 0059).
- [x] Caption translation: `captions_llm` (configured LLM, data-wrapped, index checks), `:translate` as an auto-applied edit, review per language bound to the file content.
- [x] Director stage 12 packaging (`PackagingWorkflow`): labelled design-default limits, LLM with repairs or the labelled template, render-frame thumbnails; packaging edit/approve; exports with every rule, metadata artifact with license obligations, memory `exported` job, audit, artifact refs.
- [x] Web: research sources, claim ledger, memory provenance and scoped conflicts, templates page, settings with brand kits, captions/packaging/export panels in the Video Studio, sources in the Create wizard.
- [x] Tests: memory loop units and e2e, research extraction/ingest/claims units, Director closed-book tests, research/templates/exports API tests, I12 cases for all 24 new id routes, packaging-limit and export-preset tests, thumbnail and logo render tests, captions_llm tests, e2e research and packaging/translation/export through Temporal, Vitest helpers and panels.

**Carried forward**
- [ ] A real `embed.text` model: pin a revision, verify its license at it, fetch the ONNX export, add `plugin.yaml` and the entry point to `plugins/embed/text_embed`, run the contract suite on the files — needs network access to the model host.
- [ ] `captions_llm` smoke run against a real provider (`validation` stays `untested_on_gpu`) — needs an API key and the owner's spend approval.
- [ ] Platform limits: verify each platform's title/description/hashtag limits against official sources and set `verified_at` (D16); packaging uses labelled design defaults until then.
- [ ] Video research sources (speech recognition of uploaded videos) — refused today.
- [ ] Font uploads for brand kits (fonts are recorded by name only).
- [ ] An LLM entailment check behind `ce_research.claims.support` (the code check is conservative).
- [ ] Non-English stopwords for the template packaging's keyword hashtags.

## Phase 14 — Hardening and documentation — done (2026-10-06) in mock mode

Report: `docs/progress/phase-14.md`. ADR 0060, DECISIONS D141–D148.

- [x] Observability: API metrics and spans, tracing in every service, `ce_db.ops_metrics`, side-port metrics, the `observability` profile with Prometheus, alerts, Alertmanager, Loki, Tempo, the collector and generated dashboards; verified live against natively run services.
- [x] Scheduler load test with measured results; fair-share leasing (migration 0005).
- [x] Security review: dependency audits, route permissions, web headers, client address, metrics exposure; `docs/SECURITY.md`.
- [x] Backup, restore and verify (`scripts/backup.py`), two drills; deployment and operations docs; native mode.
- [x] Demo (`make demo`), Playwright on Chromium (four specs pass; playback needs Google Chrome).
- [x] Fixes: delivered true peak, consistency features, Phase 12 omissions.
- [x] Docs: the §38 set completed and reviewed against the code.

**Carried forward**
- [ ] Carry the trace context across the Temporal boundary (node spans start their own traces).
- [ ] A likeness check against `protected_persons`; non-empty default blocklists for the operator's jurisdiction.
- [ ] Retention jobs that apply `retention_policies`.
- [ ] Rate limits beyond logins (plans, uploads, research fetches).
- [ ] A nonce-based CSP without `'unsafe-inline'` scripts.
- [ ] Face size in frame for `camera_habits.face_size_ratio` (no analyzer reports it).
- [ ] Label Loki log streams by Compose service (today they carry the container id).
- [ ] Kubernetes manifests (`infra/k8s/` is a placeholder).
- [ ] Load-test several scheduler replicas and real GPU workers.
- [ ] Run the playback step of `create-to-play.spec.ts` with Google Chrome (not downloadable here).
