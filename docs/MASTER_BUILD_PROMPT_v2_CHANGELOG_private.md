# MASTER BUILD PROMPT v2 — Changelog

Compared with `MASTER_BUILD_PROMPT.md` (v1, research snapshot 2026-10-03).

v2 is a complete rewrite, not an addendum. Every new capability is threaded through the vision, invariants, ADRs, schemas, build graph, adapters, router, QC, database, API, UI, tests, phases, MVP scope and documentation. The research snapshot date is unchanged, because this revision is architectural: no new model capabilities were introduced, and uncertain claims are now explicitly marked **[RV]** (re-verify).

---

## 1. Summary

| | v1 | v2 |
| --- | --- | --- |
| Length | ~15,000 words, sections 0–35 | ~41,000 words, sections 0–42 |
| Operating rules | 18 | 20 (adds executable invariants and the "no orphan concepts" rule) |
| Architecture invariants | none stated | I1–I14, each with a test |
| ADRs | 0001–0016 | 0001–0027 (11 new, 5 amended) |
| Behavior model | emotion labels on "beats" + events | situational acting chain → CanonicalBehaviorSpec → compiler → translator → observation |
| Environment | free-text `environment` block per scene | World DNA: versioned, with plates, overrides, continuity QC |
| Memory | one bullet + one table | structured Creator Memory: typed items, snapshots, usage log, repetition guard, contradiction checker |
| Identity root | `avatars` (avatar = creator) | `creators` → `creator_versions` pinning `appearance_versions` and `voice_versions` |

---

## 2. Section map (v1 → v2)

| v1 | v2 | Change |
| --- | --- | --- |
| 0 Role | 0 | Mission reframed as "simulate a believable, persistent creator" |
| 1 Rules (18) | 1 (20) | Rules 19 (invariants are executable) and 20 (no orphan concepts) added. Rules 5, 7, 11, 14, 15 and 16 tightened |
| 2 Vision | 2 | Adds situational examples, the persona formula and the three-rule honesty statement |
| 3 Objectives | 3 | Model independence is objective #2; the autonomous-publishing contradiction is removed |
| — | **4 Architecture invariants** | New |
| 4 Ecosystem facts | 5 | Adds the alignment-coverage fact (no tr/ar/az in Qwen3-ForcedAligner), new license landmines (SyncNet weights, emotion2vec+, Kokoro/espeak-ng, AuraFace bundle, FFmpeg GPL distribution) and [RV] markers |
| 5 ADRs | 6 | 0017–0027 added; 0002, 0003, 0004, 0007, 0012, 0013, 0014 and 0016 amended |
| 6 Stack | 7 | librosa prosody features, MediaPipe Pose/Hand, explicit no-Parselmouth (GPL), FastAPI SSE [RV] |
| 7 Repo layout | 8 | New packages `ce_creator`, `ce_world`, `ce_memory`; `config/vocab/*`, `intent_policies.yaml`, `memory.yaml`, `languages.yaml`; `plugins/providers/*`, `plugins/analysis/*`, `infra/observability/`, PR template |
| 8 Services | 9 | Adds a workflow ↔ job-kind table (26 job kinds); `worker-cpu`; render ownership clarified |
| — | **10 Domain model, layers, conventions** | New: layer stack, identity entities and lifecycle, canonical terms, status vocabularies, IDs and SpecPaths, resolution-order table |
| 9 VideoSpec | 11 | Rewritten (see §3.1 below) |
| 10 Build graph | 12 | Node list rewritten; BuildManifest; route and seed pinning; content-only digests; `no_visible_effect`; cascade; voice-lock semantics; GC references |
| 11 Director | 13 | 12 stages (context, intent, world binding, situational acting, plan-time compile, plan report) |
| — | **14 Director Intent layer** | New |
| 12 Human Behavior Engine | 15 | Rewritten as situational acting + CBS + compiler |
| — | **16 Observed behavior and Performance QA** | New |
| 14 Creator DNA | 17 | Creator as root entity; DNA as tendencies; composed keyframes; Creator Test replaces Avatar Test |
| (11 "content memory" bullet) | **18 Creator Memory** | New full section |
| — | **19 World DNA and environment continuity** | New |
| — | **20 Creator consistency across videos** | New |
| 13 Voice | 21 | Versioned drafts, tag-parser normalization, CTC aligner for tr/ar, honest accent changes, no engine splicing |
| 15 Camera/realism | 22 | Maturity per profile; resolution and fps budget per tier; reframing through capabilities |
| 16 Adapters/router | 23 | `run()` added; BehaviorTranslator; behavior matrix; new analyzer capabilities; route preview; pinning |
| 17 Plugins | 24 | License closure over dependencies; status vs validation; smoke promotion; mock engines with behavior tracks |
| 18 Scheduler | 25 | Providers as plugins; retry ownership with seeds |
| 19 QC | 26 | Thresholds keyed by metric adapter; Performance QA, world QC and consistency integrated; critique scores extended |
| 20 Rendering | 27 | Base/overlay tracks, reaction layouts, RTL fallback, per-scene acoustics |
| 21 NL editing | 28 | `EditOperation` is the only mutation path; union widened from 14 to 38 operations (including annotation and structure edits); coverage delta |
| 22 DB | 29 | About 30 tables added or reworked (§4 below) |
| 23 API | 30 | Async-only rule enforced; creators/appearances/worlds/memory/behavior/previz/manifest endpoints; `/v1/admin/*` |
| 24 Frontend | 31 | Simple/Advanced/Developer disclosure; Creator Studio, World Studio, Video Studio panels |
| 25 Safety | 32 | Dependency license closure; enable vs evaluate; provenance dev mode; share links moved to V1 |
| 26 Security | 33 | Platform-admin role; SSRF guard from Phase 4; memory as data |
| 27 Observability | 34 | Coverage, consistency, world QC and memory metrics |
| 28 Config/env | 35 | Dev-safe `.env.example`; `PROVENANCE_MODE`; KMS, idempotency, SSE and enrollment TTL variables |
| 29 Docker | 36 | `worker-cpu`; libass HarfBuzz/FriBidi; FFmpeg distribution ADR |
| 30 Testing | 37 | 17 behavior, world and memory suites with phases; invariant tests; template-Director fallback |
| 31 Docs | 38 | `BEHAVIOR.md`, `INTENT.md`, `CREATORS.md`, `MEMORY.md`, `WORLDS.md`, `CONSISTENCY.md`, `VOCABULARIES.md`, `INVARIANTS.md` |
| 32 MVP/roadmap/model map | 39 | Foundations, mocked, GPU-validation and roadmap tiers separated; "Built in phase" column; milestones |
| 34 Owner decisions | 41 | Adds FFmpeg distribution, fixture clips, audio-emotion license |
| 35 Start now | 42 | ADRs 0001–0027 and `INVARIANTS.md` |

---

## 3. What was added

### 3.1 New concepts (each traced through every layer)

| Concept | Spec / schema | DB | API | UI | Build graph / adapters | Tests | Phase |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **World DNA** (§19) | `WorldDNA`, `scenes[].world` binding, overrides (states, hide, add, move, lighting, acoustics), `continuity_ref` | `worlds`, `world_versions` (plates, plate candidates, fingerprints), `scenes` projection columns | `/v1/worlds…`, `/world-versions…`, plates, continuity | World Studio; Video Studio Creator & World panel | `world.plate`, `qc.world`, `audio.room`; `image.embed`; invalidation table §19.5 | schema, versioning, locks, dirty sets, continuity | 1, 6, 7, 10, 11 |
| **Situational acting** (§15.1–15.5) | `acting {situation, states[], events[]}` with felt/displayed emotion, masking, goals, strategies, transitions | via spec + `behavior_observations` | `/versions/{id}/behavior` | Performance panel, Performance lane | `behavior.resolve` | compilation, trajectory, acceptance fixtures | 3, 4 |
| **Director Intent** (§14) | `intent.video`, `scenes[].intent`, `derived_from` traceability | via spec | `/versions/{id}/intent` | Intent panel | intent policy engine → all channels | schema, policies, traceability | 1, 4 |
| **CanonicalBehaviorSpec** (§15.6) | envelope + content; cast-indexed; `requested_controls` per channel | artifact (kind `cbs`) | `/versions/{id}/behavior` | Developer CBS viewer | `behavior.resolve` → `behavior.compile_*`, `behavior.keyframe_state` | schema, digest stability, model swap | 3 |
| **Behavior compiler and translators** (§15.7) | `CompiledBehavior`, ProsodyPlan, VisualPlan; 16 realization methods; retry targets by method | — | — | Coverage panel | `BehaviorTranslator` per plugin; knobs + calibration | golden translations, coverage upgrade | 3, 8 |
| **Capability/behavior matrix** (§23) | manifest `behavior_matrix`, canonical dimension list | `model_behavior_profiles` (declared vs measured, by source) | Models page data | Models page | router scoring, compiler reliability | contract tests | 2, 3, 8, 11 |
| **Observed behavior / Performance QA** (§16) | `ObservedBehavior` (measurement vs judgement), `BehaviorCoverageReport`, 6 verdicts, 12 outcomes | `behavior_observations` | `/coverage`, `/takes/{id}/observations` | Observed/QC panel | `behavior.observe`, `behavior.coverage`; analyzers `face.landmarks`, `body.landmarks`, `audio.prosody`, `audio.emotion` | triad, QC actions | 3, 7, 11 |
| **Creator Memory** (§18) | `MemoryItem`, `MemorySnapshot` (per cast member), usage events | `creator_memory_items`, `memory_snapshots`, `creator_usage_events` | `/creators/{id}/memory`, memory-item actions, usage, snapshot | Creator Memory tab, Behavior history | snapshot digest via CBS content; repetition guard; contradiction checker | retrieval, contradictions, repetition | 4, 10, 12 |
| **Creator consistency** (§20) | `ConsistencyReport`, baselines | `creator_baselines`, `consistency_reports`, `human_ratings` | `/creators/{id}/consistency`, `/baselines`, ratings queue | Consistency tab | `ConsistencyWorkflow` (job after `ready`); `face.embed`, `voice.embed`, `image.embed` | continuity suites | 11 |
| **Creator root entity** (§10.2, §17) | `CreatorVersion` pins appearance and voice; cast overrides | `creators`, `creator_versions`, `appearances`, `appearance_versions`, `wardrobe_versions`, `voice_candidates` | `/creators…`, `/appearances…`, `/wardrobes…` | Creator Studio | composed keyframes | immutability | 1, 10 |
| **BuildManifest, route and seed pinning** (§12.3–12.5) | `BuildManifest` | `build_manifest_entries`, `cache_entries`, `artifact_refs` | `/versions/{id}/manifest`, `:reroute`, `:resume` | Developer viewer | router pinning | cache stability, pinning | 2 |
| **SpecPath** (§10.5) | key-addressed paths | — | locks, edits | diffs | dirty analysis | parsing/globbing | 1 |
| **Plan report / previz** | `PlanReport` | artifact | `/versions/{id}/previz` | Previz review | stage 11 | schema | 4 |

### 3.2 Other additions

- **Invariants I1–I14** (§4), each with a test in `tests/invariants/` and a named phase.
- **ADRs 0017–0027**: FFmpeg distribution, CBS, situational acting, Director Intent, World DNA, Creator Memory, the requested/compiled/observed triad, route/seed pinning, SpecPaths + EditOperation as the only mutation path, Creator as the root entity, declared vs measured capability.
- **Closed, versioned vocabularies** in `config/vocab/` (emotions, acting, events, dimensions, observation proxies, descriptions, intent, memory kinds, world elements, edit vocabulary), with lint rules.
- **Resolution-order table** (§10.6) for every value defined at more than one level.
- **Canonical terms table** (§10.3) and one meaning per status vocabulary (§10.4).
- **Workflow ↔ job-kind table** (§9).
- **Platform-admin role** and `/v1/admin/*`; org feature-flag overrides.
- **Analyzer capabilities**: `face.landmarks`, `body.landmarks`, `audio.prosody`, `audio.emotion` (sandbox), `voice.embed`, `image.embed`, `embed.text`, `asr.lid`, plus `ExpressionEditor` (V2 mock) and `IdentityTrainer` (V1 mock).
- **Mock avatar engines** with different matrices and `behavior_track.json` sidecars, so the model-swap invariant and the coverage upgrade are testable on CPU.
- **Template-Director fallback** so `make dev` works with free-form input.
- **Milestones**: M1 (complete mock + CPU product, after Phase 7) and M2 (first GPU-validated video, after Phase 9).
- **New env vars**: `PROVENANCE_MODE`, `EMBEDDING_MODEL`, `KMS_PROVIDER`, `KMS_LOCAL_KEY_PATH`, `IDEMPOTENCY_TTL_S`, `SSE_STREAM_RETENTION_S`, `ENROLLMENT_TOKEN_TTL_S`, `FIXTURE_CLIPS_DIR`.
- **New Makefile targets**: `fetch-cpu-assets`, `test-invariants`, `test-behavior`, `calibrate PLUGIN=…`.

---

## 4. What was changed

### 4.1 VideoSpec
- `characters[]` → `cast[]` pinning `creator_version_id`, with optional appearance/voice overrides, `role` and `voice_prosody`.
- `scenes[].performance {baseline, beats, events}` → `scenes[].acting {situation, states, events}`. "Beat" is now reserved for music beats.
- `scenes[].environment {environment_id, description, lighting, continuity_ref}` → `scenes[].world` (world version, camera position, time of day, weather, overrides, typed `continuity_ref`) + `scenes[].cast[]` (wardrobe per scene, placement, default posture).
- New: `intent`, `memory.snapshots`, `products`, `scenes[].intent`, `scenes[].pacing`, `generation.{seed_overrides, engine_hints}`, `tokenizer_version`, `vocab_version`, `segments[].quote_source`.
- New: shot `layer` (base | overlay); a closed shot-type enum; `reaction_source`; typed screen zooms and speed segments.
- New: `derived_from` traceability with `route_digest` for compiler approximations.
- Anchors: `Anchor {segment_id, word_start, word_end}` → `WordSpan {start: WordRef, end: WordRef}`, which can cross segments. `SegmentRef` and `SceneSpan` are defined. A canonical tokenizer is defined, with anchor rebase on script edits.
- Element references renamed from `*_id` to `*_key`. Take keys are derived from the take index.
- Locks: `{group, paths}` with array-index JSON Pointers → `{group, scope}` expanded through `edit_vocabulary.yaml` into SpecPaths.
- `script.wording_locked` is now a read-only computed property.
- `audio.room_tone` + `audio.mic_profile` → `audio.acoustics` (world-derived by default). `audio.music` → `music.cues[]`.
- `render.overscan` removed (the camera profile is the single source).
- `provenance.{c2pa, video_watermark, audio_watermark}` removed (they cannot be disabled). `consent_record_ids` → `consent_ids`.

### 4.2 Build system
- The cache key now includes `impl_version`, config digests, the translator version and the effective seed, and never a `version_id`. Downstream nodes key on compiled-output artifact hashes, and the CBS content digest only enters the compile nodes.
- QC-rejected artifacts are never cache hits (`cache_entries` points only to accepted artifacts).
- Locks pin routes for locked groups. The voice lock also pins the compiled prosody for unchanged text.
- A version's identity columns are immutable; its BuildManifest is insert-only until frozen. `:resume` replaces `jobs/{id}:retry`.
- A version state machine and origin → starting-state rules are defined.
- Chunk cascade is made explicit.

### 4.3 Director
- 12 stages (from 11): context with memory snapshots; strategy plus video intent; persona check; scenes with world binding; situational acting; validate → resolve CBS → route → compile → plan report.
- The previz gate now shows intent, the acting trajectory, predicted coverage, memory usage, repetition and contradiction reports, and world proposals.
- Mode maturity: `reaction` moved to beta. Effective maturity is capped by route validation.

### 4.4 Voice
- `VoiceVersion` has a draft → approved lifecycle. Lexicon and WPM edits happen on drafts.
- Tags normalize into acting states and events through one parser (moved from Phase 6 to Phase 4).
- tr/ar alignment uses a CTC aligner with a coarse fallback.
- Accent changes say explicitly that the timbre changes, and the lip-sync patch is gated on the duration delta.
- No TTS engine splicing within one character.

### 4.5 QC
- Thresholds are keyed by metric adapter id. The SyncNet-class lip-sync metric is advisory until its weights license is verified.
- Performance QA, world QC and consistency are integrated.
- The fallback step is skipped and reported when no fallback route exists.
- One VLM judge by default; the triage tier is optional.

### 4.6 Infrastructure and other changes
- Runtime families: a `worker-cpu` image runs the `cpu_model` family (mocks and CPU engines through the scheduler); a `cpu_inproc` family covers in-process code and light analyzers.
- GPU, LLM, storage and KMS providers are plugins under `plugins/providers/`.
- `gpu_providers.kind` became text validated against registered plugins.
- Provenance in dev before Phase 7 uses `PROVENANCE_MODE=mock_dev` with a burned label. Production refuses to start with it.
- The license policy evaluates the dependency closure. `enabled=false` is a separate product decision.
- The API never calls an LLM or GPU synchronously: edit proposals, voice tests, fingerprints and age checks are jobs.

---

## 5. What was removed or renamed

| Removed / renamed | Replacement |
| --- | --- |
| `avatars` / `avatar_versions` as the creator identity; API `creator_id` = avatar id | `creators` / `creator_versions` + `appearances` / `appearance_versions` |
| Avatar Test, `avatar_tests`, `AvatarTestWorkflow` | Creator Test, `creator_tests`, `CreatorTestWorkflow` |
| `environments` table (mutable) | `worlds` / `world_versions` |
| `wardrobes` (mutable) | `wardrobes` + `wardrobe_versions` |
| `memory_items` | `creator_memory_items` + `memory_snapshots` + `creator_usage_events` |
| `consent_records`, `consent_record_id` | `consents`, `consent_id` |
| `templates` table | `spec_templates` |
| `generation_presets`, `model_providers`, `model_provider_models` | Removed (unused). LLM providers are plugins selected by `LLM_PROVIDER` |
| `PerformanceTimeline` type, "beats" for acting | `ActingPlan`, acting states |
| `ActingDNA` as a separate type | Behavior, gesture and gaze sections of `CreatorDNA` |
| `config/behavior_vocabulary.yaml` | `config/vocab/descriptions.yaml` + the other vocab files |
| `MOCK_LLM` | `LLM_PROVIDER=fixture` |
| `WATERMARKER` | `PROVENANCE_MODE` |
| `POST /v1/versions/{id}:generate`, `POST /v1/jobs/{id}:retry` | `:approve` starts generation; `:resume` resumes a partial/failed/cancelled version |
| Synchronous `POST …/edits` → 201 and synchronous voice test | `202` + job + SSE |
| Model names in API fields (`dnsmos`) | Generic names (`speech_quality`, `lipsync_score`) |
| "Fully autonomous publishing" (Experimental) | Removed: it contradicted a non-goal |
| `podcast_two_cam`, `interview` camera profiles as production | Experimental |
| emotion2vec+ in the Creator Test core scorecard | `audio.emotion` capability, sandbox only until its license is resolved |
| "VLM triage with a small model" as a default | An optional triage tier |
| Share links and takedown in MVP policy text | V1 |

---

## 6. Contradictions resolved

1. **Cache keys depended on non-deterministic routing.** Fixed by route pinning in the BuildManifest; locked groups pin routes.
2. **Seed policy contradicted QC retries, and seeds had no storage.** Fixed with default / spec-override / attempt / effective seeds, recorded in the manifest.
3. **QC-failed artifacts were reusable cache hits.** They are now marked `qc_rejected`; `cache_entries` points only to accepted artifacts.
4. **"Immutable" voice and avatar versions were mutated by the API.** There is now a draft → approved lifecycle, and edits on approved records create new drafts.
5. **Environment and wardrobe were mutable rows referenced by immutable specs.** Both are now versioned.
6. **Environment keyframes inside the avatar version forced avatar re-versioning.** Keyframes are composed per shot instead.
7. **Behavior control was over-claimed** (per-word gaze, nods and smiles offered with no engine able to execute them). Now the full intent is represented, the compiler reports HONORED / APPROXIMATED / UNSUPPORTED, and observation measures the result.
8. **Coverage was predicted, never verified.** The requested / compiled / observed triad and Performance QA fix this.
9. **Energy, emotion, pauses, mic, overscan, loudness, visible label and mock switches each had several definitions.** The resolution-order table and single sources fix this.
10. **Tags vs events vocabulary drift.** Fixed with one tag parser and closed vocabularies with lint.
11. **Lock groups vs regenerate components used different vocabularies.** Fixed by the single `edit_vocabulary.yaml`.
12. **Lock paths used array indices and invalid JSON-Pointer wildcards.** Fixed by SpecPaths.
13. **Anchors could not span segments, word indices shifted on edits, and B-roll tiling vs overlay was undefined.** Fixed by `WordSpan`, the tokenizer with rebase, and shot layers.
14. **Claims were keyed by version, breaking on copy-forward.** They are now keyed by `(video_id, claim_key)`.
15. **15 tenant tables lacked `org_id`, and global resources were mutable by any org admin.** Every tenant table now has `org_id`, plus a platform-admin role.
16. **Mock provenance was forbidden outside test, but `make dev` had to run from Phase 2.** Fixed by `PROVENANCE_MODE=mock_dev` in dev/test with a burned label, refused in prod.
17. **MVP beta languages had no route** ("unvalidated only in beta modes") **and no tr/ar alignment.** Language support status now drives routing, and a CTC aligner is added.
18. **Sandbox engines could not be promoted until Phase 10, so the GPU phases had nothing to route to.** Smoke promotion arrives in Phase 8.
19. **No production fallback for the talking head, so the QC fallback step was dead.** LongCat is built as the fallback, and the ladder reports a missing fallback.
20. **Phase inversions** (tag parser Phase 6 vs needed in Phase 3; SSRF guard Phase 12 vs URL fetch in Phase 3; export checklist before exports; emotion model never scheduled). Phases reordered.
21. **ADR 0016 (hosted LLM default) vs §34 (fixture default).** Hosted in prod, fixture in dev/test.
22. **Service-boundary violations** (`api` calling an LLM or GPU synchronously). Fixed with async jobs.
23. **Missing endpoints** for templates, brand kits, packaging, critique, memory, wardrobes, worlds, products, members, voice candidates, captions, cost, director runs and ratings. All added.
24. **Autonomous critique "applied automatically" vs "suggest-then-approve".** Approval is always required.
25. **A non-goal (mass posting) vs an experimental item (autonomous publishing).** The experimental item was removed.
26. **`reaction` mode was MVP-production but needed V2 capabilities.** It is beta now, with honest approximation.
27. **Transitive model dependencies were not license-checked.** Fixed with license closure.
28. **The `Adapter` protocol had no execution method.** `run()` added.
29. **The extension points listed in v1 (variants, remix, autonomous mode, multi-character, OIDC) had no API, interface or phase.** Each now has a stub, a test and a phase.

The first v2 draft was also reviewed independently, and these defects found in it were fixed before delivery:
- CBS digest semantics (envelope vs content);
- testability of I1 alongside plan-time approximations;
- version-row vs manifest mutability;
- three competing dimension vocabularies;
- observation semantics for editorial methods;
- per-character memory snapshots;
- the memory conflict index;
- render ownership after freeze;
- stage-11 ordering;
- strategy and event token collisions;
- about 25 smaller orphans.

A second verification pass then fixed what the first fixes had introduced:
- stale CBS envelopes in the cache (only the content is cached now);
- observation judgements not seeing request changes (measurement is split from judgement);
- a home for plan-time routes (`planned_routes`);
- the outcome count, which is now 12, including `UNSUPPORTED_OBSERVED`;
- the world-proposal flow, through a draft world bound to the spec;
- edits made during previz now re-running previz;
- routing for real CPU engines (CPU smoke validation, `allowed_envs`);
- the missing annotation edit operations;
- the I8 test phase;
- seed namespaces for duplicated videos;
- missing [RV] marks on snapshot-specific capability claims.

---

## 7. Architectural invariants added (§4)

| # | Invariant |
| --- | --- |
| I1 | Model-independent creator behavior; engine swaps change only compiled outputs, routes, artifacts, coverage and observations |
| I2 | VideoSpec = creative source of truth; BuildManifest = reproducibility record; projections are read-only |
| I3 | Immutability of approved identity versions and of video-version identity columns |
| I4 | Requested ≠ compiled ≠ observed; every requested item has coverage and an observation verdict |
| I5 | Deterministic builds; routes and seeds pinned |
| I6 | Overrides never mutate identity (World DNA, Creator DNA, Memory) |
| I7 | Memory enters planning only through pinned snapshots |
| I8 | Locks pin values (and routes for locked groups) |
| I9 | Honest UI: only confirmed behavior is shown as delivered |
| I10 | Data is never instructions |
| I11 | Provenance always on in production |
| I12 | Tenant isolation |
| I13 | Closed, versioned vocabularies for all control fields |
| I14 | Engines are adapters; core never imports engine libraries |

---

## 8. Phase 8–11 implementation checkpoint and handoff

The completed implementation is archived in cumulative, independently reconstructable Git bundles through Phase 11.

### Completion checkpoints

| Phase | Completion / checkpoint SHA | Notes |
| --- | --- | --- |
| Phase 8 | `6f5f230eb64821e382f182da73f11430b9225758` | Phase 8 completion checkpoint. |
| Phase 9 | `1949d108ecda47b3064c94e59091474dcac974d7` | Phase 9 completion checkpoint. |
| Phase 10 | `8be739c19e85084432e7aeed2942028719ade53d` | Phase 10 completion checkpoint. |
| Phase 11 | `5d6ba91967c8097ff75d78108e8610ba46c3bebe` | Current Phase 11 checkpoint; Phase 11 implementation/report is rooted at `cd23664c3990d49f37079f07c88ebdec1906f37`. |

Phase 11 includes the three internal workstreams:

- P11a — QC gate / failure-injection ladder
- P11b — continuity / consistency / rating calibration
- P11c — critique / benchmarks / Creative Director surfaces

These are part of Phase 11 and are not separate phases.

### Archived reconstruction points

```text
bundles/
├── creator-engine-phase8.bundle
├── creator-engine-phase9.bundle
├── creator-engine-phase10.bundle
└── creator-engine-phase11.bundle
```

All four bundles were verified as complete Git bundles and reconstructed successfully in scratch repositories during handoff creation. The Phase 11 bundle is the authoritative continuation point for a new engineering session.

### Continuation boundary

The handoff state is:

```text
Phase 8  ✅
Phase 9  ✅
Phase 10 ✅
Phase 11 ✅
  ├── P11a ✅
  ├── P11b ✅
  └── P11c ✅

Phase 12 ❌ not included
Phase 13 ❌ intentionally skipped
Phase 14 ❌ not included
```

A future implementation session must reconstruct from the Phase 11 bundle, verify the recorded checkpoint SHA, and continue with Phase 12. It must not assume that an archive-only GitHub repository is itself the live source tree.

Real GPU/provider/model validation remains separate from mock/simulated validation and must only be claimed after actual execution. The project documents the distinction between implemented/tested, implemented/untested, stubbed, and planned states.

---

## 9. Phase 12 implementation checkpoint

Phase 12 (memory loop, persistent research and the claim ledger, spec templates, brand kits, caption translation, packaging and exports) was built on branch `claude/phase12-14-work` from the Phase 11 bundle, in a new container without a GPU, in mock mode. The abandoned Phase 12 work-in-progress commit `3e4fc12` was not revived.

| Phase | Checkpoint SHA | Notes |
| --- | --- | --- |
| Phase 12 | `755bdcb5af05470fc99da650aef0b44d95a7222e` | Phase 12 completion checkpoint; report `docs/progress/phase-12.md`; ADRs 0057–0059. |

- Bundle: `bundles/creator-engine-phase12.bundle` (HEAD, `main` and tag `phase-12` at the checkpoint, plus tags `phase-8` … `phase-11`), verified with `git bundle verify` and reconstructed into an empty directory (identical tree, `git fsck` clean).
- Verification on the checkpoint commit in a clean clone: 1850 tests passed, 14 skipped (CPU-engine assets unreachable), 0 failed; lint, typecheck and the web tests pass.
- Not validated: a real text-embedding model (none registered), `captions_llm` against a real provider (no paid calls), exports of real renders (dev renders carry mock provenance).

## 10. Phase 13

Phase 13 was **intentionally skipped** by the product owner (ADR 0050, `docs/SPEC_ERRATA.md` P1). No Phase 13 was implemented, reported, bundled or renumbered; Phase 12 is followed by Phase 14.

## 11. Phase 14 implementation checkpoint

Phase 14 (hardening, observability, load test, security review, backup/restore, deployment and operations, documentation, demo) was built on the same branch after the Phase 12 checkpoint, in mock mode, without a GPU, a paid provider or a hosted LLM. Report: `docs/progress/phase-14.md`; ADR 0060; decisions D141–D148.

- Observability as code (`infra/observability/`, Compose profile `observability`), verified live against the services run natively.
- Scheduler load test with measured results only (`docs/LOAD_TEST.md`): 3000 and 6000 tasks with injected faults; starvation behind a backlog found and fixed by fair-share leasing (migration 0005).
- Security review executed (`docs/SECURITY.md`): dependency audits clean; four issues fixed.
- Backup, restore and verify drills (`docs/OPERATIONS.md`); deployment guide (`docs/DEPLOYMENT.md`); native mode; scripted demo (`docs/DEMO.md`).
- Fixed along the way: the delivered true peak, creator-consistency features, `make infra-up` building images, and two Phase 12 omissions.
- Fresh-clone verification: a clone of the branch from GitHub reached the mock-mode demo from the README alone and passed the whole suite (`docs/progress/phase-14.md`).
- Checkpoint: the commit tagged `phase-14` in `bundles/creator-engine-phase14.bundle`; its SHA, the bundle verification and the reconstruction are recorded in `docs/progress/phase-14.md` by the commit that adds the bundle.

## 12. Final checkpoint state

```text
Phase 0–7 ✅  (M1)
Phase 8   ✅  GPU adapters untested on GPU
Phase 9   ✅  no paid provider called
Phase 10  ✅
Phase 11  ✅  (P11a, P11b, P11c)
Phase 12  ✅  755bdcb — bundles/creator-engine-phase12.bundle
Phase 13  ⏭  intentionally skipped
Phase 14  ✅  tag phase-14 — bundles/creator-engine-phase14.bundle
M2        ❌  not reached (needs a GPU and the owner's approval to spend)
```

The `main` branch of the repository stays the archive-only repository; the source history and the Phase 12 and Phase 14 bundles live on `claude/phase12-14-work`. Validation claims follow the same rule as before: real GPU, provider and model validation is claimed only after actual execution, and none has happened.

