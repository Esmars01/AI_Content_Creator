# Roadmap

Phases, milestones and post-MVP tracks (rule 9). The authoritative scope and Definitions of Done are in `docs/MASTER_BUILD_PROMPT.md` §39–§40; near-term tasks are in `TODO.md`. Work proceeds strictly in phase order (rule 3).

Status values: **done** (DoD passed and tested) · **in progress** · **not started**.

Every phase ends with: all tests green (including the invariant tests that exist so far), `make dev` working in mock mode, docs updated, `docs/progress/phase-N.md`, TODO/ROADMAP updated, commits made.

## Phases

### Phase 0 — Environment and bootstrap — **done** (2026-10-03)
Environment inspection, repository layout, uv/pnpm workspaces, Makefile, pre-commit, CI skeleton, Compose `core` infrastructure with healthchecks, ADRs 0001–0029, docs. DoD: `make bootstrap && make infra-up && make test` pass and services are healthy. Report: `docs/progress/phase-0.md`.

### Phase 1 — Domain core, vocabularies, persistence, API skeleton — **done** (2026-10-03)
`ce_core` models (VideoSpec, CBS, DNA, memory), vocabularies at `vocab_version 2026.10.1`, `ce_config`, `ce_db` with all §29 tables and the immutability triggers, `ce_storage` (S3 + local, contract-tested on SeaweedFS), `ce_obs`, `ce seed dev`, the API skeleton with auth, uploads and SSE, the api image in Compose `core`. Invariants I2, I12 (repositories + API), I3 (identity rows + API). Report: `docs/progress/phase-1.md`.

### Phase 2 — Execution backbone in mock mode — **done** (2026-10-04)
Contracts and plugin loader (ADR 0032), mock plugins producing real media, `ce_worker`, the scheduler with leases and Temporal async completion (ADR 0034), `ce_build` with content-only cache keys and route/seed pinning, license policy and router, `ce_exec` with node output documents (ADR 0033), Temporal workflows, FFmpeg rendering with mock provenance, the services image and Compose services (ADR 0035). DoD: `make e2e-mock` renders the fixture spec in ~22 s on 2 vCPUs, the second run is 100 % cache hits, a killed worker's task completes after lease expiry; route pinning, I5, I11, I14 and workflow tests pass. Report: `docs/progress/phase-2.md`.

### Phase 3 — Behavior backbone — **done** (2026-10-04)
`ce_behavior` (ADR 0036): the CBS resolver with the §10.6 resolution order, the engine-agnostic compiler (plan-time proposals, build-time realizations, predicted coverage), mock translators with golden files, measurement and judgement of observed behavior, the requested/compiled/observed triad with the 12 outcomes, recorded Performance QA decisions wired to take ranking, `behavior_observations` and measured mock profiles, stale-approximation flagging after a re-route; behavior and coverage API endpoints. DoD: the behavior suites marked [3] pass; `make e2e-mock` reports requested, compiled and observed entries for all 25 CBS items of the fixture spec. Invariants I1, I4. Report: `docs/progress/phase-3.md`.

### Phase 4 — AI Director, intent, acting, memory retrieval, previz — **done** (2026-10-04)
`ce_llm` (fixture, OpenAI-compatible and Anthropic providers, scenario replay, recorder, structured output with targeted repairs, prompt templates with data blocks), `ce_voice` tag parser and byte-exact script extraction, `ce_director` stages 1–11 with intent policies, situational acting, plan-time compile and the template Director (ADR 0037), `ce_memory` retrieval into pinned snapshots, the repetition guard and the contradiction checker, `ce_policy` blocklists and the testimonial guard, `ce_research` with the SSRF-guarded fetcher (ADR 0038); `PlanVideoWorkflow` → `PrevizWorkflow` → `:approve` → generation; planning API. DoD: acceptance fixtures for §2, §15.5 and §15.4 pass in fixture mode; exact-script property tests; the invalid-JSON repair test, prompt-injection fixtures, memory retrieval/contradiction/repetition and SSRF tests pass; `make e2e-mock` goes from text to a ready video through the API. Invariants I7, I10, I13. Report: `docs/progress/phase-4.md`.

### Phase 5 — Frontend MVP shell — **done** (2026-10-04)
`apps/web` (ADR 0039): Next.js 16 with a same-origin API gateway, auth and navigation, dashboard and projects, the ten-step Create wizard driven by `GET /v1/create-options` with the previz review (storyboard, intent, Performance timeline, predicted coverage, memory and findings, approve with overrides, regenerate plan), Video Studio v1 (player, SSE job progress, Performance lane, scene coverage badges, versions), read-only creators and worlds, jobs and developer viewers; the `web` Compose service. DoD: the Playwright test in mock mode (create → previz → approve → progress → play) and the I9 coverage-badge contract test pass. Invariant I9. Report: `docs/progress/phase-5.md`.

### Phase 6 — Incremental editing — **done** (2026-10-04)
`ce_core.edit` (the 38-operation `EditOperation` union with slots, SpecPatch, lock checks, anchor rebase, spec diff, translation), reference resolution and the `edit` stage, `compute_proposal` with impact, `no_visible_effect`, predicted coverage delta and alternatives (ADR 0040); behavior-aware dirty propagation with generation digests, per-position plate views and the content-pinned voice lock (ADR 0041); `ProposeEditWorkflow` / `ApplyEditWorkflow`, derived versions with planned routes and a derived plan report; edits, locks, regenerate, re-route, takes, resume, branch, restore, duplicate, compare, estimates and vocabulary endpoints; the Studio's NL edit panel and proposal cards, Advanced performance and intent editors, locks, takes, versions tree and compare page. DoD: exact dirty-set tests (behavior edits with voice locked and unlocked, every §19.5 World DNA change, accent, camera, wardrobe, pacing editorial vs re-performance), environment lock tests with explained refusals, restore as a new version, the §2 edit acceptance fixtures, re-route re-proposals of stale compiler approximations, and the e2e "make him more skeptical" + compare (stack and Playwright) pass. Invariants I3 (video versions), I6 (scene overrides), I8. Report: `docs/progress/phase-6.md`.

### Phase 7 — Real CPU pipeline — **done** (2026-10-04) → milestone **M1**
Real CPU engines behind `CPU_REAL_ENGINES` with pinned, checksummed, license-checked assets (ADR 0042): Kokoro TTS (dev only, ADR 0043), faster-whisper ASR, coarse alignment and LID, MediaPipe face and body, prosody features, DINOv2 image embedding, AuraFace, DNSMOS and PP-OCR. Language normalizers and the exact-script loop with WER/CER, attempt-seed retries and `needs_review` (ADR 0044); proxy calibration stored per analyzer revision (ADR 0045); captions with bundled Noto fonts, RTL, safe zones and ASS/SRT/VTT records; deterministic camera, reframing, realism and per-scene acoustics (ADR 0048); CPU world QC; C2PA signing with a generated dev CA and `GET /v1/renders/{id}/verify` (ADR 0046); watermarks measured on CPU and kept `mock_dev` in dev until Phase 8 (ADR 0047); screen-recording analysis, zoom/speed/bubble planning and the real `screen.prepare` (ADR 0049). DoD: render goldens (−14 ± 1 LUFS, true peak ≤ −1 dBTP, captions, aspect variants, Arabic captions, C2PA valid with an untrusted dev root), the exact-script loop with CPU TTS, analyzer tests (fixture clips skip with the reason: owner-supplied), and real analyzers on mock video reporting `NOT_MEASURABLE` without a face. Report: `docs/progress/phase-7.md`.

### Phase 8 — Real GPU adapters (sandbox until validated) — **done** (2026-10-04), GPU validation pending
`ce_plugin_kit` (adapter/backend split with CPU stand-ins, ADR 0051) and 28 GPU adapters for every "8" row of §39.7, each with license blocks for the model and every dependency at pinned revisions (re-verified online: 0 mismatches), declared behavior matrices, translators with golden tests and `estimate()`; the model registry in the database with promotion, evidence and calibration endpoints, the registry overlay and per-version snapshots (ADR 0053); `CalibrationWorkflow`; the `eval/smoke` golden set, the smoke/bench/calibrate harness and the smoke-promotion flow; GPU family Dockerfiles generated from the manifests (ADR 0052); `scripts/verify_licenses.py`; PP-OCRv6. DoD: all adapters import and pass the contract tests (CPU stand-ins); **no GPU was present, so every GPU adapter stays `validation: untested_on_gpu` and nothing claims otherwise** (`docs/GPU_VALIDATION.md`). Report: `docs/progress/phase-8.md`; the interrupted session's handoff: `docs/progress/PHASE_8_HANDOFF.md`.

### Phase 9 — Fleet and providers — **done** (2026-10-04) on simulated providers; M2 pending (no GPU)
The scheduler is the fleet authority (ADR 0054): `local_docker`, `runpod_pod`, `runpod_serverless` and a third-party stub provider; autoscaling from the backlog with provider/class/region fallback and a driver filter; one-time enrollment tokens for provisioned and self-managed hosts; provisioning timeouts; idle terminate/stop; budget projection with `budget_daily`/`budget_video`/`budget_project` holds, provider caps and alerts; `fleet_costs` for idle overhead; cold-start, load, fetch and lease-wait metrics; measured speed for backlog estimates; a model cache safe on shared volumes; the GPU API and page. DoD: simulated-provider tests pass (scale up and down, budget hold, provider failure fallback, and an end-to-end build on a fleet-provisioned host). **No `RUNPOD_API_KEY` and no approved spend: no paid provider was called. No GPU: milestone M2 is not reached.** Report: `docs/progress/phase-9.md`.

### Phase 10 — Creator Studio and World Studio — **done** (2026-10-05) in mock mode
Studio jobs on the fleet (ADR 0055): identity packs with identity scores and the VLM age check, wardrobe references, voice design, the test bench and lexicon, the Creator Test with its scorecard, ratings and baselines; World Studio with plate candidates, fingerprints, approval, diffs, override promotion (I6) and the continuity report; the consent data model and endpoints (flows off until V1); Creator Studio and World Studio in the web app. DoD: creator → test → approve and world → plates → approve → used in a video pass end to end in mock mode; world versioning tests pass. **No GPU: no real GPU run was recorded in GPU_VALIDATION.** Report: `docs/progress/phase-10.md`.

### Phase 11 — QC gate, Performance QA, continuity, Creative Director, benchmarks — **done** (2026-10-05) in mock mode
The QC gate executes in the build (ADR 0056): thresholds keyed by adapter, the VLM judge, a per-shot decision ladder with count and USD budgets that re-runs only the failing take's nodes, fallback routes, review flags with the best attempt; world continuity (element checks, background continuity); `ConsistencyWorkflow` with rolling baselines and the human rating queue; the QC report UI with the requested/compiled/observed triad; the Creative Director critique with findings as edit proposals; the golden evaluation set, the benchmark runner with blind pairwise rating and bench-based promotion; `audio_emotion` in the sandbox; calibration of low-reliability checks against human ratings. DoD: failure injection proves only failed nodes rerun and budgets cap retries; Performance QA follows the outcome matrix; continuity suites pass; critique proposals apply cleanly. **No GPU: no real engine was gated, judged or benchmarked.** Report: `docs/progress/phase-11.md`.

### Phase 12 — Memory loop, research, templates, brand, packaging and exports — **done** (2026-10-05) in mock mode
The memory loop as jobs (ADR 0057): `MemoryUpdateWorkflow` for approval, `ready`, export, applied edits and embeddings; observation habits merged per video and never promoted from mock evidence; preferences from repeated edits; embeddings in retrieval, the repetition guard and research, compared only within one model, with keyword fallbacks; conflicts with scopes. Persistent research (ADR 0058): `ResearchIngestWorkflow` for URLs (SSRF-guarded), PDF/DOCX/HTML/text/SRT/VTT uploads and notes; facts with spans; hybrid retrieval; claims re-checked in code with detection of unreported statistics; closed book = only the user's own sources, blocking without override; the claim ledger with overrides; export blocking. Spec templates (slots, compose, apply as edits, immutable versions), brand kits with the project default and the logo burned by `render.final` (ADR 0059); `captions_llm` caption translation with content-bound review; Director stage 12 packaging with labelled design-default limits, the labelled template fallback and render-frame thumbnails; exports with every rule, metadata and the memory `exported` path; the web UI for all of it. DoD: memory suites, closed-book tests, template composition, export preset and packaging-limit tests pass. **No real `embed.text` model is registered (none could be pinned or license-verified here), `captions_llm` is not smoke-validated (no paid LLM calls), dev renders are mock provenance and cannot be exported.** Report: `docs/progress/phase-12.md`.

### Phase 13 — **intentionally skipped** (removed from the plan by the owner, 2026-10-04)
The authoritative spec (`docs/MASTER_BUILD_PROMPT_v2_private.md`) has no Phase 13; Phase 12 is followed by Phase 14 (docs/SPEC_ERRATA.md P1, ADR 0050). Nothing is implemented or reported under this number. What earlier documents had scheduled here stays **unscheduled backlog**, not a dependency of any phase: the testimonial guard's review UI and quote-consent flow (the detection exists since Phase 4), disclosure verification, the export checklist UI (delivered within Phase 12's export dialog, §31), retention jobs beyond the memory tombstones that exist, and an extended cross-tenant suite (the I12 suite already covers every tenant table and every id route).

### Phase 14 — Hardening and documentation — **done** (2026-10-06) in mock mode
Observability as code (ADR 0060): metrics on side ports, API spans, row-derived `ce_ops_*` gauges, Prometheus with 15 alert rules and runbooks, Alertmanager, Loki, Tempo, the OpenTelemetry collector and five generated Grafana dashboards in the Compose `observability` profile. The scheduler load test (3000 and 6000 tasks, faults, cancellations, holds, fleet fallback) found starvation behind a backlog, fixed by fair-share leasing (migration 0005) at a measured −7 % throughput. The security review fixed web security headers, the client address used for login rate limiting and the scheduler's exposed metrics. Backup, restore and verify with two measured drills; native mode; the scripted demo, which found a true-peak overshoot in delivered renders (fixed). Operations, deployment, observability, security, load-test, demo, architecture, setup, development, testing and troubleshooting docs; the domain docs reviewed against the code. DoD: a fresh clone reaches the mock-mode demo from the README; the whole suite passes. **No GPU, no paid provider and no hosted LLM were used.** Report: `docs/progress/phase-14.md`.

## Milestones

- **M1 — after Phase 7 (reached 2026-10-04):** the complete mock + CPU product. Every flow works end to end, with mocks only where a GPU is needed (generation engines, the VLM, watermarks) or owner input is missing (analyzer fixture clips).
- **M2 — after Phase 9 (not reached: no GPU in this environment):** the first GPU-validated video on smoke-promoted routes. Only behavior validated at M2 may back a production mode.

## Extension points kept from the start (§39.5)

| Extension point | Stub | Phase |
| --- | --- | --- |
| Multi-character | `cast[]` with several entries, `speaker_key`, `overlap_with_previous_ms`, `character_key` everywhere, `two_shot` enum (rejected while disabled), `multi_person` matrix dimension | 1, 2, 3 |
| Variants | `POST …:variants` → 501; origin `variant` | 1, 6 |
| Remix | `POST …:remix` → 501; origin `remix` | 6 |
| Autonomous suggestion | `AutonomousSuggestWorkflow`, flag `autonomous_suggest_enabled=false` | 11 |
| Digital twins and cloning | consent tables, endpoints, policy checks; flag `digital_twins_enabled=false` | 1, 10 (the spec's "13" is void: P1) |
| LoRA training | job kind `lora_train`, `IdentityTrainer` interface + mock | 2 |
| Expression post-edit, pose-guided gestures | methods `post_expression`, `pose_guided`; `ExpressionEditor` pass-through; node `post.expression` | 3 |
| Segment-control avatar engines | `mock_avatar_segment` proves the upgrade path | 3 |
| OIDC | `AuthProvider` interface; OIDC adapter in V1 | 1 |
| More GPU providers (Vast.ai, SkyPilot, Modal) | plugins only | 9 |
| Share links and takedown | flag `share_links_enabled=false`; no tables until V1 | V1 |

## V1

- Digital twins with verified consent, and consent-gated voice cloning.
- Azerbaijani TTS (fine-tune or licensed engine); per-language lip-sync bake-off for tr, ru, ar and az.
- Per-creator LoRAs (image + Wan 2.2) to raise identity and world fidelity.
- Hook, CTA and caption variants; content remix.
- Walking and car vlogs (image-to-video + lip-sync patch).
- Product shots by compositing and reference-conditioned generation.
- Wan VACE outfit and background edits.
- Full brand kits and a shared spec-template library.
- SkyPilot provider. (Vast.ai: the `vast` plugin exists, mocked tests only, not yet live-validated.)
- Autonomous suggest-then-approve.
- Share links with takedown.
- The OIDC adapter.

## V2

- Two-person podcast and interview (shot/reverse-shot first, then true two-shots).
- Gesture-guided shots from a licensed motion library (pose-conditioned engines).
- An expression keyframe pass (blink, glance, smile) with a MediaPipe-based LivePortrait-style engine, making `post_expression` HONORED.
- Segment-level emotion and action engines (SoulX-LiveAct- and AptAvatar-class), promoted after sandbox validation of their measured behavior profiles.
- Listening and idle behavior engines for silent reactions.
- Long-form 5–20 minutes with drift management.
- Joint audio-video engines for reactions and ambient B-roll.
- Collaborative editing; a public API and SDK; a plugin marketplace; real-time preview.

## Experimental (sandbox only, never promised)

- Products held in hand with legible labels.
- Overlapping speech and interruptions.
- Frame-precise micro-expressions.
- Natural walking in long takes.
- 4K talking heads.
- EU-licensable video-synced foley.
- Commercial co-speech gesture generation.
- Learned "creator behavior models" trained on accepted takes.

## Promotion gates

- **MVP → V1:** the golden set passes; cost per clip is measured; mock-mode CI is green; coverage and observation reports exist for every golden item.
- **V1 → V2:** the consent flow has been audited; per-language bake-offs pass; LoRAs measurably lift identity and world scores.
- **Experimental → roadmap:** a sandboxed engine meets QC thresholds, measured-behavior targets and human-rating parity on the golden set.

## Open owner decisions (§41)

Jurisdiction and audience regions · revenue band · hosted LLM provider and key · GPU provider accounts and budget caps · commercial TTS licenses for Azerbaijani · priority languages · FFmpeg distribution for self-hosting · fixture clips for analyzer tests · audio-emotion model license · brand name and design system. Defaults apply until answered; **spending money always requires explicit approval.**
