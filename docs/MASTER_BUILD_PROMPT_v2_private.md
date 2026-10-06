# MASTER BUILD PROMPT v2 — AI Creator Engine

> Paste this whole document into a new Claude Code session, or save it in the repository as `docs/MASTER_BUILD_PROMPT.md` and tell Claude Code to read it first. It is the complete brief. Do not ask the product owner to re-explain the product or the architecture.
>
> **Research snapshot date: 2026-10-03.** Every statement about third-party models, licenses, prices and library versions reflects that date and must be re-verified at integration time. Statements marked **[RV]** were already uncertain or conflicting at the snapshot; treat them as hypotheses until you verify them against the primary source (the LICENSE file or model card at a pinned revision, the official README, the official docs). Never fill a gap with an assumed capability.
>
> This is version 2 of the prompt. It replaces version 1 completely.

---

## 0. Your role and mission

You are an autonomous senior engineering team: staff backend engineer, ML infrastructure engineer, frontend engineer, DevOps engineer, QA engineer and technical writer, working as one.

Your mission is to build **AI Creator Engine**: a modular, self-hostable platform that turns an idea, a brain dump, a structured brief or an exact script into realistic creator-style videos. The output formats are talking heads, UGC, explainers and screen-recording tutorials now; podcasts and vlogs come later.

The product is not a face generator. It **simulates a believable, persistent creator**:

> appearance + voice + personality + memory + situational acting + emotion + gaze + gesture + posture + speech rhythm + reactions + camera awareness + environment continuity + editing behavior

All of it is represented in model-independent data, so that any model can be replaced without changing who the creator is.

This is not a "call one video API" app. It is a production studio built as software, made of these parts:
- an AI Director that writes a typed plan with explicit intent and acting;
- a canonical video document that is the single creative source of truth;
- a canonical behavior layer that sits between creative intent and any model;
- an incremental build system that renders only what changed;
- specialized open models behind replaceable adapters, running on rented NVIDIA GPUs;
- a deterministic post-processing layer that supplies much of the realism;
- a quality gate that compares what was asked for with what was actually rendered;
- compliance (consent, licensing, provenance) built in.

Build it incrementally. Keep it runnable at every step. Test everything you claim, and document every decision.

---

## 1. Operating rules (non-negotiable)

1. **Inspect the environment first** (Phase 0). Record the following in `docs/ENVIRONMENT.md`: OS, CPU, RAM, disk, GPU (`nvidia-smi`), Docker and Compose, Python and `uv`, Node and `pnpm`, the FFmpeg version and build flags, and network reachability (PyPI, npm, Hugging Face, GitHub, container registries). Adapt the plan to what exists.
2. **Create the repository structure and architecture skeleton before features** (section 8).
3. **Implement phase by phase** (section 40). Do not start a phase until the previous phase's Definition of Done passes.
4. **Run the tests after every meaningful change, and fix failures before moving on.** A red test suite is never "done".
5. **Never pretend something works if it has not been executed and verified.** If you could not run it (for example, because there is no GPU), say so in three places: the plugin manifest (`validation: untested_on_gpu`), `docs/GPU_VALIDATION.md` and your progress report. Always distinguish four states: implemented and tested; implemented but untested; stubbed; planned.
6. **Keep the project runnable at every stage.** After every phase, `make dev` must bring up a working system in mock mode.
7. **Never replace the modular architecture with hard-coded shortcuts.** Model names, provider names, prices, thresholds, platform rules, engine parameters and engine prompt syntax never appear in business logic. They live in config, plugin manifests, plugin translators or the database.
8. **Never silently remove a planned extension point.** If you must defer one, keep the interface, a mock implementation, a test, and a `TODO(phase-N)` that links to the roadmap item.
9. **Keep `TODO.md` (near-term tasks) and `ROADMAP.md` (phases, V1, V2, experimental) current.** Update them at the end of every work session.
10. **Document decisions as ADRs** in `docs/adr/NNNN-title.md` (context, decision, alternatives, consequences), and index them in `docs/DECISIONS.md`. Write one whenever you choose between real alternatives or deviate from this prompt.
11. **Use real implementations wherever they can run here; use mocks only where a GPU, a paid service or an asset is unavailable.**
    - CPU-capable components (FFmpeg rendering, captions, mixing, camera and realism post, C2PA signing, prosody features) must be real from the phase that implements them onward.
    - CPU model engines (CPU TTS, CPU ASR, OCR, MediaPipe landmarks) are real whenever their assets are present (`CPU_REAL_ENGINES=auto`). They are mocked only when the assets are missing.
    - Before the implementing phase, a clearly labeled mock is allowed in `dev` and `test` only (sections 32 and 37).
12. **Mark research-grade integrations explicitly**: `status: experimental` or `research_only` in the plugin manifest, a UI badge, and a note in `docs/MODELS.md`.
13. **Ask the product owner only when a decision is genuinely blocking.** Section 41 lists the known ones and their defaults. Otherwise make a reasonable engineering decision, record it as an ADR, and continue.
14. **Reality beats this prompt.** If a library API, model license, model capability or price differs from what is written here, follow reality and record the difference in `docs/DECISIONS.md`. Verify against the primary source: the official docs, the model's LICENSE file at the pinned revision, or the GitHub README. Use documentation tools (for example Context7) or official docs for library APIs instead of memory. Items marked **[RV]** must be verified before you rely on them.
15. **License discipline.**
    - Before integrating any model, read its LICENSE and model card at a pinned revision, and fill the license block for the model *and every weight it depends on* (section 24).
    - Never integrate non-commercial weights as a production default.
    - Never add a copyleft or source-available dependency to distributed artifacts without an ADR.
16. **Security discipline.**
    - No secrets in code, logs, fixtures or commits.
    - Tenant isolation is tested.
    - Fetched web pages, uploaded documents, Creator Memory text and model outputs are data, never instructions.
17. **Commit in small, green steps** with clear messages. One logical change per commit.
18. **Progress reports.** At the end of each phase, write a short report in `docs/progress/phase-N.md`: what was built, what was tested and how, what is untested, known issues, and next steps.
19. **Architecture invariants are executable.** Every invariant in section 4 has at least one automated test in `tests/invariants/`. A change that breaks an invariant needs an ADR and an updated invariant, never a skipped test.
20. **No orphan concepts.** A new entity, schema, capability, memory kind, vocabulary item, QC metric or EditOperation must be reflected wherever it applies, in the same change or a tracked TODO:
    - the Pydantic models, the VideoSpec or CBS, and the DB migration;
    - the API, the UI, the plugin manifest or interface, and the build-graph dependencies;
    - the router, the tests, the docs, and the phase or roadmap entry.

    The PR template in `.github/pull_request_template.md` carries this checklist.

---

## 2. Product vision

Users type things like these:

- "Create a 30-second TikTok explaining why most people misunderstand AI agents."
- "Create a natural-looking 45-second video of a 25-year-old tech creator sitting at a desk explaining this exact script."
- "Here is my exact A-Z script. Turn it into a realistic creator video without changing my wording."
- "Make a UGC-style product review with a woman in her late 20s filming herself with an iPhone in her bedroom."
- "Create a video where the creator starts excited, becomes skeptical, pauses, looks away, laughs slightly, then becomes serious before the CTA."
- "I want a guy explaining why cold email is dead but actually say it's not dead and make the beginning aggressive and then show examples and finish with something controversial."
- "He realizes halfway through that the viewer probably disagrees with him, and he tries to win them over."
- "She is pretending to stay calm, but she's clearly surprised by the result."

The system turns intent into a production through these steps, in order:
- research and Director Intent;
- script and story;
- scenes, worlds and shots;
- the creator: appearance, voice, persona and memory;
- situational acting: situation, internal state, intent, emotion, prosody, face, gaze, gesture, posture, camera awareness, reactions and editorial response;
- camera, B-roll and screen recording;
- music and sound effects, captions and editing;
- quality control, ending in the final video.

Afterwards the user can type edits like these:
- "make the first 3 seconds more aggressive"
- "make him more skeptical"
- "make her smile less"
- "change the room to a modern office"
- "make the camera slightly handheld"
- "change the outfit but keep the face"
- "keep everything the same but change the accent"

The system translates each edit into structured operations, previews the impact, and regenerates only what must change.

**The primary objective is a believable person, not just visual quality.** That means natural expressions, gaze, blinking, head and body movement, breathing, pauses, hesitation, emphasis, emotional transitions, imperfect gestures, realistic camera behavior and room sound. It also means a recognizable persona: how this creator talks, reacts, jokes, frames themselves and dresses, and the room they film in.

**Be honest about what current models can do.** In 2026, most of that behavior is *emergent* in open models. It comes from the voice performance, the reference image and editing, not from direct control (section 5). The system therefore follows three rules:

1. It represents the full intended behavior anyway.
2. It executes what the selected engines can execute.
3. For every requested behavior it reports whether it was honored, approximated or unsupported, and what was actually observed in the output.

The product never claims its output is undetectable as AI. It maximizes naturalness and consistency, and it marks every output as AI-generated, as the law requires.

---

## 3. Objectives and explicit non-goals

**Objectives, in priority order:**
1. Believable, consistent creator behavior and realism.
2. Model independence: creator identity, behavior, worlds and intent survive any engine swap.
3. Modularity and replaceability of every model, provider and engine.
4. Reliability: no lost work, partial regeneration, resumability, reproducibility.
5. Cost efficiency on rented GPUs.
6. Self-hosting with open models where legally practical.
7. Honest, explainable behavior: requested vs compiled vs observed is always visible.
8. Compliance by design.

**Non-goals (never build):**
- a "humanize" or "undetectable" mode;
- watermark or label removal;
- impersonation of real people without verified consent;
- automated mass-posting or autonomous publishing to social platforms;
- non-commercial model weights in production paths.

---

## 4. Architecture invariants

These are testable properties of the system (rule 19). The tests live in `tests/invariants/`.

| # | Invariant | Test |
| --- | --- | --- |
| I1 | **Model-independent creator behavior.** Creator DNA, Creator Memory, World DNA, Director Intent, the authored acting plan in the VideoSpec and the CanonicalBehaviorSpec (CBS) content contain no engine names, engine parameters or engine prompt syntax. Swapping an engine can change only five things: CompiledBehavior, routes, artifacts, coverage and observations. The one exception is spec elements tagged `derived_from: compiler_approximation` (§15.7). They record the route they were planned for, and after a re-route they are re-proposed through an EditOperation, never silently changed. | Model-swap test: call `build_graph()` and `compile()` twice on one version with two router configurations (mock engines with different behavior matrices), persisting nothing. Assert that the spec content digest (§12.2), the CBS content digest and the DNA digests are identical, and that only compiled outputs, coverage and routes differ. A schema lint rejects engine-parameter names in the core schemas. |
| I2 | **One creative source of truth.** The VideoSpec is the creative source of truth. The version's BuildManifest is the reproducibility record (routes, effective seeds, artifact map, config digests). DB projection tables (scenes, shots, takes) are read-only projections rebuilt from these two. | Projection rebuild test |
| I3 | **Immutability.** Approved versions of creators, appearances, voices, wardrobes, worlds and products are immutable. A video version's identity columns (spec, spec hash, parent, origin) are immutable, and its build-manifest entries are insert-only until the version is frozen (§12.3). Changes create new versions, and only drafts are mutable. | Repository and API tests that try to mutate approved rows |
| I4 | **Requested ≠ compiled ≠ observed.** Every requested behavior item in a CBS gets a coverage entry with a level (`HONORED`, `APPROXIMATED` or `UNSUPPORTED`) and a method. After generation it also gets an observation verdict. Nothing reports `HONORED` without a declared method that supports it. | Coverage completeness test; triad test |
| I5 | **Deterministic builds.** The same spec, BuildManifest, config and code produce the same cache keys. Routes and seeds are pinned per version, and re-routing happens only for dirty nodes or on explicit request. | Cache-key stability test; route-pinning test |
| I6 | **Overrides never mutate identity.** Scene-level overrides never change World DNA, Creator DNA or Creator Memory. Promoting an override into a world or creator requires an explicit versioning action. | Override isolation test |
| I7 | **Memory enters planning only through a pinned snapshot.** Creator Memory influences a plan only through a MemorySnapshot pinned to the version, so later memory changes never alter an existing version's plan or cache keys. | Snapshot pinning test |
| I8 | **Locks pin values.** Locks pin spec values, and for locked groups they also pin routes. A patch to a locked path is rejected with the lock group named. | Lock tests |
| I9 | **Honest UI.** The UI never presents a behavior as controllable or delivered unless its coverage and observation say so. | Frontend contract test on coverage badges |
| I10 | **Data is never instructions.** Sources, uploads, memory text and model outputs are wrapped as data in prompts. | Prompt-injection fixtures |
| I11 | **Provenance is always on in production.** Invisible watermarks and C2PA cannot be disabled when `APP_ENV=prod`, and production refuses to start with a mock watermarker or signer. | Startup and config tests |
| I12 | **Tenant isolation.** Every tenant-owned row carries `org_id`, and repositories require an org context. | Cross-tenant suite |
| I13 | **Closed vocabularies.** Control fields in intent, acting, CBS, memory kinds and edit operations use closed, versioned vocabularies (`config/vocab/`). Free text appears only in `description` and `notes` fields, which never drive control directly. | Schema validation tests |
| I14 | **Engines are adapters.** Core packages call capabilities through the router and the adapter contracts, never a specific library or model directly. This includes CPU analyzers such as MediaPipe, which run as in-process CPU adapters. | Import-lint test: core packages cannot import plugin packages |

---

## 5. Ecosystem facts you must design around (snapshot 2026-10-03, re-verify)

These facts drive the architecture decisions. Verify each one before relying on it.

**What open models do well today:**
- Frontal and three-quarter talking heads and seated half-body shots, with good English (and Chinese) lip sync from one reference image plus audio: InfiniteTalk, LongCat-Video-Avatar 1.5, Wan2.2-S2V [RV: versions and quality at integration].
- 5–10 s B-roll at 480–720p: the Wan 2.2 family; LTX-2.5 is license-gated [RV].
- Expressive English TTS, and multilingual TTS for major languages.
- Strong VLMs for critique: Qwen3.6 / Qwen3.8 checkpoints, Gemma 4 [RV: checkpoint names and licenses].
- Reliable CPU face and pose landmarks: MediaPipe Face Landmarker (including blendshape scores) and Pose and Hand Landmarkers [RV: confirm the blendshape output in the pinned version].

**What they do not do reliably:**
- products held in hand with legible labels;
- frame-precise emotion or gaze changes inside one shot;
- natural, non-repetitive gestures over long monologues;
- single takes longer than about 60 s without color or identity drift (chunk them to 20–30 s);
- overlapping speech;
- validated lip sync for Turkish, Russian, Arabic or Azerbaijani (no published evaluation);
- text rendered inside generated video;
- convincing "listening" behavior on silent audio.

**Behavior control reality:**
- Most avatar models take one global text prompt plus audio, and infer facial emotion and head motion from the audio's prosody. Gaze, blinks, nods, gestures and posture are *emergent*: they cannot be addressed at a word or timestamp.
- Therefore the voice performance is the strongest acting lever. Emotion changes should coincide with shot boundaries, and editing (cuts, punch-ins, cutaways) carries much of the perceived performance.
- Segment-level control exists only in newer models: SoulX-LiveAct, with sequential emotion and action segments at 512×512 / 24 fps, and AptAvatar, with frame-ranged structured prompts [RV: segment syntax and quality are not independently verified].
- Deterministic expression edits (blink, glance, smile) are possible with LivePortrait-style post-processing. Use the MediaPipe detector, because InsightFace weights are non-commercial [RV: that the detector swap works in the pinned code].
- **This is why the CanonicalBehaviorSpec, the capability matrix and observed-behavior QC exist** (sections 15–16): the system must represent more behavior than today's engines can execute, and say honestly what was executed.

**Unified audio+video models** (MiniMax H3, LTX-2.5, Ovi, NAVA, MOVA [RV: names, limits and licenses]) generate speech and video in one pass. But they are capped at 10–15 s, mostly lack voice references, are less accurate verbatim than dedicated TTS, and several carry restrictive licenses. Use them as optional adapters, not as the backbone.

**Alignment coverage:** Qwen3-ForcedAligner covers zh, en, yue, fr, de, it, ja, ko, pt, ru and es. It does **not** cover tr, ar or az. Turkish and Arabic need a different aligner (section 21) [RV].

**License landmines (check before use):**

| Model | Constraint |
| --- | --- |
| MiniMax H3 | Excludes the US, EU, UK and South Korea; >$20M revenue needs approval; UI attribution required |
| HunyuanVideo 1.5, HunyuanVideo-Avatar, HunyuanVideo-Foley, HunyuanImage, HunyuanOCR | The Tencent license excludes the EU, UK and South Korea |
| LTX-2 / 2.3 / 2.5 (and LoRAs) | Paid license at ≥ $10M entity revenue |
| FLUX.2 dev, FLUX.2 klein 9B, FLUX.1 dev / Kontext dev | Non-commercial |
| Qwen-Image-2.1 | Qwen Research License (non-commercial). Qwen-Image-2512 and Qwen-Image-Edit-2511 remain Apache-2.0 |
| Higgs TTS 3, Fish Audio S2 Pro, Breeze TTS 2, OmniVoice, Voxtral TTS, F5-TTS, XTTS-v2 | Non-commercial without a paid license |
| InsightFace model weights (used by PuLID, InstantID, InfiniteYou, IP-Adapter-FaceID, Lynx and the default LivePortrait detector) | Non-commercial |
| SMPL-X body model (used by EMAGE-style gesture models) | Non-commercial |
| MMAudio, ThinkSound, PrismAudio, AudioX, Woosh, TangoFlux, YuE2, MusicGen | Non-commercial |
| LatentSync 1.6 | Conflict: Apache-2.0 on GitHub vs OpenRAIL++ on the HF card. Resolve before use [RV] |
| Krea 2, SD 3.5, Stable Audio 3 | Free only below $1M revenue |
| Qwen Community License models (e.g. Qwen3.8-Flash-Next) | Need a separate license if you offer "model as a service". Licenses differ per checkpoint, so name checkpoints, never families |
| Llama 4 multimodal | Not licensed to EU-domiciled companies |
| SyncNet pretrained weights (syncnet_python) | Code is MIT; the weights license is unspecified [RV]. Until it is resolved, the lip-sync metric is advisory only (section 26) |
| emotion2vec+ (audio emotion) | The repo says MIT; the HF card says "other / model-license" [RV]. Sandbox only until resolved |
| Kokoro (CPU TTS for dev) | Weights are Apache-2.0, but the G2P fallback may pull in espeak-ng (GPL-3.0) [RV]. Dev/CPU only; never in distributed images without an ADR |
| AuraFace | Apache-2.0, but verify that the bundle you integrate does not include InsightFace detector weights [RV] |
| FFmpeg built with libx264 | GPL build. Distributing images that contain it to self-hosting customers is distribution (ADR 0017) |
| Remotion | Company license plus per-render fees for prompt-to-video products |
| MinIO | Upstream repository archived April 2026 — do not use |

**Azerbaijani:** no commercially licensed open TTS officially supports it. There are two ways to add it later: fine-tune an Apache/MIT TTS from its Turkish ability on licensed data, or buy a commercial license (Higgs TTS 3 or Fish S2 Pro [RV: their Azerbaijani support and license terms]). Mark Azerbaijani unsupported until then.

**Compliance dates** (this is not legal advice; the product owner will have counsel review):
- EU AI Act Article 50 transparency obligations apply from 2026-08-02. The EU Code of Practice (final 2026-06-10) expects two machine-readable marking layers: signed metadata plus an invisible watermark.
- California SB 942 (as amended by AB 853) applies from 2026-08-02 to generators above 1M monthly users.
- TAKE IT DOWN Act platform duties have been enforceable since 2026-05-19.
- The FTC Consumer Review Rule (16 CFR 465) bans testimonials that misrepresent that the reviewer exists.
- New York's synthetic-performer ad disclosure law took effect 2026-06-09.
- YouTube, TikTok and Meta auto-label content carrying C2PA AI assertions.

---

## 6. Architecture decisions (write these as ADRs in Phase 0)

| ADR | Decision | Main reason |
| --- | --- | --- |
| 0001 | **Hybrid, audio-first architecture.** Dedicated TTS produces the master audio timeline; talking shots use audio-to-video models; B-roll uses text/image-to-video. Unified audio+video models are optional adapters. | Verbatim accuracy, persistent voices, clip-length limits, licenses |
| 0002 | **The VideoSpec is the single creative source of truth**, immutable per version. The Director writes specs, and every mutation is an `EditOperation`. Each version also has a **BuildManifest** (routes, effective seeds, artifact map, config digests) as its reproducibility record. Workers consume a derived execution graph. | Reproducibility, partial regeneration, auditability |
| 0003 | **Incremental build graph with content-addressed artifacts.** The cache key is a hash of normalized inputs, including config digests and code `impl_version`. QC-rejected artifacts are never reused. Regeneration, locks, versions and resume all use this one mechanism. | One mechanism instead of five; cheap versions |
| 0004 | **Time anchored to words, resolved to seconds after alignment.** Voice re-synthesis does not break timings. Script edits rebase anchors through a token alignment, and the impact preview reports moved anchors. | Robust timing under edits |
| 0005 | **Temporal for durable workflows** (MIT); a **custom Postgres-backed GPU scheduler** for placement. Redis is never the job system of record. Hatchet and DBOS were considered; record why they were not chosen. | Durable retries, cancellation and child workflows; model-affinity and VRAM-aware placement are domain-specific |
| 0006 | **GPU workers dial out** to the scheduler over HTTPS and exchange data only through presigned object-storage URLs. | Works on any provider and behind NAT; least privilege |
| 0007 | **One Docker image per runtime family.** GPU families: `image`, `wan`, `tts`, `asr`, `audio`, `lipsync`, `vision`, `post`, `vllm`. There are two CPU families. `cpu_model` covers CPU model engines (Kokoro, faster-whisper, the CTC aligner, DNSMOS) and all mock model adapters; it runs in the `worker-cpu` image through the scheduler, exactly like a GPU family. `cpu_inproc` covers deterministic code plugins (effects, captions, provenance signing) and light analyzers (MediaPipe landmarks, prosody features, PP-OCR, AuraFace, image embedding), loaded in-process by the orchestrator and render worker. | Upstream repos pin conflicting Python/PyTorch/CUDA versions; CPU model engines must be schedulable and cacheable like GPU ones |
| 0008 | **Native inference pipelines in production** (LightX2V, Diffusers, upstream repos, vLLM/SGLang). ComfyUI is for R&D in the sandbox only, run unmodified as an internal service. | Typed APIs, reproducible dependencies, multi-GPU; ComfyUI is GPL-3.0 and its custom nodes are unpinned |
| 0009 | **Deterministic post-processing for camera behavior and realism** (handheld motion, punch-ins, grain, LUTs, compression, mic and room acoustics) on overscanned plates. | Cheap, repeatable, never adds generative artifacts |
| 0010 | **Object storage through the S3 API only.** SeaweedFS for local and self-hosted, Cloudflare R2 for managed; boto3 client. | MinIO is archived; R2 has zero egress fees |
| 0011 | **FFmpeg + PyAV + libass for rendering**, with a custom timeline UI; no Remotion. | Cost and licensing |
| 0012 | **License policy engine.** Every model *and every weight it depends on* has machine-readable license terms. `evaluate()` checks the most restrictive license across that whole set against the operator profile. Whether a plugin is enabled is a separate product decision. | Several top models are territory- or revenue-restricted; base models carry their own terms |
| 0013 | **Provenance on every production export**: a C2PA manifest plus invisible video and audio watermarks, which cannot be disabled in production. In `dev`/`test` before Phase 7, a mock watermarker is allowed. It burns a visible "MOCK PROVENANCE — NOT FOR DISTRIBUTION" label, and production refuses to start with it. | EU AI Act Art. 50, SB 942, platform labeling; a runnable dev system from Phase 2 |
| 0014 | **Auth lives in the API service**: argon2id, httpOnly sessions in Postgres, hashed scoped API keys, and per-org roles plus a separate **platform-admin** role for global resources. OIDC (Keycloak) is a later adapter. | One authority for authorization; safe global settings |
| 0015 | **Pydantic is the schema source of truth.** JSON Schema is exported from it and TypeScript types are generated. | One definition across Python and TypeScript |
| 0016 | **The Director's LLM is a hosted model in production** behind an `LLMProvider` adapter, with a self-hosted OpenAI-compatible option. Dev and test use the **fixture-replay provider** by default. | Planning quality; tests that work without network |
| 0017 | **FFmpeg build and distribution.** Default: a GPL build (libx264, libass) for server use. Distributed images carry license notices and a corresponding-source offer, or else an LGPL build with a non-GPL encoder is used; the product owner decides (section 41). | GPL compliance for a self-hostable product |
| 0018 | **CanonicalBehaviorSpec (CBS).** A versioned, model-independent behavior document per scene sits between the Director and every engine. The engine-specific translation happens only inside adapter-provided `BehaviorTranslator`s. | Creator behavior survives model swaps |
| 0019 | **Situational acting model with closed vocabularies.** Acting is the chain situation → internal state → intent → emotion → expression channels → reaction → editorial response, with emotional trajectories over time. It is not a single emotion label. | Believable behavior and explainable control |
| 0020 | **Director Intent layer and intent policies.** Scene and video intent (what the scene is trying to achieve) is stored in the spec. A deterministic policy engine turns it into defaults for voice, acting, camera, B-roll, editing, captions, music and SFX, and each derived decision is traceable to its intent. | Separates WHAT from HOW; coherent multi-channel direction |
| 0021 | **World DNA is an independent, versioned entity.** Scenes bind a world version, a camera position and time of day, plus non-mutating overrides. Keyframes are composed from creator × wardrobe × world plates. | Environment continuity across videos without creator re-versioning |
| 0022 | **Structured Creator Memory.** Memory is a set of typed items with provenance, confidence, recency, pin, forget, supersede and contradiction states, plus an append-only usage log. Planning reads a pinned MemorySnapshot. | Persistent behavioral identity; reproducible plans; no repetition |
| 0023 | **The requested / compiled / observed triad.** CPU and GPU analyzers measure observed behavior. The results feed QC, the consistency reports and the measured capability profiles of models. | Honesty and model benchmarking |
| 0024 | **Route and seed pinning.** Routes and effective seeds live in the BuildManifest and are reused for clean nodes. QC retries record the winning seed. | Stable caches, true reproducibility |
| 0025 | **Key-addressed spec paths** (`SpecPath`). `EditOperation` is the only way to mutate a spec; lock, take-selection and regenerate endpoints are thin wrappers around it. | Stable addressing across versions; one validation and audit path |
| 0026 | **Creator is the root identity entity.** A CreatorVersion pins Creator DNA plus its default appearance version and voice version. A video's cast pins a creator version, with optional per-video overrides from the same creator. | Clear identity model; per-video variation without identity churn |
| 0027 | **Declared vs measured capability matrices.** Manifests declare behavior capabilities. Benchmarks and production observations produce measured profiles. The router scores on measured data, and the compiler plans on declared capability minus known failures. | Model claims are hypotheses until measured |

---

## 7. Tech stack (use the current stable release of each at build time; verify)

**Python**
- **Python 3.12** for control-plane services. Shared packages that GPU workers import (`ce_contracts`, `ce_worker`) must stay **compatible with Python 3.10+**, because some upstream model repos pin 3.10 or 3.11.
- **uv** workspaces; **ruff** (lint and format); **mypy** (strict on `ce_core`, `ce_contracts`, `ce_behavior`); **pytest**, pytest-asyncio, **hypothesis**.
- **FastAPI** (≥ 0.135, for native SSE support [RV]; fall back to `sse-starlette` if needed), **Pydantic v2**, **pydantic-settings**, **SQLAlchemy 2.1** (async) with **Alembic**, **asyncpg**, **httpx**, **structlog**, **OpenTelemetry**, and **Typer** for the `ce` admin CLI.
- **Temporal** server with the `temporalio` Python SDK. Configure it with `temporalio.contrib.pydantic.pydantic_data_converter` so Pydantic v2 models serialize correctly. Pass IDs between workflows and activities, never whole specs or media, because Temporal has payload and history size limits.

**Data and storage**
- **PostgreSQL 18** with **pgvector**.
- **Redis**, used for Redis Streams (the replayable SSE event log), caching, rate limits and short-lived locks.
- **Embeddings** for research, Creator Memory and repetition checks: a CPU-capable multilingual text-embedding model with an Apache-2.0 or MIT license. Candidates are BGE-M3 and multilingual-E5-large [RV: license at the pinned revision]. It runs behind the `embed.text` capability, with its dimension fixed in config (`EMBEDDING_DIM` ≤ 2,000 so pgvector HNSW indexes work; 1,024 is typical).
- **SeaweedFS** (S3 gateway) locally, with a boto3/aioboto3 client.

**Media and analysis**
- **FFmpeg**, current stable, with libass and libx264 (see ADR 0017). Use FFmpeg's native `aac` encoder, and never ship libfdk-aac.
- **PyAV**, **pysubs2** (ASS captions), **pyloudnorm**, **numpy**, **Pillow**, **OpenCV (headless)**.
- **librosa** (ISC) for beat tracking and prosody features (pitch via pYIN, energy, speech rate, pauses).
- **MediaPipe** Face, Pose and Hand Landmarkers (Apache-2.0), run as CPU adapters.
- **PaddleOCR** (PP-OCR, CPU-capable) and **c2pa-python**.
- **Do not use** Parselmouth/Praat: it is GPL.

**Frontend**
- **Node 24 LTS** (or the current LTS), **pnpm** workspaces, **Next.js 16** (App Router), **React 19**, **TypeScript strict**.
- **Tailwind CSS**, **shadcn/ui**, **TanStack Query**, **Zustand**, **openapi-typescript** with **openapi-fetch**, **Vitest** and **Playwright**.

**Infrastructure**
- **Docker** and **Docker Compose**, with profiles `core`, `mock-gpu`, `cpu-real` and `observability`.
- GPU images: a **CUDA 12.8+** base for Ampere, Ada and Hopper; separate **CUDA 13** tags for Blackwell NVFP4 paths; and **PyTorch** matching each family's upstream requirements.
- Observability: an OpenTelemetry collector, Prometheus, Grafana, Loki and Tempo (an internal, optional profile), plus the NVIDIA DCGM exporter on GPU hosts.

---

## 8. Repository layout

```
creator-engine/
├── apps/
│   ├── web/                    # Next.js 16 frontend
│   ├── api/                    # FastAPI service (public REST + SSE, auth, admin)
│   ├── orchestrator/           # Temporal worker: workflows + CPU activities (Director, behavior, planner, router, QC, policy, memory); cpu_inproc plugins
│   ├── scheduler/              # GPU task queue, leases, fleet manager, cost ledger, worker API (FastAPI + loop)
│   ├── gpu-worker/             # generic worker runtime entrypoint (loads plugins by family; also built as worker-cpu)
│   └── render-worker/          # Temporal activity worker on queue `render` (FFmpeg/PyAV, cpu_inproc plugins)
├── packages/
│   ├── py/
│   │   ├── ce_core/            # domain models: VideoSpec, SpecPath, anchors, tokenizer, IDs, enums, errors, spec utils, vocab loader
│   │   ├── ce_contracts/       # capability interfaces, request/response models, plugin manifest + behavior matrix schema (py>=3.10)
│   │   ├── ce_worker/          # worker runtime: lease loop, artifact I/O, cancellation, model cache (py>=3.10)
│   │   ├── ce_db/              # SQLAlchemy models, Alembic migrations, repositories (org-scoped)
│   │   ├── ce_config/          # layered config loader, YAML schemas, validation, config digests
│   │   ├── ce_creator/         # Creator/Appearance/Voice/Wardrobe DNA models, identity pack logic, Creator Test, baselines
│   │   ├── ce_world/           # World DNA models, world binding + override validation, plates, fingerprints
│   │   ├── ce_memory/          # Creator Memory store, retrieval, snapshots, usage log, repetition guard, contradiction checker
│   │   ├── ce_director/        # Director stages, prompt templates, strategy packs, mode templates, intent policy engine
│   │   ├── ce_behavior/        # acting model, CBS resolver, behavior compiler, coverage, observation comparison
│   │   ├── ce_voice/           # acting-tag parser, prosody plan, normalizers, verification loop, lexicon
│   │   ├── ce_build/           # execution graph, node keys, cache keys, BuildManifest, dirty analysis, impact estimation
│   │   ├── ce_router/          # model router, routing profiles, fallback chains, route pinning
│   │   ├── ce_camera/          # camera profiles, procedural motion, reframing
│   │   ├── ce_realism/         # realism post: grain, LUT, compression, acoustics
│   │   ├── ce_render/          # timeline/EDL, FFmpeg filtergraph compiler, captions, mixing
│   │   ├── ce_qc/              # QC checks, thresholds, aggregation, performance QA, world QC, consistency, critique
│   │   ├── ce_policy/          # license policy, consent, blocklists, testimonial guard, provenance rules
│   │   ├── ce_storage/         # StorageProvider interface + registry
│   │   ├── ce_gpu/             # GPUProvider interface + registry (implementations are plugins)
│   │   ├── ce_llm/             # LLMProvider interface + registry, structured-output repair helpers
│   │   ├── ce_research/        # ingestion, chunking, retrieval, claims, SSRF-guarded fetcher
│   │   ├── ce_obs/             # logging, tracing, metrics helpers
│   │   └── ce_testing/         # fixtures, mock media generators, factories, invariant helpers
│   └── ts/
│       └── api-client/         # generated OpenAPI client + VideoSpec/CBS TS types
├── plugins/                    # each plugin: plugin.yaml + adapter + (if behavior-capable) translator
│   ├── providers/ gpu/{mock,local_docker,runpod_pod,runpod_serverless}/ llm/{anthropic,openai_compatible,fixture}/ storage/{s3,local_fs}/ kms/local/
│   ├── mock/                   # mock adapters for every capability; mock avatar engines with configurable behavior matrices
│   ├── image/ z_image/ qwen_image_edit/
│   ├── video/ wan22_lightx2v/ ltx2/ (manifest-only fixture for license-policy tests; adapter is roadmap)
│   ├── avatar/ infinitetalk/ longcat_avatar/
│   ├── voice/ chatterbox/ qwen3_tts/ voxcpm2/ kokoro_cpu/ (dev only)
│   ├── lipsync/ musetalk/
│   ├── asr/ qwen3_asr/ faster_whisper/ ctc_aligner/
│   ├── audio/ ace_step/ moss_sfx/
│   ├── analysis/ mediapipe_face/ mediapipe_body/ prosody_features/ speaker_embed/ image_embed/ audio_emotion/ (sandbox)
│   ├── vision/ vllm_vlm/ ppocr/ auraface/
│   ├── embed/ text_embed/
│   ├── post/ seedvr2/ rife/
│   ├── qc/ syncnet/ (advisory until license resolved) vqa/ speech_quality/
│   ├── translate/ captions_llm/   # captions.translate via the LLM provider
│   ├── provenance/ videoseal/ audioseal/ c2pa/
│   ├── effects/ transitions/ titles/ lower_thirds/ disclosure/
│   ├── camera/ luts/ motion_models/
│   ├── captions/ ass_renderer/
│   └── research/ url_fetch/ file_ingest/ web_search_stub/
├── config/
│   ├── default.yaml
│   ├── env/{dev,test,prod}.yaml
│   ├── languages.yaml           # per language: support (production|beta|unsupported), normalizer, aligner preference
│   ├── vocab/                   # closed, versioned vocabularies (each file carries vocab_version)
│   │   ├── emotions.yaml        # labels, families, valence/arousal, compatible strategies
│   │   ├── acting.yaml          # situation kinds, internal states, social/audience goals, performance intents, strategies, postures, transition styles
│   │   ├── behavior_events.yaml # look_away, glance_at_element, small_smile, eyebrow_raise, small_laugh, nod, sigh, shrug, head_tilt, double_take, …
│   │   ├── behavior_dimensions.yaml   # dimensions used by behavior matrices, coverage and observation
│   │   ├── observation_proxies.yaml   # measurable proxies per dimension/label with reliability
│   │   ├── descriptions.yaml    # model-agnostic natural-language descriptions of vocab items (for text-prompted translators and the LLM)
│   │   ├── intent.yaml          # narrative/emotional/persuasion/information/attention goals, audience effects, reveal + performance strategies
│   │   ├── memory_kinds.yaml
│   │   ├── world_elements.yaml  # element kinds, standard states
│   │   └── edit_vocabulary.yaml # lock groups ↔ SpecPath patterns ↔ regenerate components ↔ node kinds
│   ├── intent_policies.yaml     # intent → defaults for voice, acting, camera, B-roll, edit, captions, music, SFX
│   ├── memory.yaml              # retrieval budgets, recency decay, promotion thresholds, repetition windows
│   ├── models/*.yaml            # model registry seeds (license blocks incl. dependencies)
│   ├── routing/*.yaml           # routing profiles: draft, final, cheapest
│   ├── camera_profiles/*.yaml
│   ├── mic_profiles/*.yaml
│   ├── rooms/*.yaml + impulse_responses/*.wav
│   ├── luts/*.cube
│   ├── modes/*.yaml             # video mode templates
│   ├── strategy_packs/*.yaml|md
│   ├── caption_styles/*.yaml
│   ├── platforms/*.yaml         # tiktok, instagram_reels, youtube_shorts, youtube, linkedin, x: rules + render presets, each with verified_at
│   ├── qc/{draft,final}.yaml + qc/behavior.yaml + qc/world.yaml + qc/consistency.yaml
│   ├── policy/*.yaml            # blocklists, testimonial rules, operator profile defaults
│   └── gpu/pools.yaml
├── prompts/                     # versioned Director prompt templates (prompts/<stage>/<version>.md)
├── eval/                        # golden evaluation set: scripts, creators, worlds, seeds, expected checks, behavior fixtures
├── infra/
│   ├── docker/                  # Dockerfiles: api, orchestrator, scheduler, render-worker, web, worker-cpu, worker-<family>
│   ├── compose/                 # docker-compose.yml (+ profiles)
│   ├── observability/           # dashboards and alert rules as code
│   └── k8s/                     # later: Helm charts
├── scripts/                     # bootstrap, fetch-cpu-assets, smoke tests per GPU plugin, benchmarks, calibration
├── tests/                       # cross-service integration, invariants/, e2e (Playwright in apps/web/e2e)
├── docs/                        # see section 38
├── .github/                     # CI workflows, PR template with the rule-20 checklist
├── .env.example
├── Makefile
├── TODO.md
├── ROADMAP.md
├── CHANGELOG.md
└── README.md
```

---

## 9. Services and workflows

| Service | Responsibility | Notes |
| --- | --- | --- |
| `web` | All UI. Talks only to `api` (REST + SSE). | Generated typed client; no business logic duplicated from the backend |
| `api` | Auth, tenancy, CRUD, validation, presigned uploads, starting workflows, SSE event stream, read models for the UI, cheap synchronous estimates. | Stateless. **Never calls GPUs or LLMs directly**: anything that needs them is a job (`202`) |
| `orchestrator` | Temporal workflows and CPU activities: Director stages, intent policies, memory retrieval and snapshots, CBS resolution, behavior compilation, build planning, routing, QC aggregation, observation comparison, policy checks, critique. Dispatches model nodes (GPU and `cpu_model`) to `scheduler` and render work to queue `render`. Loads `cpu_inproc` plugins in-process. | Workflow code must be deterministic: no I/O in workflow functions, I/O only in activities |
| `scheduler` | GPU task table and leases, model-affinity placement, priorities, fleet manager (provision, idle shutdown, budgets), model cache registry, cost ledger, worker registry, and the internal worker API. | Single active leader via a Postgres advisory lock; otherwise stateless |
| `gpu-worker` | Registers with its capabilities and hardware; long-polls for leases; loads plugins and models; executes; uploads artifacts through presigned URLs; heartbeats; honors cancellation. | Outbound HTTPS only; per-worker token. The `worker-cpu` build runs the `cpu_model` family: mock adapters and CPU engines |
| `render-worker` | Timeline compile, camera and realism post, acoustics, captions, mix, encode, watermark, C2PA, proxies, render-level measurements. Loads `cpu_inproc` plugins (effects, captions, provenance). | CPU; Temporal activity worker on queue `render` |

Infrastructure: Postgres 18 with pgvector, Redis, the Temporal server (and its UI), SeaweedFS, and an optional observability stack.

**Workflows.** Each job kind maps to exactly one workflow, and each workflow writes a `generation_jobs` row (`kind` equals the workflow's job kind):

| Job kind | Workflow | Notes |
| --- | --- | --- |
| `plan` | `PlanVideoWorkflow` | Director stages 1–11 → planned version |
| `previz` | `PrevizWorkflow` | TTS + verify + align + world plates + one keyframe per shot + coverage prediction → `previz_ready` |
| `generate` | `GenerateVersionWorkflow` | Builds every dirty node of a version. One child `SceneBuildWorkflow` per scene. The parent owns the video-level nodes: music, captions, mix, the renders listed in `spec.render.outputs`, the proxy, and `behavior.coverage`. When the version is `ready`, it starts `ConsistencyWorkflow` and writes the usage events (§18.3) |
| `regenerate` | `GenerateVersionWorkflow` | Same workflow, started on a new version created by a `regenerate` operation. Components that need the LLM (`acting`) go through `edit_propose` in replan mode and are then auto-applied (§12.7) |
| `edit_propose` | `ProposeEditWorkflow` | LLM → operations → patch → impact (async; emits `edit.proposed`) |
| `edit_apply` | `ApplyEditWorkflow` | New version, then `GenerateVersionWorkflow` for its dirty nodes |
| `render` | `RenderWorkflow` | Extra render presets requested after the version is `ready` (§30). The output is `renders` rows cached by key, outside the frozen BuildManifest |
| `critique` | `CritiqueWorkflow` | Creative Director critique |
| `package` | `PackagingWorkflow` | Director stage 12 |
| `identity_pack` | `BuildIdentityPackWorkflow` | Appearance identity pack, including the VLM apparent-age check after the canonical face is chosen |
| `world_plates` | `BuildWorldPlatesWorkflow` | Plate candidates per camera position × time of day × weather. It also runs in `fingerprint` mode when a plate is chosen |
| `wardrobe_refs` | `BuildWardrobeWorkflow` | Wardrobe reference images, conditioned on the canonical face (§17.2) |
| `creator_test` | `CreatorTestWorkflow` | Creator Test scorecard and baselines |
| `voice_design` | `VoiceDesignWorkflow` | Voice candidates |
| `voice_test` | `VoiceTestWorkflow` | Test bench synthesis and metrics |
| `memory_update` | `MemoryUpdateWorkflow` | Write paths after approval and export (section 18) |
| `consistency` | `ConsistencyWorkflow` | Cross-video creator and world consistency. It runs after `ready`, outside the build graph, because its inputs (baselines, other videos) change over time |
| `research_ingest` | `ResearchIngestWorkflow` | Sources → facts → embeddings |
| `screen_analysis` | `ScreenAnalysisWorkflow` | Screen-recording analysis |
| `asset_validation` | `AssetValidationWorkflow` | ffprobe, type sniff, rights |
| `benchmark` | `BenchmarkWorkflow` | Golden set, pairwise items, behavior profiles |
| `calibration` | `CalibrationWorkflow` | Engine knob calibration (§15.7), started by `POST /v1/admin/models/{model_id}:calibrate` |
| `retention` / `deletion` | `RetentionWorkflow` / `DeletionWorkflow` | Data lifecycle |
| `lora_train` | `IdentityTrainingWorkflow` | V1; mock in MVP |
| `autonomous_suggest` | `AutonomousSuggestWorkflow` | V1; feature flag `autonomous_suggest_enabled=false` |

---

## 10. Domain model, layers and conventions

This section defines the vocabulary every later section uses. Each concept is defined once; later sections refer back here.

### 10.1 The layer stack

```
User input (idea | structured brief | brain dump | exact script)
  └─▶ CreativeBrief                         (§13 stage 1)
        └─▶ Director plan  ──────────────▶ VideoSpec (immutable per version; §11)
              ├─ Director Intent (video + scene)                      §14
              ├─ Script (segments, annotations)                       §11
              ├─ Scenes, cast, World bindings, shots                  §11, §19
              └─ Situational acting (states, trajectory, events)      §15
VideoSpec scene + CreatorVersion DNA + MemorySnapshot + WorldVersion
  └─▶ CanonicalBehaviorSpec (CBS, per scene; model-independent)       §15
        └─▶ Behavior Compiler + adapter capability matrix (+ route)
              └─▶ CompiledBehavior (methods, coverage levels, abstract directives)
                    └─▶ BehaviorTranslator (inside the plugin) ─▶ engine request
                          └─▶ Artifacts (takes, chunks, renders)       §12
                                └─▶ ObservedBehavior (analyzers)       §16
                                      └─▶ BehaviorCoverageReport: requested vs compiled vs observed
```

Everything above the Behavior Compiler is **WHAT**: creative and behavioral intent, independent of any model. Everything from the compiler down is **HOW**: engine-specific execution. Invariant I1 enforces the boundary.

### 10.2 Identity entities

| Entity | Versioned record | Holds | Scope |
| --- | --- | --- | --- |
| **Creator** | `CreatorVersion` | Creator DNA (§17): persona, speech, behavior, gesture, gaze, camera, fashion, world and editing preferences, avoidances, canon. DNA holds tendencies only. References live in columns: the default `appearance_version_id`, the default `voice_version_id`, `default_world_ids` and `default_wardrobe_version_ids`. | org |
| **Appearance** | `AppearanceVersion` | Visual identity: appearance DNA, the canonical face asset, the identity pack and LoRA artifacts (V1). Belongs to one creator. | creator |
| **Voice** | `VoiceVersion` | Voice identity: references, description, prosody defaults, lexicon, WPM, cross-language notes (§21). Belongs to a creator, or is an org preset. | creator or org |
| **Wardrobe** | `WardrobeVersion` | One outfit: description, reference assets, constraints. | creator |
| **World** | `WorldVersion` | World DNA (§19): geometry, elements, camera positions, lighting, time of day, acoustics, plates, fingerprints. | org (a creator may list it as a default) |
| **Product** | `ProductVersion` | Product description, assets, allowed claims. | org |

**Lifecycle.** Every versioned record has the status `draft → approved → archived`.
- Drafts are mutable. This is where identity-pack choices, lexicon edits, WPM calibration and world-plate approvals happen.
- Approved versions are immutable (I3). Changing one creates a new draft version from it, carrying a `parent_version_id`.
- Specs may reference only approved versions. Creator Tests and World Studio previews may also use drafts, and their results are marked accordingly.
- **The one exception is a world proposal.** When planning needs a world that does not exist, the Director creates a draft world version (§19.2) and binds the scene to it. The version carries the flag `needs_world_approval`. While the flag is set, previz runs only its audio parts and `:approve` is refused. Approving the draft world keeps its id, so the spec reference becomes valid without changing the spec; the flag is cleared and previz completes. Choosing an existing world instead is an edit that creates a derived version (§12.8).
- Model-dependent checks needed for approval (the apparent-age check, world fingerprints, identity scores) run inside jobs. `:approve` endpoints only verify that the recorded results exist and passed.

**Creator Memory** (§18) is not a versioned record. It is a curated, append-mostly store of typed items attached to a creator. Each item records the creator-version range it applies to. Planning reads a pinned, immutable **MemorySnapshot** (I7).

### 10.3 Canonical terms (use these names; avoid the synonyms)

| Term | Meaning | Do not call it |
| --- | --- | --- |
| creator | the persistent persona (root identity) | avatar (as an identity), character (as an identity) |
| appearance | a creator's visual identity record | avatar |
| avatar engine | a model that animates a person from image + audio (`avatar.a2v`) | — |
| cast member / character | a creator's appearance in one video (`cast[].key = char_…`) | — |
| wardrobe | one outfit record | outfit (UI label only), fashion item |
| world | a recurring environment record (World DNA) | room, set, background, environment (UI may say "environment") |
| world plate | the canonical image of a world from one camera position and time of day, without people | background |
| background | the visible world portion behind a subject in a rendered shot; regenerating it creates a new plate variation of the same world version | — |
| room acoustics | the acoustic profile of a world (`config/rooms`) | room (for visuals) |
| intent | what a video or scene is trying to achieve (§14) | — |
| acting state | one span of a scene's emotional trajectory (§15) | beat (reserved for music beats) |
| behavior event | a discrete behavior at a word or time (look_away, nod…) | — |
| music beat | a tempo beat in the music bed | beat (alone) |
| CBS | CanonicalBehaviorSpec, the per-scene resolved behavior document | — |
| coverage level | HONORED / APPROXIMATED / UNSUPPORTED (predicted execution) | — |
| observation verdict | CONFIRMED / PARTIAL / NOT_OBSERVED / CONTRADICTED / NOT_MEASURABLE / NOT_APPLICABLE | — |
| job / workflow / node / attempt / task | user-visible unit / Temporal workflow / build-graph node / one execution try of a node / scheduler's GPU queue entry for an attempt | — |
| lip-sync patch | the build node `lipsync.patch` that calls the capability `lipsync.dub` | — |
| restore | copy an older spec forward as a new current version (this is the only "rollback") | rollback (as a separate operation) |
| spec template | a partial spec saved by a user (`spec_templates`) | template (alone) |
| mode | a video mode template (`config/modes`) | template (alone) |
| render preset | an encode preset in `config/platforms/*.yaml` | preset (alone) |
| packaging | the per-platform title, description, hashtags and thumbnails produced by Director stage 12 | export (an export is a packaged render download) |
| consent | a verified consent record (`consents`, `consent_id`) | consent record id |

### 10.4 Status vocabularies (one meaning each)

- **Plugin/model `status`**: `production | sandbox | experimental | research_only | disabled`. Routing uses `production` in normal runs and `sandbox` only in sandbox runs.
- **Plugin/model `validation`**: `untested_on_gpu | smoke_passed | bench_passed | failed`. These values are evidence, separate from `status`. `models.promotion_basis` (`smoke | bench`) records what a promotion relied on.
- **Feature `maturity`** (for modes, camera profiles and features): `production | beta | experimental`. This is the *design* maturity. The *effective* maturity shown in the UI is capped by the validation of the mode's default routes. For example, a `production` mode whose talking-head or TTS route is still `untested_on_gpu` shows as "production design, unvalidated routes".
- **Language `support`** (`config/languages.yaml`): `production | beta | unsupported`.
- **Roadmap bucket**: `MVP | V1 | V2 | Experimental`.
- **Record lifecycle**: `draft | approved | archived`.

### 10.5 Identifiers and paths

- **Database entities** (videos, versions, creators, worlds, assets, jobs…) have UUIDv7 primary keys and are referenced as `*_id`.
- **Spec elements** have stable, prefixed string keys referenced as `*_key`. A key is generated once and preserved across versions; projection tables store it in `*_key` columns. The prefixes are:
  - `char_` cast member, `seg_` segment, `an_` annotation, `clm_` claim, `hk_` hook candidate;
  - `scn_` scene, `sht_` shot, `tk_` take, `mv_` camera move;
  - `st_` acting state, `ev_` behavior event;
  - `mc_` music cue, `sfx_` SFX event, `fx_` effect, `zm_` screen zoom.

  World DNA uses the same convention for its own elements: `el_` element, `cam_` camera position, `zone_` placement zone.
- **Take keys** are derived from the take index (`tk_1`, `tk_2`…), so changing `takes.count` never renames existing takes.
- **SpecPath** is a key-addressed path syntax used by locks, patches, `derived_from` and coverage references. Examples: `/cast[char_alex]/overrides/voice_version_id`, `/scenes[scn_hook]/acting/states[st_2]/emotion/displayed`. Glob `[*]` is allowed only in lock and vocabulary patterns. Array indices are never used. `ce_core.spec.paths` parses, resolves and globs SpecPaths. A display-only RFC 6902 rendering is generated for diffs.
- **Language codes** are BCP-47 everywhere (`en-US`, `tr-TR`, `az-Latn-AZ`, `ar`, …). Engine language matching compares the primary subtag unless a manifest declares regional variants.

### 10.6 Resolution order for values defined at more than one level

The table uses two notations:
- `A ⊳ B ⊳ C` means **override with bounds**: later items override earlier ones, and earlier items bound later ones.
- `A, else B` means **fallback**: B is used only when A is absent.

| Value | Resolution |
| --- | --- |
| Behavior baseline and ranges (energy, confidence, warmth, expressivity, per-emotion intensity ranges, gaze, gesture and head-motion tendencies) | Creator DNA ⊳ active memory habits from the pinned snapshot (adjust defaults within DNA bounds) ⊳ scene acting states (absolute values, clamped to DNA bounds unless the state sets `out_of_character: {allowed: true, reason}`, which previz flags) ⊳ user tags and edits (normalized into acting states and events by one normalizer). Voice prosody defaults feed only the voice translator's baseline. |
| Habits and social traits (reaction, gesture and gaze habits; directness, warmth, sarcasm, seriousness) | Creator DNA ⊳ active memory items of the same kind (they move the value within DNA bounds). Conflicts follow §18.7. |
| Signature and recurring phrases | The union of the DNA signature phrases and active `recurring_phrase` memories. Usage is capped by each phrase's `max_per_video` and by the repetition windows (§18.6). |
| Avoidances | The union of the DNA avoidances and active `avoidance` memories. An avoidance is never overridden by a habit. |
| Speech rate | Measured WPM on the VoiceVersion, adjusted by the DNA `speech_rate_preference`. |
| Pauses | `pause` annotations are the only representation. `set_pacing` edits them; DNA and memory supply the defaults the Director uses when writing them. |
| Emotion requested by a user tag (`[excited]`) | The tag parser converts it into an acting-state override for its span (`source: user_tag`), and audio-only tags (`[laughs]`, `[sigh]`, `[whispers]`, `[breath]`) into annotations. An event and an annotation may cover the same span only when they address different dimensions: a visual `small_laugh` event with a `nonverbal_audio` laugh annotation is the standard pairing. Same-dimension duplicates are rejected. |
| Wardrobe | `scene.cast[].wardrobe_version_id`, else the creator version's default wardrobe. |
| World | `scene.world`, else the creator version's default world, else a world proposal by the Director (§19.2). |
| Room acoustics and room tone | `audio.acoustics.source = world` (the default; per scene, from the bound world version + overrides) or `override` (an explicit room and mic profile). |
| Mic profile | `audio.acoustics.mic_profile`, else the camera profile's `audio.mic_profile`. |
| Overscan | The camera profile's `framing.overscan`. There is no spec-level overscan. |
| Loudness | `audio.loudness` is the master target. A render preset may declare `loudness_override` (for example a broadcast preset), in which case that output is re-normalized from the stems. |
| Visible AI label | `provenance.visible_label` (`auto` by default); `auto` resolves from the operator profile and the regions served. `VISIBLE_LABEL_DEFAULT` sets only the default for new videos. |
| Camera profile and edit grammar | Creator DNA camera preferences ⊳ mode template defaults ⊳ scene intent policy ⊳ shot camera. |
| Voice prosody for one video | VoiceVersion `default_prosody` ⊳ `cast[].voice_prosody` (a per-video offset) ⊳ CBS prosody directives. |
| Mock switches | `LLM_PROVIDER=fixture` (there is no separate `MOCK_LLM`). `MOCK_GPU=true` registers the mock GPU provider and the mock adapters, which run on `worker-cpu`. The Compose profiles `mock-gpu` and `cpu-real` only choose which adapters the `worker-cpu` container loads. |

---

## 11. The canonical VideoSpec (creative source of truth)

Define the VideoSpec in `ce_core.spec` as Pydantic v2 models. Export its JSON Schema to `packages/ts/api-client/schema/videospec.schema.json`, generate TypeScript types from it, and snapshot-test the exported schema. The first implemented schema is `schema_version: "1.0"`; write migration functions for future versions. Every spec also records `vocab_version`, which is validated against `config/vocab/`.

The data flows in this order:
`CreativeBrief` (interpretation) → `VideoSpec` (what the video is, including intent and acting) → `CanonicalBehaviorSpec` (derived per scene) → `ExecutionGraph` (how to build it; derived) → `Artifacts` (content-addressed files) → `ObservedBehavior`.

**Rules**

- **Tokenizer.** `ce_core.text.tokenize` is the canonical word tokenizer. Word indices in anchors are positions in its output.
  - It splits on Unicode word boundaries (UAX #29 through the `regex` module).
  - Punctuation attaches to the preceding word, and in-word apostrophes and hyphens are kept.
  - It is deterministic and versioned (`tokenizer_version` in the spec). Every node that resolves anchors includes `tokenizer_version` in its cache key.
  - Tests cover en, de, es, tr, az, ru and ar. Scripts without spaces are out of scope (roadmap).
- **Anchors, not seconds.** Every timed field uses one of these types:
  - `WordRef {segment_key, word}`;
  - `WordSpan {kind: "words", start: WordRef, end: WordRef}`, inclusive, which may cross segments of the same scene in order;
  - `SegmentRef {segment_key, edge: start|end}`;
  - `ShotRef {kind: "shot", shot_key, offset_ms}`;
  - `DurationSpan {kind: "duration", after: ShotRef | SegmentRef, duration_ms}`;
  - `SceneSpan {kind: "scene", scene_key}`.

  A resolver maps anchors to seconds using the alignment artifact. In previz before alignment, and in the UI, estimated times come from calibrated WPM.
- **Shot layers.**
  - **Base** shots (`layer: "base"`) tile the scene's speech span without gaps or overlaps (validator). Non-speech base shots (title cards, intro/outro, silent reaction holds) use `DurationSpan`.
  - **Overlay** shots (`layer: "overlay"`: B-roll, inserts, screen segments, product inserts) cover part of a base shot. Dialogue continues underneath, and the timeline puts the overlay on the video track for its span.
- **Exact-script spans are character offsets.** In exact-script mode the Director never re-types text. The LLM returns character offsets into `brief.raw_input`, and code slices the raw input to build segment text. Canonical acting tags typed by the user are removed from the text and converted by the tag parser (§21): audio tags become annotations, and visual tags become acting events or state overrides with `source: user_tag`. Every other character is preserved byte for byte, with no Unicode normalization of stored text.
- **Segments** have `key`, `speaker_key`, `text`, `language`, `annotations`, `claim_keys`, and an optional `quote_source {consent_id, asset_id}` for real customer quotes (§32).
- **Script annotations** have `key`, `type`, `tag` (a token of that type's vocabulary), `span` and `source`. The types are: `pause`, `emphasis`, `nonverbal_audio` (laugh, sigh, breath), `delivery` (whisper, aside), `pronunciation` and `inserted_disfluency`. An `inserted_disfluency` marks filler words the Director added for naturalness; it is allowed only when wording is unlocked, and the verification loop and later edits treat it as authored-by-system. `script.wording_locked` is a **read-only computed property**: true when the `script` lock group is active. It is exposed in API responses and rejected on input. While it is true, any patch touching `/script/segments[*]/text` is rejected.
- **Intent and acting are first class.**
  - `intent.video` and `scenes[].intent` follow §14.
  - `scenes[].acting` follows §15. It holds a situation, ordered acting `states` that form the emotional trajectory, and keyed `events`. Every state and event carries a `character_key` (it defaults to the scene's only character) and a `source`.
  - `scenes[].pacing {target_wpm_delta, cut_cadence}` holds optional pacing adjustments that `set_pacing` edits.
  - The Director writes every requested behavior, including behavior no current engine can execute. Execution is decided later by the compiler, never by deleting intent.
- **World binding** (`scenes[].world`, §19): `world_version_id`, `camera_position_key`, `time_of_day`, `weather`, non-mutating `overrides`, and a typed `continuity_ref`.
- **Cast.**
  - `cast[]` holds `{key, role (host | guest | narrator), creator_version_id, overrides {appearance_version_id?, voice_version_id?}, voice_prosody {rate, pitch_semitones, energy}?}`. Overrides must belong to the same creator, or be org voice presets, and the consistency report flags them.
  - `scenes[].cast[]` sets each character's `wardrobe_version_id`, `placement` (a world zone key) and `default_posture` for that scene.
- **Keyframe strategy** (`shots[].visual.keyframe.strategy`): `composite` (the default; §17.2) or `asset` (a user-supplied frame).
- **Shot types** (closed enum): `talking_head`, `broll`, `insert`, `screen`, `product`, `title_card`, `reaction_clip`, `silent_hold`, `two_shot`. `two_shot` is rejected while `multi_character_enabled=false`.
- **Products.** `products[]` lists `{key, product_version_id}`. A shot of type `product` is an asset insert or B-roll of the product. Nothing is generated in hand in the MVP.
- **Reactions.** A `reaction_clip` shot carries `reaction_source {asset_id, range_s, layout: pip|split}`, the clip being reacted to.
- **Screen shots** have typed `zooms [{key, span, rect, ease}]`, `speed_segments [{span, speed}]` and `webcam_bubble {enabled, corner, size}`.
- **Traceability.** Shots, camera moves, music cues, SFX and effects may carry `derived_from: [{kind: intent|acting|dna|memory|policy|compiler_approximation|user, ref: SpecPath, route_digest?}]`, so the Studio can show why a decision was made. Entries of kind `compiler_approximation` carry the `route_digest` of the route they were planned for (§15.7).
- **Memory.** `memory.snapshots: [{character_key, snapshot_id}]` pins one MemorySnapshot per cast member (I7). Derived versions inherit their parent's snapshots unless a `refresh_memory` operation is applied.
- **Seeds.** `generation.seed_namespace` is set to the `video_id` at creation and copied on duplicate, so a duplicated video reuses its source's cache.
- **Engines are never named in the spec**, except in `generation.engine_hints {node_key: adapter_id}`, which records user pins. A pin is honored only if it passes the router's hard filters.
- **Seed overrides.** `generation.seed_overrides {node_key: seed}` holds seeds the user explicitly changed (§12).
- **Locks** are `{group, scope: {scene_keys?, character_keys?, shot_keys?}, set_by}`. Groups come from `config/vocab/edit_vocabulary.yaml`, which expands each group into SpecPath patterns (§12).
- **Validators**:
  - unique keys;
  - anchors in bounds and tiling;
  - durations plausible for the target (± tolerance);
  - references exist and are approved (creator, appearance, voice, wardrobe, world, product versions, assets);
  - world bindings valid against the World DNA (camera position exists, override elements and states exist, lighting deltas within world tolerance unless `deviation_declared: true`);
  - acting states tile each scene's speech span or explicitly carry the previous state (`carry: true`);
  - all control values come from the declared `vocab_version`;
  - lock groups and scopes are valid;
  - every character used has an approved creator version;
  - provenance fields satisfy section 32.

**Example** (valid JSON, abbreviated to one scene; `script.wording_locked` is omitted because it is computed; implement the full model):

```json
{
  "schema_version": "1.0",
  "vocab_version": "2026.10.1",
  "tokenizer_version": "1",
  "video_id": "0192f0a0-0000-7000-8000-000000000001",
  "version_id": "0192f0a0-0000-7000-8000-000000000002",
  "parent_version_id": null,
  "meta": {
    "title": "Why most people misunderstand AI agents",
    "mode": "talking_head_explainer",
    "language": "en-US",
    "platform_targets": ["tiktok", "youtube_shorts"],
    "primary_aspect": "9:16",
    "target_duration_s": 30,
    "quality_tier": "draft",
    "template_ids": [],
    "strategy_pack": "myth_vs_reality"
  },
  "brief": {
    "input_mode": "idea",
    "raw_input": "Create a 30-second TikTok explaining why most people misunderstand AI agents.",
    "audience": "curious non-technical professionals",
    "angle": "agents are workflows with judgment, not smarter chatbots",
    "assumptions": ["No sources supplied; claims kept general and non-statistical."],
    "hook_candidates": [
      {"key": "hk_1", "text": "Everyone thinks AI agents are just smarter chatbots."},
      {"key": "hk_2", "text": "You've been sold the wrong idea about AI agents."}
    ],
    "selected_hook_key": "hk_1",
    "sources_policy": "open",
    "constraints": []
  },
  "intent": {
    "video": {
      "narrative_goal": "challenge_common_belief",
      "audience_effect": "insight",
      "persuasion_goal": "reframe_mental_model",
      "information_goal": "define_concept",
      "emotional_arc": ["confident", "curious", "serious"],
      "attention_goal": "sustain",
      "cta_goal": "follow_for_series"
    }
  },
  "memory": {
    "snapshots": [{"character_key": "char_alex", "snapshot_id": "0192f0a0-0000-7000-8000-0000000000a1"}]
  },
  "cast": [
    {
      "key": "char_alex",
      "role": "host",
      "creator_version_id": "0192f0a0-0000-7000-8000-000000000011",
      "overrides": {"appearance_version_id": null, "voice_version_id": null}
    }
  ],
  "products": [],
  "script": {
    "segments": [
      {
        "key": "seg_1",
        "speaker_key": "char_alex",
        "text": "Everyone thinks AI agents are just smarter chatbots.",
        "language": null,
        "annotations": [
          {"key": "an_1", "type": "emphasis", "tag": "emphasize", "span": {"kind": "words", "start": {"segment_key": "seg_1", "word": 6}, "end": {"segment_key": "seg_1", "word": 6}}, "source": "director"}
        ],
        "claim_keys": []
      },
      {
        "key": "seg_2",
        "speaker_key": "char_alex",
        "text": "But here's the thing... they're not.",
        "language": null,
        "annotations": [
          {"key": "an_2", "type": "pause", "tag": "short_pause", "span": {"kind": "words", "start": {"segment_key": "seg_2", "word": 3}, "end": {"segment_key": "seg_2", "word": 3}}, "source": "intent_policy"},
          {"key": "an_3", "type": "emphasis", "tag": "emphasize", "span": {"kind": "words", "start": {"segment_key": "seg_2", "word": 5}, "end": {"segment_key": "seg_2", "word": 5}}, "source": "intent_policy"}
        ],
        "claim_keys": []
      }
    ]
  },
  "scenes": [
    {
      "key": "scn_hook",
      "purpose": "hook",
      "order": 1,
      "segment_keys": ["seg_1", "seg_2"],
      "intent": {
        "narrative_goal": "challenge_common_belief",
        "emotional_goal": "create_doubt_then_reveal",
        "audience_effect": "curiosity",
        "persuasion_goal": "none",
        "information_goal": "state_misconception",
        "attention_goal": "delay_payoff",
        "reveal_strategy": "tease_then_reveal",
        "tension_level": 0.6,
        "curiosity_level": 0.8,
        "performance_strategy": "start_confident_then_lower_energy",
        "notes": "Say the common belief as if agreeing, then undercut it."
      },
      "world": {
        "world_version_id": "0192f0a0-0000-7000-8000-000000000020",
        "camera_position_key": "cam_desk_front",
        "time_of_day": "late_afternoon",
        "weather": "clear",
        "overrides": {"element_states": {"el_monitor": "on"}, "hide_elements": [], "add_elements": [], "move_elements": [], "lighting": null, "acoustics": null},
        "continuity_ref": {"kind": "world_plate", "camera_position_key": "cam_desk_front"}
      },
      "cast": [
        {"character_key": "char_alex", "wardrobe_version_id": "0192f0a0-0000-7000-8000-000000000030", "placement": "zone_desk_chair", "default_posture": "seated_upright"}
      ],
      "acting": {
        "situation": {
          "kind": "contradicting_the_audience",
          "description": "Alex states the popular belief as if agreeing, then realizes the viewer probably believes it too and decides to challenge it.",
          "audience_stance": "agrees_with_misconception",
          "stimulus": null
        },
        "states": [
          {
            "key": "st_1",
            "character_key": "char_alex",
            "source": "director",
            "span": {"kind": "words", "start": {"segment_key": "seg_1", "word": 0}, "end": {"segment_key": "seg_1", "word": 7}},
            "internal_state": {"label": "amused_certainty", "description": "Knows the belief is wrong and finds it a little funny."},
            "social_goal": "build_rapport",
            "audience_goal": "make_viewer_nod_along",
            "performance_intent": "mock_agreement",
            "emotion": {
              "felt": {"label": "amused", "intensity": 0.4},
              "displayed": {"label": "confident", "intensity": 0.6},
              "masking": true
            },
            "confidence_delta": 0.0,
            "attention_target": "camera",
            "strategies": {
              "prosody": "assertive_light",
              "gaze": "hold_camera",
              "gesture": "illustrative_light",
              "posture": "seated_upright",
              "reaction": "suppressed",
              "camera_awareness": "direct_address"
            },
            "transition_in": null,
            "priority": "should"
          },
          {
            "key": "st_2",
            "character_key": "char_alex",
            "source": "director",
            "span": {"kind": "words", "start": {"segment_key": "seg_2", "word": 0}, "end": {"segment_key": "seg_2", "word": 5}},
            "internal_state": {"label": "deliberate_reveal", "description": "Drops the act; wants the viewer to feel the turn."},
            "social_goal": "establish_authority",
            "audience_goal": "create_doubt",
            "performance_intent": "undercut_belief",
            "emotion": {
              "felt": {"label": "serious", "intensity": 0.6},
              "displayed": {"label": "serious", "intensity": 0.6},
              "masking": false
            },
            "confidence_delta": 0.15,
            "attention_target": "camera",
            "strategies": {
              "prosody": "slow_measured",
              "gaze": "glance_away_and_return",
              "gesture": "still",
              "posture": "seated_lean_in",
              "reaction": "none",
              "camera_awareness": "direct_address"
            },
            "transition_in": {
              "trigger": {"kind": "realization", "at": {"segment_key": "seg_2", "word": 0}, "description": "Realizes the viewer agrees with the misconception."},
              "style": "sudden",
              "duration_words": 1
            },
            "priority": "must"
          }
        ],
        "events": [
          {"key": "ev_1", "character_key": "char_alex", "type": "look_away", "at": {"segment_key": "seg_2", "word": 2}, "duration_ms": 700, "direction": "down_left", "intensity": 0.5, "purpose": "thinking", "priority": "should", "source": "director"},
          {"key": "ev_2", "character_key": "char_alex", "type": "small_smile", "at": {"segment_key": "seg_2", "word": 5}, "duration_ms": 600, "direction": null, "intensity": 0.3, "purpose": "knowing", "priority": "nice", "source": "director"}
        ]
      },
      "shots": [
        {
          "key": "sht_1",
          "type": "talking_head",
          "layer": "base",
          "span": {"kind": "words", "start": {"segment_key": "seg_1", "word": 0}, "end": {"segment_key": "seg_2", "word": 5}},
          "character_key": "char_alex",
          "camera": {
            "profile_id": "phone_front_selfie",
            "framing": "medium_close_up",
            "angle": "eye_level",
            "moves": [
              {"key": "mv_1", "type": "punch_in", "at": {"segment_key": "seg_2", "word": 0}, "scale": 1.12, "transition": "cut", "derived_from": [{"kind": "intent", "ref": "/scenes[scn_hook]/intent/reveal_strategy"}]}
            ]
          },
          "visual": {"keyframe": {"strategy": "composite", "asset_id": null}, "prompt_extra": "", "negative": ""},
          "takes": {"count": 2, "selected_take_key": null}
        },
        {
          "key": "sht_2",
          "type": "broll",
          "layer": "overlay",
          "span": {"kind": "words", "start": {"segment_key": "seg_2", "word": 2}, "end": {"segment_key": "seg_2", "word": 3}},
          "character_key": null,
          "camera": {"profile_id": "desk_mirrorless", "framing": "insert", "angle": "high", "moves": []},
          "broll": {
            "source": "generate",
            "asset_id": null,
            "prompt": "close-up of a chat window on a laptop screen, shallow depth of field, soft office light",
            "world_bound": true,
            "allow_text_in_frame": false
          },
          "takes": {"count": 1, "selected_take_key": null},
          "derived_from": [{"kind": "compiler_approximation", "ref": "/scenes[scn_hook]/acting/events[ev_1]", "route_digest": "sha256:…"}]
        }
      ]
    }
  ],
  "audio": {
    "music": {"cues": [{"key": "mc_1", "span": {"kind": "scene", "scene_key": "scn_hook"}, "mode": "generate", "mood": "curious, light electronic, no vocals", "bpm": 96, "asset_id": null, "duck_db": -18}]},
    "sfx": [{"key": "sfx_1", "at": {"kind": "shot", "shot_key": "sht_2", "offset_ms": 0}, "description": "soft whoosh", "gain_db": -10}],
    "acoustics": {"source": "world", "mic_profile": null},
    "loudness": {"integrated_lufs": -14, "true_peak_dbtp": -1}
  },
  "captions": {
    "enabled": true,
    "style_id": "bold_pop_highlight",
    "language": "en",
    "max_words_per_line": 3,
    "highlight": "active_word",
    "placement": "platform_safe_zone",
    "translations": []
  },
  "brand": {"brand_kit_id": null, "logo_overlay": false},
  "render": {
    "outputs": [{"preset_id": "tiktok_1080x1920_30", "aspect": "9:16"}],
    "reframe": {"strategy": "subject_aware", "regenerate_if_crop_loss_above": 0.25}
  },
  "provenance": {"visible_label": "auto", "consent_ids": []},
  "assets": [],
  "effects": [],
  "generation": {"seed_namespace": "0192f0a0-0000-7000-8000-000000000001", "seed_overrides": {}, "engine_hints": {}},
  "locks": [
    {"group": "voice", "scope": {"character_keys": ["char_alex"]}, "set_by": "user"}
  ]
}
```

In this example, `ev_1` (a look-away inside a talking shot) is not controllable by a global-prompt engine. At planning time the compiler proposed covering it with a B-roll cutaway (`sht_2`, `derived_from` the event, with the planned route's digest). The coverage report lists `ev_1` as `APPROXIMATED (editorial_cutaway)`. If a later re-route selects an engine with gaze control, the system proposes removing `sht_2` through an EditOperation (§15.7). See §16 for how the observation is reported.

Also define these types in `ce_core`:
- anchors: `WordRef`, `WordSpan`, `SegmentRef`, `ShotRef`, `DurationSpan`, `SceneSpan`, `SpecPath`;
- spec parts: `AssetRef`, `Effect`, `CreativeBrief`, `ResearchDossier`, `Claim`;
- intent: `VideoIntent`, `SceneIntent`;
- acting: `ActingPlan`, `Situation`, `ActingState`, `EmotionSpec`, `BehaviorEvent`;
- world: `WorldBinding`, `WorldOverrides`;
- identity: `CreatorDNA`, `AppearanceDNA`, `VoiceDNA`, `WardrobeSpec`, `WorldDNA`, `MemoryItem`, `MemorySnapshot`;
- behavior: `CanonicalBehaviorSpec` (envelope + content), `CompiledBehavior`, `KeyframeState`, `ObservedBehavior`, `BehaviorCoverageReport`, `PlanReport`;
- editing and build: `EditOperation` (closed union, §28), `ExecutionGraph`, `ExecutionNode`, `BuildManifest`, `RouteDecision`;
- QC: `QCReport`, `ConsistencyReport`, `Critique`.

---

## 12. Build graph, caching, seeds, routes, locks, versions

Implement in `ce_build`.

### 12.1 Nodes

`build_graph(spec, build_manifest_of_parent, artifact_index, routing_profile) -> ExecutionGraph`.

The **node key** is `{kind}:{element_key}[:c{chunk}][:t{take}]`, for example `avatar.render:sht_1:c2:t1`. Node kinds:

| Kind | Per | Runs on | Notes |
| --- | --- | --- | --- |
| `behavior.resolve` | scene | CPU | Builds the CBS (§15) |
| `behavior.compile_voice` | segment | CPU | CBS prosody → abstract ProsodyPlan for the routed voice engine |
| `voice.prepare` | voice version × TTS adapter | GPU/CPU | Per-engine conditioning (`voice.clone_prepare`), cached as `voice_conditioning` artifacts |
| `tts.segment` | segment (per character) | GPU/CPU | |
| `asr.verify` | segment | GPU/CPU | Exact-script verification |
| `align.segment` | segment | GPU/CPU | Word timings |
| `behavior.compile_visual` | shot (per chunk) | CPU | CBS + alignment + avatar route → visual directives |
| `world.plate` | world version × camera position × time of day × weather × overrides digest | GPU | Reuses an approved canonical plate when one matches exactly. Otherwise it generates a variation conditioned on the nearest canonical plate and on the `continuity_ref` |
| `behavior.keyframe_state` | talking shot | CPU | From the CBS and the scene cast (no timing): the displayed expression, posture, placement and attention target at the shot's start |
| `image.keyframe` | talking shot | GPU | Appearance × wardrobe × world plate × framing × keyframe state (the `keyframe_conditioning` method, §15.7) |
| `avatar.render` | talking shot chunk × take | GPU | Chunks of about 20–30 s, boundaries placed at pauses or state transitions |
| `lipsync.patch` | shot | GPU | Calls `lipsync.dub` |
| `post.expression` | shot | GPU | V2. In the MVP this is a pass-through mock that labels its output |
| `video.broll` | B-roll shot × take | GPU | |
| `video.upscale`, `video.interpolate` | shot | GPU | Mandatory in `final` when the generation resolution is below the preset (§22) |
| `screen.prepare` | screen shot | CPU | |
| `behavior.observe` | take | CPU (+ optional GPU VLM) | **Measurement only**: tracks and signatures, keyed by the take artifact. Its judgement (verdicts against the CBS requests) runs in `qc.shot` and `behavior.coverage` (§16.3) |
| `qc.shot` | take | CPU/GPU | Includes the per-take behavior judgement; keyed by the tracks + the CBS content digest |
| `qc.world` | shot | CPU/GPU | §19 |
| `post.camera`, `post.realism` | shot | CPU | Deterministic |
| `audio.music` | music cue | GPU | |
| `audio.sfx` | SFX event | GPU | |
| `audio.room` | scene | CPU | Per-scene acoustics on dialogue |
| `captions.build` | caption track | CPU | |
| `mix.audio` | video (per loudness target) | CPU | |
| `render.final` | output preset | CPU | |
| `render.proxy` | video | CPU | |
| `provenance.watermark_video`, `provenance.watermark_audio`, `provenance.sign` | render | CPU/GPU | |
| `qc.render` | render | CPU | |
| `captions.translate` | caption language | CPU (LLM) | One node per language in `captions.translations[]`; review state lives in `captions.review_state` |
| `behavior.coverage` | version | CPU | After `render.final`: maps take observations through the EDL, measures `approximation_executed`, and writes the `coverage_report` artifact (§16.4). Keyed by the EDL, the tracks and the CBS content digests |

Each node kind declares its **spec dependencies** as SpecPath patterns, together with the DNA and world fields it reads. For example, `image.keyframe` reads the appearance version, the wardrobe version, the world plate artifact, the shot framing and the keyframe state artifact. It does not read persona DNA. Dirty analysis uses these declarations.

Creator consistency (§20) is **not** a build node. It runs as `ConsistencyWorkflow` after `ready`, because its inputs (baselines, other videos) change over time.

### 12.2 Cache keys

The cache key is SHA-256 over canonical JSON of:
- the node kind and the node kind's `impl_version` (bump it whenever deterministic code changes output);
- the digest of the declared spec fragment;
- the digests of the referenced DNA, world and memory fields, restricted to the fields the node reads;
- upstream artifact hashes;
- the route (adapter id, model id, revision, translator version);
- parameters;
- the **effective seed** and the take index;
- the `config_digest` of every config file the node reads (camera profile YAML, LUT file, vocab version, intent policies version, QC thresholds where relevant).

Canonicalize before hashing: sorted keys and normalized floats. `cache_entries` maps `(org_id, cache_key)` to the accepted artifact, and storage deduplicates by content `sha256`.

**Digests are content-only.**
- The **spec content digest** hashes the spec without `video_id`, `version_id` and `parent_version_id`.
- The **CBS content digest** hashes the CBS `content` without its `envelope` (§15.6).
- The CBS content digest appears in the cache keys of the nodes that **read requests**: `behavior.compile_*`, `behavior.keyframe_state`, `qc.shot` (behavior judgement) and `behavior.coverage`. Generation nodes depend on the **hash of the compiled output artifact** they consume, so when an edit leaves a compiled output unchanged, all generation stays cached, and only the cheap judgement is recomputed.
- `behavior.resolve` caches the CBS **content** only (artifact kind `cbs`). The envelope is assembled per version on read, from the spec and the manifest.
- No cache key ever contains a `version_id`. A `continuity_ref` of kind `shot` enters keys as the referenced artifact's hash, never as ids.

**Artifact kinds** (closed): `video`, `audio`, `image`, `captions`, `alignment`, `cbs`, `compiled_behavior`, `keyframe_state`, `observed_behavior`, `coverage_report`, `plan_report`, `screen_analysis`, `world_fingerprints`, `voice_conditioning`, `logs`, `other`. Memory snapshots are rows in `memory_snapshots`, not artifacts.

An artifact that failed QC is marked `qc_rejected` and is never served as a cache hit. When a QC retry succeeds, the cache entry for that key points to the accepted artifact.

### 12.3 BuildManifest (per version)

The BuildManifest records:
- `routes {node_key: RouteDecision}`;
- `effective_seeds {node_key: seed}`;
- `artifact_map {node_key: artifact_id}`;
- `config_digests`;
- `impl_versions`.

Memory snapshots are pinned in the spec (`memory.snapshots`), not in the manifest.

The BuildManifest is stored as insert-only rows in `build_manifest_entries` (§29) and assembled on read. A version's identity columns never change.
- While nodes complete, entries are added. Existing entries are never replaced.
- A version in `partial`, `failed` or `cancelled` can be resumed in place (§30 `:resume`). Resuming only adds entries for nodes that have none yet.
- The manifest is frozen (`frozen_at`) when the version reaches `ready` or `needs_review`. After that, any change produces a new version.
- Render presets requested after freezing are `renders` rows cached by key outside the manifest (§9 `RenderWorkflow`).

**Plan-time routes** (Director stage 11) are stored in `video_versions.planned_routes`, written once when the version is created. They are the initial pins for build-time routing. The BuildManifest records the routes actually used.

A rebuild from an empty cache uses the BuildManifest to reproduce the version exactly, on the same routes with the same effective seeds.

### 12.4 Routes are pinned

When a version is derived from a parent, every node whose cache-key inputs (other than the route) are unchanged reuses the parent's route. Dirty nodes are re-routed, except that **locked groups also pin routes**: a voice lock keeps the TTS engine.

A pinned route is replaced only in three cases:
1. on an explicit `reroute` operation;
2. when the pinned adapter is disabled, deleted or now denied by license policy;
3. when the pinned adapter fails in a way the QC ladder escalates to a fallback.

In every case the impact preview shows the forced re-render and why.

### 12.5 Seeds

- **Default seed** = hash(`generation.seed_namespace`, node_key, take_index). The namespace is the original `video_id` and is copied on duplicate. Never derive seeds from `version_id`; that would invalidate every cache key on every edit.
- **Spec seed override**: `generation.seed_overrides[node_key]`, written only by an explicit user `regenerate` with `seed_policy: new`. That operation creates a new version.
- **Attempt seed**: an infrastructure retry uses the same seed. A QC retry uses `hash(base_seed, retry_n)`.
- **Effective seed**: the seed of the accepted artifact. It is recorded in the BuildManifest and in `job_attempts.seed`.
- Variation between consecutive shots (anti-repetition, §15.8) comes from distinct node keys and compiled directives, not from mutating seeds.

### 12.6 Takes and chunks

`avatar.render` and `video.broll` support `takes.count`. QC and behavior observation rank the takes. Each take has a stable `tk_` key and a row in `takes`. The user can override the choice through `select_take`.

A chunk continues from the previous chunk's last frames, so **editing or retrying chunk k invalidates chunks k+1…n**. Dirty analysis and the impact preview show this cascade.

### 12.7 Locks

`config/vocab/edit_vocabulary.yaml` is the single vocabulary. It defines:
- **lock groups**: `script`, `voice`, `appearance`, `wardrobe`, `world`, `acting`, `intent`, `camera`, `music`, `sfx`, `broll`, `captions`, `product`;
- each group's **SpecPath patterns**;
- whether each group **pins routes**;
- the **regenerate components**: `voice`, `keyframe`, `avatar_video`, `background`, `world_plate`, `broll`, `camera_post`, `acting`, `music`, `sfx`, `captions`, `lipsync`;
- each component's node kinds, the spec paths it may change, and the lock groups that block it.

Rules:
- **Locks pin spec values, not artifacts.** A patch that modifies a locked path in scope is rejected with the lock group named.
- A node may still re-render when a non-locked input changes. For example, with appearance, voice and wardrobe locked, a world change re-renders the talking shot with the same appearance version, the same voice audio artifact (and the same TTS route) and the same wardrobe.
- A regenerate request is refused, with an explanation, when every input of the targeted nodes is locked.
- **The voice lock also pins delivered audio.** For a locked character, `behavior.compile_voice` reuses the parent version's compiled ProsodyPlan for every segment whose text is unchanged. Changed text is still synthesized, with the same voice version and TTS route. Acting edits that would change vocal delivery are reported as `blocked_by_lock: voice` in the coverage delta.
- A `regenerate` of `acting` needs the Director. It runs as an `edit_propose` job in replan mode and is auto-applied after validation. While `voice` is locked, the coverage report states that vocal expression could not change, so the change is visual and editorial only.

### 12.8 Versions

A version row has **immutable identity columns** (`spec`, `spec_hash`, `spec_content_digest`, `parent_version_id`, `origin`, `planned_routes`) and **mutable status columns** (`state`, `flags`, `plan_report_artifact_id`, `coverage_summary`, `cost_actual_usd`, `frozen_at`). Its manifest lives in `build_manifest_entries`.

The version state machine is:
`planning → planned → previz_running → previz_ready → approved → generating → ready | partial | needs_review | failed | cancelled`. A `partial`, `failed` or `cancelled` version can go back to `generating` through `:resume`.

Operations:
- create from plan;
- apply edit;
- regenerate (a new version with a new seed override or route for the chosen nodes);
- branch (a copy under a new branch name);
- restore (copy an older spec and BuildManifest forward as a new current version);
- compare (structured spec diff, CBS diff, coverage diff and both artifact maps);
- duplicate (copy a version into a new video).

**Starting states:**
- `plan` and `:replan` start in `planning` and pass the previz gate.
- Derived versions (`edit`, `regenerate`, `reroute`, `lock_change`, `take_select`, `restore`, `branch`, `duplicate`) of a parent that passed approval start in `approved` and move straight to `generating`. They reach `ready` immediately when every node is cached.
- A derived version of a parent that never passed approval (an edit during previz review) starts in `planned`. `PrevizWorkflow` re-runs for its dirty previz nodes, and the version lands in `previz_ready`. Approval always applies to an up-to-date previz.

Version numbers are a single per-video sequence across branches. `videos.current_version_id` points to the version the user is working on. Never mutate or delete a version.

### 12.9 Dirty analysis and impact

`diff_graph(old_version, new_spec) -> Impact{regenerate: [node_key], keep: [node_key], cascade: [node_key], no_visible_effect: [item_ref], route_changes: [...], locks_blocking: [...], estimate}`. It runs without executing anything.

`behavior.resolve` and both compile nodes are cheap CPU nodes, so `diff_graph` evaluates them directly. When a behavior change leaves the compiled output digest unchanged (for example, a gaze request that is `UNSUPPORTED` before and after), downstream nodes are **kept**. The change is listed in `no_visible_effect` with the reason, so the user learns that the current engine cannot show it instead of paying for a useless re-render.

### 12.10 Garbage collection

An `artifact_refs(artifact_id, ref_type, ref_id)` table is maintained transactionally. Its references come from BuildManifests, takes, renders, identity packs, world plates, voice conditioning, Creator Tests and memory snapshots. GC removes only unreferenced artifacts older than the retention period.

---

## 13. AI Director

Implement in `ce_director`. The Director is a pipeline of typed stages. Each stage takes a Pydantic input, then either makes an LLM call with JSON-schema structured output or runs pure code. Its output is validated with Pydantic and goes through a targeted repair loop (max N attempts). Each stage persists a `director_runs` record with the template version, provider, model, input, output, tokens, latency and cost.

Two rules apply to every stage:
- **LLM output is never trusted for control values.** It is validated against the closed vocabularies (I13). Unknown labels go to the repair loop. If a label is still unknown after repair, it is mapped to the nearest vocabulary item and recorded in `assumptions`. The mapping uses alias and lexical matching against `descriptions.yaml`; embedding similarity is added in Phase 12.
- Memory text, sources and uploads are inserted into prompts inside data delimiters (I10).

| # | Stage | Output | Key requirements |
| --- | --- | --- | --- |
| 1 | Interpret | `CreativeBrief` | Classify the input mode. For exact scripts, the LLM returns character offsets and code slices the text (it is never retyped by the LLM); code then verifies that the slices reassemble the original apart from converted tags. Convert brain dumps into explicit constraints. Ask nothing: record assumptions in `brief.assumptions`. |
| 2 | Context | `ContextPack` | Mostly code. Pins one **MemorySnapshot per cast member** (§18). Loads Creator DNA summaries, candidate worlds and wardrobes, project history, brand rules and the recent usage log. Never sends the whole memory to the LLM: the snapshot is already budgeted per category. |
| 3 | Research (optional) | `ResearchDossier` | Retrieval over project sources (pgvector); closed-book mode when `sources_policy = closed_book`. Source text is data. URL fetching goes through the SSRF guard (§33). |
| 4 | Strategy and video intent | audience, angle, 3–5 hooks, structure, `intent.video` | Uses a strategy pack (YAML/MD, configurable). The repetition guard (§18.6) rejects hooks too similar to the creator's recent hooks. Intent values come from `config/vocab/intent.yaml`. |
| 5 | Script | segments + annotations | **AI mode**: written for speech in the creator's voice. Uses Speech DNA and memory (vocabulary level, signature phrases at their allowed frequency, transitions, humor style), never phrases on the avoid list. Fillers and false starts are added only when wording is unlocked, marked `inserted_disfluency`. Target words = duration × calibrated WPM. **Exact mode**: the LLM proposes segment boundaries as character offsets plus annotations, and code builds the segments. |
| 6 | Fact and persona check | claims with verdicts; persona consistency report | Claim verdicts are supported / unsupported / uncertain / conflicting, with evidence ids. Unsupported claims block approval unless overridden (logged). The **contradiction checker** (§18.7) compares persona statements in the script ("I live in Berlin", stances) with the creator canon and active persona memories. A contradiction of a pinned item blocks approval unless the user overrides it. |
| 7 | Scenes, worlds and shots | scenes with `intent`, `world` binding, `cast` (wardrobe, placement), shots | Uses a mode template (`config/modes/*.yaml`) that defines allowed shot types, cadence rules, default camera profiles, default world kinds and acting-density guidance. Scene intent comes from the strategy and the intent vocabulary. World binding prefers the creator's default worlds and varies camera position or time of day to avoid visual repetition (§18.6). If no approved world fits, the stage emits a world proposal (§19.2). |
| 8 | Situational acting | `scenes[].acting` (situation, states, events) | Writes the full acting chain (§15), bounded by Creator DNA ranges and active memory habits. Avoids repeating recent emotional sequences. Records every requested behavior. Consults a **route preview** (the candidates that pass capability, language and routing-profile filters, recorded in `director_runs`) and their declared and measured matrices only to choose between equally valid alternatives (for example, prefer `seated_lean_in` over pacing when seated). It never deletes intent because an engine is weak. |
| 9 | Camera and edit | camera profiles, moves, cut cadence, overlay shots | The intent policy engine (§14) proposes defaults, and the edit grammar comes from the mode template (max shot length, cutaway cadence, punch-ins on emphasis). |
| 10 | Sound | music cues, SFX, acoustics | Proposed by intent policies. Music is instrumental under speech. Acoustics come from the bound worlds. |
| 11 | Validate, resolve and repair | final `VideoSpec` + `plan_report` | (a) Schema, tiling, duration fit, references, policy (blocklists, testimonial guard) and a license policy pre-check. (b) `behavior.resolve` → CBS per scene. (c) Plan-time routing using the CBS's requested dimensions, recorded as the draft BuildManifest routes. (d) Plan-time compile: editorial approximations and shot splits proposed by the compiler are written into the spec with `derived_from: compiler_approximation` and the route digest, so nothing hidden happens at render time. (e) Predicted coverage. (f) The `plan_report` artifact: repetition, contradiction, timing drift and predicted coverage. |
| 12 | Packaging (after render or on export) | per-platform title, description, hashtags, CTA text, thumbnail candidates | Uses the platform YAML limits. Thumbnail candidates are the best keyframes with a text overlay rendered by the render worker. The user edits them before export. Workflow `PackagingWorkflow`. |

**Mode templates to ship.** Each YAML has a design `maturity`; the effective maturity is capped by route validation (§10.4). Adding a mode means adding a YAML file (plus an optional prompt fragment), with no core change.
- **Production**: `talking_head_explainer`, `educational`, `ugc_selfie`, `product_review_cutaways`, `product_demo_cutaways`, `tutorial_screen_voiceover`, `tutorial_screen_webcam`, `storytelling`, `news_presenter`, `commentary`, `social_ad`, `testimonial_dramatization`.
- **Beta**:
  - `reaction`: the creator speaks reactions over a user-supplied clip shown picture-in-picture. Silent reactions are approximated editorially and reported as such.
  - `cinematic_storytelling` and `meme`.
- **Experimental stubs** (V1/V2): `vlog`, `walking_vlog`, `car_vlog`, `podcast`, `interview`, `street_interview`.

**Strategy packs to ship**: `hook_problem_payoff_cta`, `myth_vs_reality`, `listicle`, `story_arc`, `contrarian_take`, `tutorial_steps`. They are configurable and never hard-coded.

**Prompt templates** live in `prompts/<stage>/<version>.md` with Jinja2 variables. Each run records its template version.

**Previz.** After planning, `PrevizWorkflow` runs:
- TTS, verification and alignment for all segments;
- the world plates the scenes need (reusing approved canonical plates);
- one keyframe per talking shot.

It then computes measured durations, estimated times for each acting state, the predicted coverage per CBS item, the memory items used, the repetition and contradiction reports, the fact-check flags and the cost estimate. These are stored in the `plan_report` and served by `GET /v1/versions/{version_id}/previz`. Finally it sets the version state to `previz_ready`.

Video generation starts only after explicit approval (`POST /v1/versions/{version_id}:approve`).

**Creative Director critique** (§26) reuses the operation vocabulary of §28.

---

## 14. Director Intent layer

Intent describes **what** a video or scene is trying to achieve. It never says how a model should do it.

**`intent.video`**: `narrative_goal`, `audience_effect`, `persuasion_goal`, `information_goal`, `emotional_arc` (an ordered list of emotion labels), `attention_goal`, `cta_goal`.

**`scenes[].intent`**: `narrative_goal`, `emotional_goal`, `audience_effect`, `persuasion_goal`, `information_goal`, `attention_goal`, `reveal_strategy`, `tension_level` (0–1), `curiosity_level` (0–1), `performance_strategy`, `notes` (free text, non-controlling).

```json
{
  "narrative_goal": "challenge_common_belief",
  "audience_effect": "curiosity",
  "emotional_goal": "create_doubt_then_reveal",
  "performance_strategy": "start_confident_then_lower_energy",
  "attention_goal": "delay_payoff"
}
```

All control fields come from `config/vocab/intent.yaml`. Each list is closed and versioned, and the shipped minimum is:

| Field | Values |
| --- | --- |
| `narrative_goal` | `challenge_common_belief`, `establish_credibility`, `explain_mechanism`, `tell_anecdote`, `show_example`, `contrast_options`, `reveal_payoff`, `call_to_action` |
| `emotional_goal` | `create_doubt_then_reveal`, `build_excitement`, `reassure`, `provoke`, `amuse`, `inspire`, `sober_reflection` |
| `audience_effect` | `curiosity`, `doubt`, `trust`, `amusement`, `urgency`, `surprise`, `insight`, `reassurance` |
| `persuasion_goal` | `none`, `reframe_mental_model`, `build_trust`, `create_urgency`, `overcome_objection`, `social_proof` |
| `information_goal` | `none`, `state_misconception`, `define_concept`, `explain_steps`, `give_example`, `compare`, `summarize` |
| `attention_goal` (scene and video) | `pattern_interrupt`, `sustain`, `delay_payoff`, `build_suspense`, `re_engage`, `release` |
| `reveal_strategy` | `immediate_answer`, `tease_then_reveal`, `open_loop`, `slow_build`, `callback` |
| `performance_strategy` (named trajectory templates) | `start_confident_then_lower_energy`, `build_from_calm_to_urgent`, `deadpan_then_crack`, `warm_confessional`, `skeptic_converted` |
| `cta_goal` (video) | `none`, `follow`, `follow_for_series`, `comment`, `save`, `share`, `click_link` |

`intent.video.emotional_arc` is an ordered list of labels from `emotions.yaml`. The validator checks that it summarizes the trajectory of the scene acting states.

**Intent policy engine** (`ce_director.intent_policy`, `config/intent_policies.yaml`). This is a deterministic rule engine. Each rule matches on intent values, the mode, Creator DNA traits and position in the video. It proposes defaults for every channel:

| Channel | Example proposal for `reveal_strategy = tease_then_reveal` |
| --- | --- |
| voice | pause of 300–500 ms before the reveal word; emphasis on the reveal word |
| acting | the trajectory template from `performance_strategy`; a transition trigger at the reveal |
| camera | punch-in on the reveal word |
| B-roll | no overlay during the reveal; overlay allowed during the tease |
| editing | hold the shot through the reveal; no cut within ±2 words |
| captions | hide the reveal keyword until it is spoken; emphasis style on it |
| music | duck or drop out 1 beat before the reveal; resolve after it |
| SFX | optional riser before, impact after (mode permitting) |

Director stages 7–10 start from these proposals. The LLM may change them only within the vocabulary and must give a recorded reason. Every adopted decision carries `derived_from: [{kind: "intent", ref: SpecPath}]`, so the Studio can answer "why does the camera punch in here?".

**WHAT vs HOW.** Intent and acting describe the goal and the performance. The compiler and the adapters decide execution. Intent never contains engine parameters (I1).

**Editing intent.** `set_intent` changes scene intent. `ProposeEditWorkflow` re-runs the intent policies and the dependent Director stages for that scene only, in replan mode. The proposal shows every channel change the new intent implies, and the user can accept them selectively.

**Tests:**
- intent schema and vocabulary validation;
- intent policy unit tests (rule matching, precedence, determinism);
- traceability (every policy-derived field carries `derived_from`);
- intent-only edit impact (dirty set limited to the scene's affected channels).

---

## 15. Situational acting and the Human Behavior Engine

Implement in `ce_behavior` (models in `ce_core`).

### 15.1 The acting chain

Acting is never a single emotion label. The canonical representation is the chain below:

| Link | Where it lives |
| --- | --- |
| SITUATION | `acting.situation {kind, description, audience_stance, stimulus}` |
| INTERNAL STATE | `states[].internal_state {label, description}` |
| INTENT | `states[].social_goal`, `audience_goal`, `performance_intent` (plus scene intent, §14) |
| EMOTION | `states[].emotion {felt, displayed, masking}` + `confidence_delta`, i.e. the emotional trajectory over states |
| PROSODY | `states[].strategies.prosody` (`assertive`, `assertive_light`, `slow_measured`, `hesitant_with_pauses`, `controlled_even`, `playful_rising`, `urgent_fast`, `deadpan`, `whispered_aside`) + `pause`/`emphasis` annotations → CBS `prosody_directives` |
| FACIAL BEHAVIOR | displayed emotion + `events` of facial types (`small_smile`, `eyebrow_raise`, `frown`, …) |
| GAZE | `states[].attention_target` + `strategies.gaze` (`hold_camera`, `glance_away_and_return`, `look_down_thinking`, `side_glance`, `avoid_camera`) + gaze `events` (`look_away`, `glance_at_element`) |
| GESTURE | `strategies.gesture` (`still`, `illustrative_light`, `illustrative_strong`, `emphatic_beats`, `self_soothing`) + gesture `events` (`open_palms`, `count_on_fingers`, `shrug`, `point_offscreen`, …) |
| POSTURE | `strategies.posture` (posture vocabulary: `seated_upright`, `seated_relaxed`, `seated_lean_in`, `seated_withdrawn`, `standing_upright`, `standing_relaxed`, …) + `scene.cast[].default_posture`, validated against the world zone's `allowed_postures` |
| CAMERA AWARENESS | `strategies.camera_awareness` (`direct_address`, `aside_to_camera`, `ignores_camera`, `breaks_fourth_wall`) |
| REACTION | `strategies.reaction` (`none`, `suppressed`, `open`, `slow_burn`, `exaggerated`) + reaction `events` (`small_laugh`, `double_take`, `sigh`) with optional `trigger_ref` |
| EDITORIAL RESPONSE | proposed by intent policies and the compiler: cuts, punch-ins, cutaways, holds, caption emphasis |

### 15.2 Closed vocabularies (`config/vocab/`)

- `emotions.yaml`: each label carries a family, valence, arousal and dominance coordinates, compatible strategies, incompatible labels, a description and observation-proxy keys. The shipped labels include at least: `neutral`, `confident`, `amused`, `curious`, `excited`, `serious`, `skeptical`, `uncertain`, `hesitant`, `confused`, `surprised`, `embarrassed`, `frustrated`, `angry`, `sad`, `calm`, `warm`, `sarcastic`, `determined`, `relieved`, `concerned`, `playful`. Compound states ("curious and energetic") are a primary label plus intensity and arousal, never new free-form labels.
- `acting.yaml`: situation kinds, internal-state labels, social goals, audience goals, performance intents, the strategy list for each channel, the posture vocabulary, attention-target kinds and transition styles (`sudden`, `gradual`, `lagged`).
- `behavior_events.yaml`: event types, each with its dimension, default duration and allowed parameters.
- `behavior_dimensions.yaml`: the **single canonical dimension list**, used everywhere: event types, adapter behavior matrices, CBS `requested_controls`, observation proxies, `behavior_observations`, `model_behavior_profiles` and metrics. It has two groups:
  - **Requestable dimensions**: `emotion_visual`, `emotion_vocal`, `facial_expression`, `gaze`, `blink`, `head_motion`, `gesture`, `posture`, `camera_awareness`, `reaction`, `listening`, `nonverbal_audio`, `delivery` (whisper, aside), `prosody_rate`, `prosody_pitch`, `prosody_energy`, `prosody_emphasis`, `prosody_pause`, `accent`.
  - **Engine-property dimensions**: `segment_control`, `prosody_coupling`, `continuity`, `object_interaction`, `multi_person`, `temporal_control`.
  - It also records each dimension's method preference order (§15.7).
- `descriptions.yaml`: model-agnostic natural-language descriptions of every vocabulary item, with aliases. Text-prompted translators and the LLM use them.
- **Lint rules**: strategy tokens and event tokens are disjoint, every token has a description, and every event type maps to exactly one requestable dimension.
- Every file carries `vocab_version`. A spec, CBS or memory item records the version it was validated against. Vocabulary changes are additive within a major version, and a migration map handles renames.

### 15.3 Acting plan schema (in the VideoSpec)

- **`situation`**: `kind` (vocab), `description`, `audience_stance` (vocab), `stimulus` (optional: `{kind: element|clip|statement|memory, ref}`).
- **`states[]`** form an ordered list per character that tiles the scene's speech span. Silent spans are covered by `DurationSpan` states. A state may set `carry: true` to inherit the previous state. Each state has:
  - `key`, `character_key`, `source` (`director | user_tag | user_edit | intent_policy`), `span`, `internal_state`, `social_goal`, `audience_goal`, `performance_intent`;
  - `emotion {felt {label, intensity}, displayed {label, intensity}, masking}`. `masking: true` represents "pretending to stay calm while surprised": felt `surprised`, displayed `calm`;
  - `confidence_delta` (−1…1, accumulated into an absolute confidence in the CBS);
  - `attention_target`: `camera`, a world element key, `off_camera_person`, `notes` or `self`;
  - `strategies {prosody, gaze, gesture, posture, reaction, camera_awareness}`;
  - `transition_in {trigger {kind, at: WordRef, description}, style, duration_words}`;
  - `priority` (`must | should | nice`);
  - `out_of_character` (optional, with a reason).
- **`events[]`**: `key`, `character_key`, `type` (vocab; this determines the dimension), `at: WordRef` or `span`, `duration_ms`, `direction`, `target`, `intensity`, `purpose`, `trigger_ref`, `priority` and `source` (`director | user_tag | user_edit | compiler_approximation`).
- **Validation**:
  - states tile the scene;
  - intensity jumps above a configured threshold need a `transition_in` with `style: sudden` or a trigger;
  - masking requires felt ≠ displayed;
  - events must sit inside their scene;
  - no same-dimension duplicate of an event and an annotation on the same span (§10.6);
  - labels exist in the declared `vocab_version`;
  - values fall within Creator DNA ranges unless `out_of_character` is set;
  - postures are in the bound zone's `allowed_postures`.
- **Annotation and element sources** use the same vocabulary as states (`director | user_tag | user_edit | intent_policy | compiler_approximation`).

**Mapping from the product brief's field names** to the schema:

| Brief | Schema |
| --- | --- |
| `situation` | `acting.situation` |
| `internal_state` | `states[].internal_state` |
| `social_goal`, `audience_goal`, `performance_intent` | the same names on `states[]` |
| `emotional_state`, `emotional_intensity` | `states[].emotion.displayed.label` / `.intensity`, plus `felt` and `masking` |
| `confidence_delta`, `attention_target` | the same names on `states[]` |
| `gaze_strategy`, `gesture_strategy`, `posture_strategy`, `prosody_strategy`, `reaction_strategy` | `states[].strategies.{gaze, gesture, posture, prosody, reaction}` |
| `camera_awareness` | `states[].strategies.camera_awareness` |
| `transition_trigger` | `states[].transition_in.trigger` |

### 15.4 Emotional trajectories

The ordered states of all scenes form the video's emotional trajectory. `intent.video.emotional_arc` is its summary and is validated against it. The Performance Timeline (§31) displays the states as bands in seconds: estimated in previz, measured after alignment.

**Example request:** "0–3 s confident; 3–6 s realization; 6–8 s uncertainty; 8–12 s curiosity; 12–17 s confidence; 17–22 s humorous reaction; 22–30 s serious conclusion."

The Director represents it as seven states. Each has a word span chosen so that its estimated start time is closest to the requested second, and the previz shows the drift from the requested times. The transitions into "realization" and "humorous reaction" carry triggers. The 17–22 s state uses `strategies.reaction: open` with a `small_laugh` event, plus a `nonverbal_audio` annotation so that the voice actually laughs. Time requests in seconds are honored approximately, because time is word-anchored (ADR 0004). The drift is reported, never hidden.

### 15.5 Situational examples (acceptance fixtures, §37)

| Request | Representation (abbreviated) |
| --- | --- |
| "He realizes the viewer may disagree with him." | situation `contradicting_the_audience`; transition trigger `realization` at the word where he notices; internal state `doubt_about_reception` → `determined`; gaze strategy `glance_away_and_return` at the trigger; prosody `slow_measured` after it; editorial: a punch-in at the trigger |
| "She remembers something embarrassing." | stimulus `memory`; internal state `embarrassed_recall`; felt `embarrassed`, displayed `amused` (masking); gaze strategy `look_down_thinking`; reaction strategy `suppressed` + a `small_smile` event; prosody `hesitant_with_pauses` + a `pause` annotation; posture `seated_withdrawn` |
| "He is trying to convince a skeptical audience." | situation `persuading_skeptic`; audience stance `skeptical`; social goal `win_trust`; performance intent `build_case`; confidence rising across states through `confidence_delta`; gesture strategy `illustrative_strong` with `open_palms` and `count_on_fingers` events; camera awareness `direct_address`; scene intent `persuasion_goal: reframe_mental_model` |
| "She becomes confident after realizing she is correct." | two states: `uncertain` → `confident`; `transition_in.trigger.kind = realization`, style `sudden`, `confidence_delta = +0.4`; posture `seated_upright` → `seated_lean_in`; prosody `hesitant_with_pauses` → `assertive` |
| "He is pretending to stay calm while being surprised." | felt `surprised` (0.7), displayed `calm` (0.5), `masking: true`; reaction strategy `suppressed`; event `eyebrow_raise` at low intensity; prosody `controlled_even`; a brief `double_take` event as the only leak |
| "starts excited, becomes skeptical, pauses, looks away, laughs slightly, then becomes serious before the CTA" | states `excited` → `skeptical` → `serious`, with a `pause` annotation, a `look_away` event, and a `small_laugh` event + `nonverbal_audio` annotation; the CTA scene intent is `call_to_action` |

### 15.6 CanonicalBehaviorSpec (CBS)

The CBS is the model-independent, fully resolved behavior document for one scene. It sits between the Director and every engine. The node `behavior.resolve` produces it deterministically from:
- the scene's acting plan and intent;
- each cast member's Creator DNA (only the behavior-relevant fields);
- each cast member's pinned MemorySnapshot (habits and avoidances);
- the bound world's **behavior digest** (element keys, kinds, attention targets, seating and allowed postures, hand space; lighting and acoustics are excluded so that they never invalidate behavior);
- the script annotations and the lock state.

It is versioned (`cbs_version`), validated by a Pydantic model and its JSON Schema, stored as a content-addressed artifact (kind `cbs`), and included in the build graph.

**Envelope and content.** A CBS has two parts:
- The **`envelope`** holds the schema and vocabulary versions, the scope (video, version, scene) and the identity references (creator, appearance, voice, memory snapshot and world-binding ids). It is for traceability only.
- The **`content`** holds everything that affects behavior.

The **CBS content digest** (envelope field `content_digest`) hashes only the content. It enters the cache keys of the request-reading nodes listed in §12.2, and generation nodes key on the compiled outputs. The content digest is therefore identical across versions when behavior did not change, and identical across engine swaps (I1). Only the content is cached as an artifact; the envelope is assembled per version on read.

**Content:**
- `cast[]`: per character, `dna_behavior_digest` and the resolved `behavior_profile` (baseline, emotion ranges, habits with memory item ids and confidence);
- `world`: `behavior_digest` and `affordances`;
- `intent`: the full scene intent;
- `situation`;
- `trajectory[]`: resolved states per character, with absolute confidence, clamped intensities, `strategies` for every channel and `transition_in`;
- `events[]`: every gaze, gesture, facial, head, posture and reaction event, each with `character_key` and `dimension`;
- `prosody_directives[]`: per character and segment, a **ProsodyDirective** (strategy, rate, energy, pitch variation, emphasis words, pauses, non-verbal sounds). The compiler later turns these into an engine-ready **ProsodyPlan**;
- `continuity.requirements`: **behavioral continuity only**: posture carried from the previous scene, and interactions with world elements that are attention targets or affordances. Visual continuity (wardrobe, element states, lighting) is checked by shot and world QC from the spec, so it never invalidates behavior;
- `constraints`: active locks, avoidances (the union of DNA and memory, §10.6), permitted out-of-character items, intensity caps;
- `requested_controls[]`: a flat list with **one entry per requested behavior item and channel**. Every state emotion (visual and vocal), every state strategy (gaze, gesture, posture, prosody, reaction, camera awareness), every event and every prosody annotation gets an entry `{item_ref, character_key, dimension, value, temporal_precision, priority, planner_confidence, observation_reliability}`. Dimensions come from `behavior_dimensions.yaml`. This list is what coverage and observation evaluate;
- `provenance[]`: for each item, the `source` (director, user tag, user edit, intent policy, DNA default, memory habit, compiler approximation) and `supported_by` references.

**Model independence.** The CBS carries only model-independent confidence metadata: planner confidence, priority, and the observation reliability class from `observation_proxies.yaml`. **Engine coverage lives in the CompiledBehavior**, which references CBS items, and is never written back into the CBS.

**Multi-person ready.** The cast is a list, and every state, event and directive carries `character_key`. A future multi-person engine therefore needs no schema change.

**Example** (abbreviated, valid JSON; one character, one scene):

```json
{
  "envelope": {
    "cbs_version": "1.0",
    "vocab_version": "2026.10.1",
    "scope": {"video_id": "0192f0a0-0000-7000-8000-000000000001", "version_id": "0192f0a0-0000-7000-8000-000000000002", "scene_key": "scn_hook"},
    "refs": {
      "cast": [{"character_key": "char_alex", "creator_version_id": "0192f0a0-0000-7000-8000-000000000011", "appearance_version_id": "0192f0a0-0000-7000-8000-000000000012", "voice_version_id": "0192f0a0-0000-7000-8000-000000000013", "memory_snapshot_id": "0192f0a0-0000-7000-8000-0000000000a1"}],
      "world": {"world_version_id": "0192f0a0-0000-7000-8000-000000000020", "camera_position_key": "cam_desk_front", "time_of_day": "late_afternoon", "weather": "clear"}
    },
    "content_digest": "sha256:…"
  },
  "content": {
    "cast": [
      {
        "character_key": "char_alex",
        "dna_behavior_digest": "sha256:…",
        "behavior_profile": {
          "baseline": {"energy": 0.65, "confidence": 0.7, "warmth": 0.6, "expressivity": 0.55},
          "emotion_ranges": {"confident": [0.3, 0.8], "serious": [0.2, 0.8], "amused": [0.1, 0.6]},
          "habits": [
            {"memory_item_id": "0192f0a0-0000-7000-8000-0000000000b1", "kind": "gaze_habit.thinking_glance", "value": {"direction": "down_left", "typical_ms": 600}, "confidence": 0.8},
            {"memory_item_id": "0192f0a0-0000-7000-8000-0000000000b2", "kind": "reaction_habit.laughter", "value": {"expression": "small_smile", "intensity": 0.3, "frequency": "often"}, "confidence": 0.7}
          ]
        }
      }
    ],
    "world": {
      "behavior_digest": "sha256:…",
      "affordances": {"attention_targets": ["camera", "el_monitor", "el_window"], "seated": true, "allowed_postures": ["seated_upright", "seated_relaxed", "seated_lean_in"], "hand_space": "desk_surface"}
    },
    "intent": {"narrative_goal": "challenge_common_belief", "emotional_goal": "create_doubt_then_reveal", "audience_effect": "curiosity", "persuasion_goal": "none", "information_goal": "state_misconception", "attention_goal": "delay_payoff", "reveal_strategy": "tease_then_reveal", "tension_level": 0.6, "curiosity_level": 0.8, "performance_strategy": "start_confident_then_lower_energy"},
    "situation": {"kind": "contradicting_the_audience", "audience_stance": "agrees_with_misconception", "stimulus": null},
    "trajectory": [
      {"key": "st_1", "character_key": "char_alex", "span": {"kind": "words", "start": {"segment_key": "seg_1", "word": 0}, "end": {"segment_key": "seg_1", "word": 7}},
       "internal_state": {"label": "amused_certainty"}, "social_goal": "build_rapport", "audience_goal": "make_viewer_nod_along", "performance_intent": "mock_agreement",
       "emotion": {"felt": {"label": "amused", "intensity": 0.4}, "displayed": {"label": "confident", "intensity": 0.6}, "masking": true},
       "confidence": 0.7, "attention_target": "camera",
       "strategies": {"prosody": "assertive_light", "gaze": "hold_camera", "gesture": "illustrative_light", "posture": "seated_upright", "reaction": "suppressed", "camera_awareness": "direct_address"},
       "transition_in": null, "priority": "should"},
      {"key": "st_2", "character_key": "char_alex", "span": {"kind": "words", "start": {"segment_key": "seg_2", "word": 0}, "end": {"segment_key": "seg_2", "word": 5}},
       "internal_state": {"label": "deliberate_reveal"}, "social_goal": "establish_authority", "audience_goal": "create_doubt", "performance_intent": "undercut_belief",
       "emotion": {"felt": {"label": "serious", "intensity": 0.6}, "displayed": {"label": "serious", "intensity": 0.6}, "masking": false},
       "confidence": 0.85, "attention_target": "camera",
       "strategies": {"prosody": "slow_measured", "gaze": "glance_away_and_return", "gesture": "still", "posture": "seated_lean_in", "reaction": "none", "camera_awareness": "direct_address"},
       "transition_in": {"trigger": {"kind": "realization", "at": {"segment_key": "seg_2", "word": 0}}, "style": "sudden", "duration_words": 1}, "priority": "must"}
    ],
    "events": [
      {"key": "ev_1", "character_key": "char_alex", "type": "look_away", "dimension": "gaze", "at": {"segment_key": "seg_2", "word": 2}, "duration_ms": 700, "direction": "down_left", "intensity": 0.5, "purpose": "thinking", "priority": "should", "source": "director"},
      {"key": "ev_2", "character_key": "char_alex", "type": "small_smile", "dimension": "facial_expression", "at": {"segment_key": "seg_2", "word": 5}, "duration_ms": 600, "intensity": 0.3, "purpose": "knowing", "priority": "nice", "source": "director"}
    ],
    "prosody_directives": [
      {"character_key": "char_alex", "segment_key": "seg_1", "strategy": "assertive_light", "rate": 1.0, "energy": 0.65, "pitch_variation": 0.5, "emphasis_words": [6], "pauses": [], "nonverbal": []},
      {"character_key": "char_alex", "segment_key": "seg_2", "strategy": "slow_measured", "rate": 0.9, "energy": 0.55, "pitch_variation": 0.35, "emphasis_words": [5], "pauses": [{"after_word": 3, "ms": 350}], "nonverbal": []}
    ],
    "continuity": {
      "requirements": [
        {"kind": "attention_target", "character_key": "char_alex", "ref": "el_monitor"},
        {"kind": "posture_carry", "character_key": "char_alex", "ref": "previous_scene_end"}
      ]
    },
    "constraints": {"locks": ["voice"], "avoidances": ["gesture:finger_point_at_camera", "emotion_visual:angry>0.5"], "out_of_character": [], "max_intensity": 0.8},
    "requested_controls": [
      {"item_ref": "/scenes[scn_hook]/acting/states[st_1]/emotion", "character_key": "char_alex", "dimension": "emotion_visual", "value": "displayed:confident@0.6;felt:amused@0.4;masking", "temporal_precision": "segment", "priority": "should", "planner_confidence": 0.8, "observation_reliability": "low"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_1]/strategies/reaction", "character_key": "char_alex", "dimension": "reaction", "value": "suppressed", "temporal_precision": "segment", "priority": "should", "planner_confidence": 0.7, "observation_reliability": "low"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_1]/strategies/gesture", "character_key": "char_alex", "dimension": "gesture", "value": "illustrative_light", "temporal_precision": "segment", "priority": "nice", "planner_confidence": 0.6, "observation_reliability": "medium"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_2]/emotion", "character_key": "char_alex", "dimension": "emotion_visual", "value": "serious@0.6", "temporal_precision": "word", "priority": "must", "planner_confidence": 0.9, "observation_reliability": "medium"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_2]/emotion", "character_key": "char_alex", "dimension": "emotion_vocal", "value": "serious@0.6", "temporal_precision": "segment", "priority": "must", "planner_confidence": 0.9, "observation_reliability": "medium"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_2]/strategies/posture", "character_key": "char_alex", "dimension": "posture", "value": "seated_lean_in", "temporal_precision": "segment", "priority": "should", "planner_confidence": 0.7, "observation_reliability": "medium"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_2]/strategies/gaze", "character_key": "char_alex", "dimension": "gaze", "value": "glance_away_and_return", "temporal_precision": "segment", "priority": "should", "planner_confidence": 0.7, "observation_reliability": "high"},
      {"item_ref": "/scenes[scn_hook]/acting/events[ev_1]", "character_key": "char_alex", "dimension": "gaze", "value": "look_away:down_left@seg_2.w2+700ms", "temporal_precision": "word", "priority": "should", "planner_confidence": 0.8, "observation_reliability": "high"},
      {"item_ref": "/scenes[scn_hook]/acting/events[ev_2]", "character_key": "char_alex", "dimension": "facial_expression", "value": "small_smile@seg_2.w5", "temporal_precision": "word", "priority": "nice", "planner_confidence": 0.6, "observation_reliability": "high"},
      {"item_ref": "/script/segments[seg_2]/annotations[an_2]", "character_key": "char_alex", "dimension": "prosody_pause", "value": "350ms after w3", "temporal_precision": "word", "priority": "must", "planner_confidence": 0.9, "observation_reliability": "high"},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_2]/strategies/prosody", "character_key": "char_alex", "dimension": "prosody_rate", "value": "slow_measured", "temporal_precision": "segment", "priority": "should", "planner_confidence": 0.8, "observation_reliability": "high"}
    ],
    "provenance": [
      {"item_ref": "/scenes[scn_hook]/acting/events[ev_1]", "source": "director", "supported_by": ["memory:0192f0a0-0000-7000-8000-0000000000b1"]},
      {"item_ref": "/scenes[scn_hook]/acting/states[st_1]/strategies/reaction", "source": "director", "supported_by": ["intent:/scenes[scn_hook]/intent/performance_strategy", "memory:0192f0a0-0000-7000-8000-0000000000b2"]},
      {"item_ref": "/script/segments[seg_2]/annotations[an_2]", "source": "intent_policy", "supported_by": ["intent:/scenes[scn_hook]/intent/reveal_strategy"]}
    ]
  }
}
```

### 15.7 Behavior compiler

`compile(cbs, target) -> CompiledBehavior`, where `target` is `{node_kind, route, capability matrix (declared ∧ not measured-unreliable), alignment (visual only)}`. The core compiler is engine-agnostic. It chooses a **realization method** for every requested control:

| Method | Coverage level | Typical use |
| --- | --- | --- |
| `native_parametric` | HONORED | The engine exposes a control for this dimension at the needed temporal precision |
| `native_segment` | HONORED | Segment-level emotion or action control (SoulX-LiveAct- or AptAvatar-class engines, once validated) |
| `text_prompt_segment` | APPROXIMATED | Per-segment text conditioning without verified control |
| `text_prompt_global` | APPROXIMATED | One prompt per shot. Valid only when the state covers the shot |
| `shot_split` | APPROXIMATED | Split a shot at a state transition so each part gets its own global prompt (plan-time) |
| `prosody_transfer` | APPROXIMATED | Rely on the voice performance to drive audio-driven expression and head motion |
| `audio_nonverbal` | HONORED (audio) / APPROXIMATED (visual) | Laugh, sigh or breath synthesized by TTS |
| `editorial_cutaway` | APPROXIMATED | A cutaway or B-roll overlay over a behavior the face cannot perform (plan-time) |
| `editorial_punch` | APPROXIMATED | Punch-in or punch-out to sell an emphasis or realization |
| `caption_emphasis`, `music_cue`, `sfx_cue` | APPROXIMATED | Supporting channels |
| `keyframe_conditioning` | APPROXIMATED | Set the starting expression, posture and attention of a talking shot through its keyframe (`behavior.keyframe_state` → `image.keyframe`). Audio-driven engines appear to inherit much from the reference frame [RV: measure per engine] |
| `pose_guided` | HONORED once validated | Pose-conditioned engines driven by a licensed motion reference (V2) |
| `post_expression` | HONORED once V2 validates it | Deterministic expression edit (blink, glance, smile) |
| `omit` | UNSUPPORTED | Nothing available. The item stays in the CBS and is shown as unsupported |

Methods are selected from a configurable preference order per dimension (`config/vocab/behavior_dimensions.yaml`). Editorial methods are allowed only where the mode template permits them.

**Outputs.**
- `realizations[]`: `{item_ref, dimension, level, method, detail}`.
- An abstract `ProsodyPlan` per segment.
- An abstract `VisualPlan` per shot or chunk: sub-spans with vocabulary descriptors, abstract knob values (`expressivity`, `motion_energy`, `head_motion`) and segment lists.
- `editorial_actions[]`.
- `predicted_coverage`.

**Two compile passes:**
1. **Plan-time** (Director stage 11) runs against the plan-time routes. It may propose spec changes (shot splits, cutaways, punch-ins); the Director applies them, marked `derived_from: compiler_approximation` with the planned route's `route_digest`. Whenever a later version's route for the affected shot differs from that digest (re-route, fallback, coverage upgrade), the system **auto-proposes an EditOperation** that removes or re-plans those approximations, with the coverage delta shown. Nothing is changed silently (I1).
   While such a proposal is open, the version carries the flag `approximations_stale`; applying or dismissing the proposal clears it.
2. **Build-time** (`behavior.compile_voice` and `behavior.compile_visual`) produces the final abstract plans. It may never change spec structure. If a fallback route has lower capability than the planned one, the build-time pass records the coverage downgrade, and the version is flagged `coverage_changed`.

**Translators.** Every behavior-capable plugin ships a `BehaviorTranslator`: `translate(compiled: CompiledBehavior, base_request) -> engine_request`. It turns abstract plans into native syntax: prompt text, parameters, tags, segment JSON. It is the **only** code that knows engine parameter names (I1). Its version is part of the route and of the cache key. Translators are unit-tested with golden files.

**Abstract knobs and calibration.** A manifest maps each abstract knob to an engine parameter and range, together with a monotonicity claim. `CalibrationWorkflow` (Phase 8) measures the knob → observed-effect curves and stores them in `model_behavior_profiles`. It is started by the calibrate scripts or `POST /v1/admin/models/{model_id}:calibrate`. Until a knob is calibrated, the compiler does not use it. The method falls back, and coverage says why.

**Retry targets by method.** When Performance QA (§16.5) wants a retry, the node to re-run depends on the method:

| Method | Node re-run |
| --- | --- |
| `native_*`, `text_prompt_*`, `shot_split`, `keyframe_conditioning`, `pose_guided` | `avatar.render` (new attempt seed); for `keyframe_conditioning`, first `image.keyframe` |
| `prosody_transfer`, `audio_nonverbal` | `tts.segment` (attempt seed) and then the dependent `avatar.render`. Only when voice is unlocked; when it is locked, go straight to `needs_review` |
| `editorial_*`, `caption_emphasis`, `music_cue`, `sfx_cue` | No regeneration for the behavior itself. If `approximation_executed` is false, re-run the timeline/`render.final` (that would be a bug) |
| `post_expression` | `post.expression` |
| `omit` | none |

**Coverage report.** There are three `BehaviorCoverageReport` stages:
- **predicted**, in the `plan_report` at plan time;
- **compiled**, in the CompiledBehavior artifacts at build time;
- **observed**, in the `coverage_report` artifact written by the `behavior.coverage` node.

Each entry links its CBS item, CompiledBehavior realization and observation, and `video_versions.coverage_summary` holds the counts. Section 16 defines the observed part.

### 15.8 Anti-repetition

- **Within a video**: consecutive talking shots of the same creator vary their compiled descriptors (descriptor variants from `descriptions.yaml`), framing and, where allowed, camera position.
- **Across videos**: after observation, each take gets a **behavior signature** (gesture-energy profile, head-motion pattern, expression sequence, emotional-arc shape) that is recorded in the creator usage log (§18). The repetition guard compares new plans and new takes against recent signatures. Take ranking penalizes near-duplicates, and the Director avoids reusing a recent emotional sequence for the same creator unless the strategy pack asks for a signature format.

---

## 16. Observed behavior and Performance QA

Requested behavior is a plan. Compiled behavior is a claim about execution. **Observed behavior** is what is measurable in the output. The system keeps all three (ADR 0023, I4).

### 16.1 Analyzers (capabilities, §23)

| Capability | MVP adapter (CPU unless noted) | Measures |
| --- | --- | --- |
| `face.landmarks` | MediaPipe Face Landmarker | head yaw, pitch and roll; gaze proxy (head pose + iris offset); blink events (eye closure); smile, brow raise and jaw-open scores (blendshapes [RV]) |
| `body.landmarks` | MediaPipe Pose + Hand Landmarkers | hands visible; per-hand motion energy (gesture intensity); shoulder/nose geometry (lean and posture proxy); head-motion energy |
| `audio.prosody` | `prosody_features` (librosa) | pitch contour and range, energy, speech rate, pause positions and lengths |
| `asr.align` | (the existing aligner) | timing of pauses and reaction words relative to anchors |
| `vision.video` | vLLM VLM (GPU) | structured per-window questions, e.g. "between 3.2 and 4.0 s, does the person look away from the camera?" or "is the expression closer to A or B?". Sub-second temporal precision is unverified [RV], so these answers start at `reliability: low` |
| `audio.emotion` | `audio_emotion` (sandbox until the license is verified [RV]) | coarse audio emotion classes, mapped to canonical labels through proxies |
| `face.embed`, `voice.embed`, `image.embed` | AuraFace, a speaker-verification model, an image embedding model (§23) | identity and continuity (§19, §20) |

### 16.2 Observation proxies

`config/vocab/observation_proxies.yaml` maps each requestable dimension and label to:
- measurable proxies;
- thresholds and a timing tolerance (± ms around the anchored time);
- a **reliability class** (`high`, `medium`, `low`);
- a minimum analyzer confidence.

**The shipped reliability classes are initial hypotheses [RV].** Phase 7 calibrates them on the fixture clips, and Phase 11 calibrates them against human ratings. The measured reliability is stored per analyzer revision, and that measured value is what QC uses. Initial values:

| Proxy | Measure | Initial reliability |
| --- | --- | --- |
| `look_away` | gaze deviation above θ for at least d ms, within ±300 ms of the anchor | high |
| `small_smile` | smile score above θ | high |
| `nod` | head pitch oscillation | medium |
| `seated_lean_in` | shoulder-width ratio increase | medium |
| `slow_measured` | speech rate below the creator's baseline by x% | high |
| `confident` | steady gaze ratio, moderate head motion, pitch stability | low; never a gate, VLM or human preferred |
| sub-second VLM window questions | — | low until benchmarked against human ratings |

### 16.3 ObservedBehavior (per take)

`ObservedBehavior` is an artifact (kind `observed_behavior`) produced by `behavior.observe` for each raw take. Observation has two halves:
- **Measurement**: tracks and signatures, keyed only by the take artifact. It is the expensive half, and is reused whenever the take is unchanged.
- **Judgement**: verdicts of the measured tracks against the CBS requests. It is cheap, runs in `qc.shot` (per take, for ranking) and `behavior.coverage` (viewer level), and is recomputed whenever the requests change. VLM window questions are part of judgement, because their wording depends on the request.

The artifact contains:
- the analyzers used (adapter id and revision);
- summarized tracks per visible character (gaze, blinks, head motion, smile, gesture energy, posture, speech rate, pauses, pitch);
- the take's behavior signature.

The judgement writes `item_observations[]` (`{item_ref, character_key, dimension, verdict, measures, confidence, method}`) to the take's `qc_reports` row and, at viewer level, to the `coverage_report`. Every judged item also produces a `behavior_observations` row (§16.6).

**Verdicts:**
- `CONFIRMED`;
- `PARTIAL`: present, but weaker, mistimed or shorter;
- `NOT_OBSERVED`;
- `CONTRADICTED`: the opposite behavior was observed;
- `NOT_MEASURABLE`: no reliable proxy, or analyzer confidence too low;
- `NOT_APPLICABLE`: the item has no observable target in this artifact. For example, a vocal item checked against a video-only track, or a character who is not in the take.

### 16.4 Requested vs compiled vs observed (viewer level)

The `behavior.coverage` node runs after `render.final`. It judges each CBS item on **what the viewer actually sees and hears in the final timeline**:
1. It maps the take observations through the EDL to the item's window.
2. It runs audio proxies on the final mix.
3. It measures `approximation_executed` from the EDL: whether the editorial action (cut present, punch-in at the word, overlay span) happened where the compiler planned it.

The result is the `coverage_report` artifact. Each entry looks like this:

```json
{
  "item_ref": "/scenes[scn_hook]/acting/events[ev_1]",
  "character_key": "char_alex",
  "dimension": "gaze",
  "requested": "briefly look away while thinking (down_left, 700 ms at seg_2.w2)",
  "compiled": {"level": "APPROXIMATED", "method": "editorial_cutaway", "detail": "overlay sht_2 covers seg_2.w2–w3"},
  "approximation_executed": true,
  "observed": {"verdict": "NOT_OBSERVED", "measures": {"gaze_deviation_deg_max": 3.1}, "confidence": 0.9},
  "outcome": "APPROXIMATED_NOT_OBSERVED",
  "summary": "APPROXIMATED → FAILED OBSERVATION",
  "expected_for_method": true
}
```

**Outcome** = `{level}_{verdict}`. There are exactly 12 outcomes:
- the 4 `HONORED_*` outcomes: `CONFIRMED`, `PARTIAL`, `NOT_OBSERVED`, `CONTRADICTED`;
- the 4 `APPROXIMATED_*` outcomes with the same verdicts;
- `UNSUPPORTED`: nothing was executed and nothing was observed;
- `UNSUPPORTED_OBSERVED`: nothing was executed, but the behavior happened anyway (emergent). This is recorded so that measured profiles can learn from it;
- `NOT_MEASURABLE` and `NOT_APPLICABLE`, which keep their level in a separate field.

Display rules:
- `*_NOT_OBSERVED` and `*_CONTRADICTED` display as **FAILED OBSERVATION**. When `expected_for_method` is true, the UI adds "(expected: approximated editorially, not visible on the face)".
- `*_PARTIAL` displays as **PARTIAL**.
- Only `*_CONFIRMED` counts as delivered (I9).

`expected_for_method` is true for editorial methods. The face usually does not show the requested behavior there: the cut *implies* it rather than showing it. The QC ladder therefore judges such an item on `approximation_executed`, not on the face.

### 16.5 Performance QA policy (`config/qc/behavior.yaml`)

Severity depends on the item's priority and outcome:
- **`must` items**: `HONORED_NOT_OBSERVED` or `HONORED_CONTRADICTED`, or `APPROXIMATED_NOT_OBSERVED` or `APPROXIMATED_CONTRADICTED` with `expected_for_method: false`, triggers a QC retry. The node to re-run comes from the method (§15.7 retry targets). If the retry fails, try the fallback route when the measured profile favors one, then mark `needs_review` with the best take. `*_PARTIAL` warns.
- **`should` items** influence take ranking.
- **`nice` items** are informational.
- `NOT_MEASURABLE`, `NOT_APPLICABLE` and low-reliability proxies never fail a node; they can only warn.
- Editorial items with `approximation_executed: false` are treated as render defects.
- Observation results rank takes together with the other QC metrics.

### 16.6 Feeding benchmarks and routing

Every viewer-level entry and every raw take observation writes a `behavior_observations` row with: version, take, item, character, dimension, requested, method, level, verdict, outcome, adapter, translator version, revision, language.

Aggregation (nightly and on demand) updates `model_behavior_profiles`. The key is adapter × translator version × model revision × dimension × language, and every profile carries a `source` (`mock | bench | production`).

The router uses these profiles as a quality signal and **ignores `mock`-sourced profiles unless `MOCK_GPU=true`**. The compiler treats a declared control as **unreliable** when its measured success rate is below a configurable threshold, and then plans it as `APPROXIMATED`, with a warning. Benchmarks (§24) run the behavior fixtures of the golden set to produce the same profiles for sandbox engines.

### 16.7 Mock mode and test fixtures

- **Mock avatar adapters** write a ground-truth `behavior_track.json` sidecar describing what they "performed". The track follows their declared matrix and the compiled directives, with configurable failure injection. The mock observer reads it with noise, so the full triad (requested → compiled → observed → outcome → QC action) runs on CPU in tests.
- **Real CPU analyzers** are tested on short fixture clips with known gaze, blink, smile and lean events. The clips must be licensed for this use: a consented recording supplied by the product owner, or a permissively licensed clip (section 41). Without them, the tests skip with an explicit reason.

### 16.8 Honesty limits

Analyzer outputs are proxies. Audio emotion classes are coarse, and VLM reads of generated faces are noisy. Human ratings (`human_ratings`) are ground truth for subjective items such as "feels confident" or "same person". The UI shows observation confidence and never upgrades `NOT_MEASURABLE` into a pass.

---

## 17. Creator DNA, appearance, identity pack, Creator Test

Implement in `ce_creator`.

### 17.1 Creator DNA (in `CreatorVersion.dna`)

Creator DNA describes **tendencies, targets and bounds**. It never describes engine controls. It is used:
- to default and bound the Director's acting plan;
- to resolve the CBS;
- to rank takes by similarity to the creator's habits;
- to measure consistency across videos.

Fields that drive control use the closed vocabularies; scalars are 0–1 unless stated otherwise.

| Section | Fields |
| --- | --- |
| identity and canon | display name; a short bio; **canon facts** `[{key, subject, predicate, object, pinned}]` (city, occupation, pets…) used by the contradiction checker. The creator `kind` lives on `creators.kind`, and `age_appearance` on the AppearanceDNA |
| personality | trait scales (openness, conscientiousness, extraversion, agreeableness, neuroticism); values; humor style (vocab); directness; warmth; sarcasm; seriousness; conversational style (vocab) |
| speech | vocabulary level; signature phrases `[{text, max_per_video}]`; sentence-structure preference; filler tendency `{fillers[], rate}`; transition phrases; pronunciation notes (→ lexicon proposals); `speech_rate_preference` (`slow \| average \| fast`). Measured WPM lives only on the VoiceVersion |
| behavior | baseline `{energy, confidence, warmth, expressivity}`; emotion ranges `{label: [min, max]}`; escalation and de-escalation style; masking tendency; reaction habits `{surprise, laughter, hesitation, skepticism, excitement, frustration}: {expression, intensity, frequency}` |
| gesture and posture | preferred gestures with frequency; gesture density; preferred hand; head-movement amplitude; default posture; fidgets |
| gaze | eye-contact ratio; typical look-away directions and durations; thinking-gaze habit |
| camera | framing preference; camera distance; selfie behavior; movement preference; preferred camera profiles |
| fashion | style descriptors; avoid list. Default wardrobe ids are columns of the creator version |
| world | world-kind preferences; avoid list. Default world ids are columns of the creator version |
| editing | cut-cadence preference; caption-style preference; music-taste descriptors |
| avoidances | gestures, emotions above intensity caps, camera behaviors, wardrobe, worlds, phrases, topics |

**DNA is model-independent** (I1). The behavior, gaze and gesture fields are honored only as far as the routed engine allows. For today's audio-driven engines, most of them work as **targets for take selection and consistency scoring**, not as controls; coverage and observation report this honestly.

A `CreatorVersion` also pins `appearance_version_id`, `voice_version_id`, `default_world_ids` and `default_wardrobe_version_ids`, as columns (§29). Creating a new appearance or voice version does not change existing creator versions; making it the default creates a new creator version.

### 17.2 Appearance and identity pack

`AppearanceDNA` covers face shape, skin, hair, eyes, distinctive features, body type, grooming style and `age_appearance`. An `AppearanceVersion` stores the canonical face asset, the identity pack and LoRA artifacts (V1).

**Identity pipeline** (`BuildIdentityPackWorkflow`, on a draft appearance version):
1. Generate 8–16 face candidates with the image engine.
2. The user picks the canonical face.
3. Expand it to angles and expression references with the image-edit engine, always conditioned on the canonical face and never chained from previous edits.
4. Score each image by face-embedding similarity (`face.embed`) to the canonical face.
5. The user approves or rejects each image.
6. Approve the version.

**Wardrobe versions** are generated the same way by `BuildWardrobeWorkflow`: conditioned on the canonical face, scored and approved. They are stored as `WardrobeVersion`s of the creator, not inside the appearance.

**Keyframes are composed, not stored in the appearance.** Each talking shot's keyframe is the node `image.keyframe`. It combines appearance version × wardrobe version × world plate (§19) × framing × lighting, and is checked for identity before animation. Adding a world or an outfit never forces a new appearance version.

### 17.3 Creator kinds and age safety

- **`synthetic`** (fictional): the creator must present as an adult. Three checks are required and all are logged in `audit_logs`:
  - the `age_appearance` field must be ≥ 18;
  - a VLM apparent-age estimate on the canonical face, run inside `BuildIdentityPackWorkflow` after the canonical face is chosen, with a conservative margin (an estimate below 25 blocks approval pending review) [RV: VLM age estimates are unreliable, which is why this is only one of three checks];
  - an attestation by the creator's owner.
- **`digital_twin`**: requires a valid, unexpired, unrevoked consent. Disabled by feature flag `digital_twins_enabled=false` until V1.

### 17.4 Creator Test

`CreatorTestWorkflow` renders a fixed, about 20-second test script in the creator's default world, with the current default routes. The script contains a mini trajectory: neutral → excited → hesitant → serious, a look-away event, a small laugh and one emphasis.

It produces a scorecard stored in `creator_tests`:
- identity similarity distribution (`face.embed`) and voice similarity (`voice.embed`) against the references;
- lip-sync metric (advisory until its license is resolved, §26);
- WER;
- speech-quality score;
- measured WPM;
- accent: a human rating (ASR language ID only confirms the language; it cannot tell accents apart);
- requested vs compiled vs observed for every test item (§16);
- head- and body-motion energy (flags frozen or frantic motion);
- world fidelity (§19);
- a VLM critique (eyes, teeth, hands, naturalness);
- a human 1–5 rating and a "same person as the canonical face?" rating.

The test also establishes or updates the creator's **baselines** (§20). Drafts can be tested, and results are marked with the tested versions. History is shown in the UI.

---

## 18. Creator Memory

Implement in `ce_memory`. Creator Memory gives a creator a persistent **behavioral and content identity** that grows across videos. It is structured data first: embeddings are an index, never the store of record.

### 18.1 Categories and kinds (`config/vocab/memory_kinds.yaml`)

| Category | Kinds (typed values) |
| --- | --- |
| `speech_habit` | `vocabulary_level`, `recurring_phrase {text, max_per_video, contexts}`, `sentence_structure`, `filler_tendency {fillers, rate}`, `transition_phrase`, `pronunciation_pattern {term, respelling}`, `humor_style` |
| `reaction_habit` | `surprise`, `laughter`, `hesitation`, `skepticism`, `excitement`, `frustration`, each `{expression (vocab), intensity, frequency}` |
| `gesture_habit` | `common_gesture {gesture, frequency}`, `gesture_density`, `preferred_hand`, `head_movement`, `posture` |
| `emotional_tendency` | `baseline_mood`, `emotional_range`, `escalation`, `deescalation`, `confidence_pattern` |
| `gaze_habit` | `eye_contact_ratio`, `thinking_glance {direction, typical_ms}`, `look_away_frequency` |
| `camera_habit` | `framing_preference`, `camera_distance`, `selfie_behavior`, `movement_preference` |
| `social_behavior` | `directness`, `warmth`, `sarcasm`, `seriousness`, `conversational_style` |
| `avoidance` | `gesture`, `emotional_state`, `camera_behavior`, `wardrobe`, `world`, `phrase`, `topic` |
| `persona_fact` | `{subject, predicate, object}`, e.g. "lives in · Austin" |
| `stance` | `{topic, position, strength}` |
| `preference` | learned user preferences (for example "smile intensity −0.2 on this creator") |
| `rejected_pattern` | patterns of rejected proposals or takes |

Recurring worlds, wardrobes and products are **not** copied into memory as text. They are entities; memory and the usage log reference them by id.

### 18.2 Memory item schema (`creator_memory_items`)

- **Identity**: `id`, `org_id`, `creator_id`, `category`, `kind`, a `key` (normalized dedup key, e.g. a phrase's normalized text) and a `value_hash`.
- **Content**: `value` (typed by kind; a Pydantic discriminated union), `text` (a human-readable summary), `embedding` (optional), `vocab_version`.
- **Source**: `source {type: authored | plan | export | observation | user_edit | critique | import, video_id, version_id, element_key, edit_proposal_id}`.
- **Evidence**: `confidence` (0–1), `evidence_count`, `first_seen_at`, `last_seen_at` (recency).
- **State**: `status` (`proposed | active | forgotten | superseded`), `pinned`, `superseded_by_id`, `conflict_ids`, `conflict_state` (`none | unresolved | resolved`).
- **Version scope**: `creator_version_from`, `creator_version_to` (nullable): the creator versions the item applies to.
- **Audit**: `created_by`, `updated_at`.

Only `active` items (pinned or not) influence planning. Pinned items always enter a snapshot and outrank unpinned ones. Forgotten items are excluded but kept for audit until deleted (18.9).

### 18.3 Usage log (`creator_usage_events`, append-only)

The usage log has one append-only event per cast member when a version reaches `ready` (`event: ready`), and another when it is exported (`event: exported`). Repetition windows count **distinct videos**, using the latest event of each video, so versions of one video never count as separate videos. Each event records:
- hooks used (text + embedding);
- phrases used (n-gram fingerprints);
- the emotional-arc signature;
- the gesture and behavior signatures of the selected takes;
- the visual-pattern signature (shot-type and camera sequence, world, camera position, time of day, wardrobe);
- the worlds and wardrobes used.

The repetition guard (18.6) and the consistency reports (§20) read it.

### 18.4 Write paths

The usage event on `ready` is written directly by `GenerateVersionWorkflow`, starting in Phase 4. All other write paths run in `MemoryUpdateWorkflow` (Phase 12).

| Trigger | Writes |
| --- | --- |
| User authoring (UI/API) | `active` items, `source=authored`, confidence 1 |
| Version approved (previz) | Persona facts and stances extracted from the script, as `proposed` |
| Version `ready` | A usage event (§18.3); behavior signatures of the selected takes |
| Version exported | Proposed persona facts and stances from that version become `active` (the creator "said it publicly") |
| Behavior observation on selected takes | Habit evidence is aggregated per kind (merged by `key`, `evidence_count` and confidence updated). Promoted to `active` when the evidence count and confidence pass `config/memory.yaml` thresholds |
| Repeated user edits of the same kind on the same creator (e.g. three "smile less" edits) | A `proposed` preference; the user confirms or dismisses it in the Memory panel |
| Accepted critique proposals | `proposed` preferences |
| `memory_feedback` operation ("she never says 'guys'") | A `proposed` avoidance, applied after confirmation |

Merging is deterministic: the same `(creator, kind, key)` merges evidence, and a different value for the same key creates a **conflict** (18.7).

### 18.5 Retrieval and snapshots

`MemoryRetriever.retrieve(creator_id, creator_version, context) -> MemorySnapshot`. It:
- applies structured filters (category, kind, version scope, status `active`);
- ranks text-like kinds by embedding similarity to the brief;
- applies recency decay (half-life per category), confidence weighting and pinned-first ordering;
- enforces per-category budgets from `config/memory.yaml`.

For retrieval, conflicts resolve as pinned > authored > higher confidence > more recent, and unresolved conflicts are listed in the snapshot.

The snapshot (`memory_snapshots`: id, creator version, item ids with copied values, retrieval parameters, conflicts, digest) is immutable. One snapshot per cast member is pinned in the spec (`memory.snapshots`, I7). A new plan creates new snapshots. Edits, `:replan` and derived versions reuse the pinned snapshots, unless a `refresh_memory` operation is applied.

Before embeddings exist (Phase 4), retrieval uses structured filters plus keyword matching. Embedding ranking arrives in Phase 12 behind the same interface.

### 18.6 Repetition guard

Thresholds and windows live in `config/memory.yaml`. The guard checks:
- **hooks**: similarity above θ to the creator's hooks in the last N videos → rejected. Before Phase 12 this is n-gram and keyword similarity; after, embedding similarity;
- **phrases**: n-gram overlap with recent videos above θ → flagged; signature phrases are exempt up to their `max_per_video` and recency limits;
- **emotional sequences**: arc-shape similarity to the last N videos above θ → flagged, unless the strategy pack declares a signature format;
- **gesture and behavior signatures**: near-duplicates penalized in take ranking;
- **visual patterns**: the same shot-type and camera sequence plus the same world camera position, time of day and wardrobe in consecutive videos → flagged, unless the user pinned them;
- **worlds**: reusing a creator's world is continuity, not repetition. Repetition is judged on how it is shown (camera position, time of day, props state).

The results appear in previz. The Director retries the affected stage once automatically.

### 18.7 Contradiction checker

- **Script vs persona**: Director stage 6 extracts persona assertions and stances from the script (structured LLM extraction into `{subject, predicate, object}` using the vocabulary). Code then compares them with the DNA canon and active `persona_fact`/`stance` items. A conflict with a pinned or canon item blocks approval until the user overrides it (logged) or edits the script. Other conflicts warn.
- **Memory vs memory**: on insert, the same key with a different value sets `conflict_state: unresolved` on both items. Both rows can coexist because uniqueness is on `(creator_id, kind, key, value_hash)`. Code enforces at most one active, unconflicted item per (creator, kind, key, version scope). The UI offers keep A, keep B (the other becomes `superseded`), or keep both with scope.
- **DNA vs memory**: a new creator version whose DNA contradicts active items flags them for review. Nothing is deleted.

### 18.8 How memory is used

| Consumer | Uses |
| --- | --- |
| Director stage 2 | Builds the snapshot |
| Stages 4–5 | Hooks, phrases, humor, vocabulary, avoidances |
| Stage 6 | Persona facts and stances |
| Stage 7 | Worlds, wardrobes, visual habits |
| Stage 8 | Reaction, gesture and gaze habits (`reaction_habit`, `gesture_habit`, `gaze_habit`); emotional tendencies |
| CBS `behavior_profile.habits` | Resolved behavior habits |
| Take ranking and consistency baselines | Habit similarity |

Memory never changes an existing version (I7).

### 18.9 Forget, delete, security

- **Forget** sets `status=forgotten`, with an audit entry.
- **Hard delete** (`DELETE /v1/memory-items/{memory_item_id}`, through `DeletionWorkflow`) removes the text, value and embedding and keeps a tombstone.
- Memory text is inserted into prompts as delimited data (I10).
- Memory belongs to the org and the creator. It is never shared across orgs.

---

## 19. World DNA and environment continuity

Implement in `ce_world`. A **world** is a recurring environment that must stay recognizable across many videos. It is independent of Creator DNA (ADR 0021): several creators can share a world, and one creator can use many worlds.

### 19.1 World DNA (in `WorldVersion.dna`)

| Section | Fields |
| --- | --- |
| identity | `world_id`, `world_version_id`, name, kind (vocab: `home_office`, `bedroom`, `studio`, `kitchen`, `living_room`, `cafe`, `car_interior`, `outdoor_location`, …), style tags, dominant color palette |
| geometry | approximate dimensions (m), ceiling height, shape, a layout description, and a normalized floor-plan coordinate frame (x, y in 0–1, z height in m) |
| zones | `[{key: "zone_desk_chair", label, position, allowed_postures}]` (where people can be placed) |
| elements | `[{key: "el_desk", kind (vocab: furniture, prop, fixture, window, light, screen, plant, decor), label, description, position, orientation, size, material and color, signature: bool, mutability: fixed \| movable \| stateful, states: ["on", "off"], default_state (required when stateful)}]` |
| background layouts | per camera position: visible element keys in depth order and a composition description |
| lighting | key light (azimuth, elevation, intensity, color temperature K), fill ratio, practicals (element keys with color temperature), window light (direction, strength by time of day), contrast; a lighting tolerance |
| time and weather | default and allowed `time_of_day` values (`morning`, `midday`, `late_afternoon`, `golden_hour`, `evening`, `night`); default and allowed `weather` values (`clear`, `overcast`, `rain`); window visibility |
| acoustics | room profile id (`config/rooms`), RT60 estimate, ambient sound profile (room tone, HVAC, street, birds…), noise floor (dB) |
| camera positions | `[{key: "cam_desk_front", position, target, height_m, distance_m, lens_equiv_mm, default_framing, allowed_camera_profiles[], status: permitted \| forbidden}]`. Approved plates are stored only in `world_versions.plates` (§29), never inside the DNA |
| continuity | signature elements that must appear from each position; position tolerances; forbidden views |
| references | reference assets with roles (`establishing`, `position:<cam>`, `element:<el>`) |
| similarity data | **fingerprints** (computed): image embeddings, color statistics and a lighting estimate per canonical plate; stored as an artifact referenced by the version |

**Example** (abbreviated YAML; actual storage is JSON validated by `WorldDNA`):

```yaml
name: Alex's home office
kind: home_office
geometry: { dimensions_m: [3.5, 3.0, 2.6], layout: "desk against the right wall, window on the left, bookshelf behind the chair" }
zones: [ { key: zone_desk_chair, label: desk chair, position: [0.62, 0.55, 0.0], allowed_postures: [seated_upright, seated_relaxed, seated_lean_in] } ]
elements:
  - { key: el_desk, kind: furniture, label: oak desk, position: [0.7, 0.55, 0.0], signature: true, mutability: fixed }
  - { key: el_monitor, kind: screen, label: 27-inch monitor, position: [0.78, 0.55, 0.75], mutability: stateful, states: [on, off], default_state: on }
  - { key: el_shelf, kind: furniture, label: bookshelf with plants, position: [0.5, 0.95, 0.0], signature: true }
  - { key: el_neon, kind: light, label: small orange neon sign, position: [0.35, 0.97, 1.6], signature: true, mutability: stateful, states: [on, off], default_state: on }
  - { key: el_mug, kind: prop, label: white coffee mug, position: [0.72, 0.5, 0.76], mutability: movable }
  - { key: el_window, kind: window, position: [0.02, 0.5, 1.2] }
lighting: { key: { azimuth_deg: -60, elevation_deg: 20, intensity: 0.8, color_temp_k: 5200 }, fill_ratio: 0.4,
            practicals: [ { element: el_neon, color_temp_k: 2200 } ], tolerance: { color_temp_k: 400, luminance: 0.12 } }
time_and_weather: { default_time_of_day: late_afternoon, allowed_times: [morning, late_afternoon, evening], default_weather: clear, allowed_weather: [clear, overcast] }
acoustics: { room_profile: small_office, rt60_s: 0.35, ambient: [room_tone_light_hvac], noise_floor_db: -62 }
camera_positions:
  - { key: cam_desk_front, height_m: 1.2, distance_m: 0.6, lens_equiv_mm: 24, default_framing: medium_close_up,
      allowed_camera_profiles: [phone_front_selfie, webcam, laptop_camera, desk_mirrorless], status: permitted }
  - { key: cam_side_wide, height_m: 1.4, distance_m: 2.2, lens_equiv_mm: 28, default_framing: medium_wide,
      allowed_camera_profiles: [desk_mirrorless, dslr], status: permitted }
continuity: { must_show_from: { cam_desk_front: [el_shelf, el_neon] } }
```

### 19.2 Plates

`BuildWorldPlatesWorkflow` runs on a draft world version. It generates candidate **world plates** (no people) for the permitted camera positions and for the time-of-day × weather combinations the user requests. It uses the image engine and the image-edit engine, conditioned on the reference assets and the World DNA description. Candidates are stored as assets in `world_versions.plate_candidates`.

The user chooses one canonical plate per position × time of day × weather, and choosing a plate enqueues its fingerprint computation (`image.embed`, color statistics, lighting estimate). Not every combination needs a canonical plate; the default time of day and weather for each permitted position are required. Approval only checks that the required plates and their fingerprints exist, then freezes them in `world_versions.plates`.

The node `world.plate` reuses an approved canonical plate whenever the binding matches one exactly and has no overrides. Otherwise it generates a variation conditioned on the nearest canonical plate (same position) and on the `continuity_ref`. It never generates from scratch, so the world stays recognizable.

**World proposals.** When planning or an edit ("change the room to a modern office") needs a world that does not exist, the Director creates a draft world and binds the scene to its draft version (§10.2).
- Previz runs its audio parts and shows a blocking step. The user either approves the world, after `BuildWorldPlatesWorkflow` and plate choices, or picks an existing world, which is an edit.
- Until then the version keeps the flag `needs_world_approval`, and `:approve` is refused.
- Approving the draft keeps its id, so the spec stays valid. Previz then completes the plates and keyframes.

### 19.3 Scene binding and overrides

`scenes[].world` holds `world_version_id`, `camera_position_key` (must be `permitted`), `time_of_day` (must be allowed), `weather`, `overrides` and `continuity_ref`.

**Overrides**:
- `element_states {el_key: state}`;
- `hide_elements []`;
- `add_elements [{key, kind, label, description, position}]`, scene-local and prefixed `el_x_`;
- `move_elements [{key, position}]`, only for elements with `mutability: movable`;
- `lighting` deltas within tolerance, unless `deviation_declared: true`, which previz flags;
- `acoustics`: ambient additions.

**`continuity_ref`** is a typed union:
- `{kind: "world_plate", camera_position_key}`;
- `{kind: "shot", video_id, version_id, shot_key}`: match a previous video's look;
- `{kind: "asset", asset_id}`.

`world.plate` uses `continuity_ref` as a conditioning reference. `background_continuity` and `cross_video_world` QC (19.6) compare against it.

Overrides are scene-local and **never mutate World DNA** (I6). Making an override permanent is an explicit action: `POST /v1/worlds/{world_id}/versions {from_version_id, patch}` creates a new draft world version, which the user approves. Moving a video to the new world version is then an edit (`set_world_binding`), with an impact preview.

### 19.4 Locks and versioning

- Lock group `world` (scoped by scene) pins `world_version_id`, `camera_position_key`, `time_of_day`, `weather`, `overrides` and `continuity_ref`.
- The regenerate component `background` makes a new plate variation of the same binding (new seed). It is refused while `world` is locked.
- The component `world_plate` re-creates the plate from the canonical one (only for bindings with overrides).
- World versions are immutable once approved (I3), and their history and diffs are visible in World Studio.

### 19.5 Build-graph dependencies (exact invalidation)

| Change | Dirty nodes | Kept |
| --- | --- | --- |
| A scene moves to a new world version with a different plate | `world.plate`, `image.keyframe`, `avatar.render` (and later chunks), `post.*`, `qc.world`, `behavior.observe`/`qc.shot` for that scene's talking shots; world-bound B-roll; `audio.room` and `mix.audio` only if acoustics changed; `behavior.resolve` only if the world's behavior digest changed; render | TTS, alignment, music, SFX, captions, other scenes |
| Lighting override only | `world.plate`, `image.keyframe`, `avatar.render`, post and QC for that scene's shots | TTS, alignment, CBS, compile, audio |
| Acoustics override only | `audio.room`, `mix.audio`, render | All video nodes |
| Element state change (`el_monitor: off`) | Plate, keyframes, renders of shots from positions where the element is visible; `behavior.resolve` only if the element is an attention target in the CBS | Shots from positions where it is not visible |
| Camera position change | Plate, keyframe, render, post and QC for those shots; camera reframing | Audio |
| Time-of-day or weather change | Plate (a canonical one if available, otherwise a variation), keyframes, renders, post and world QC for that scene; `audio.room` only if the ambient profile differs | TTS, alignment, CBS, compile |
| Add, hide or move element | Plates and downstream nodes for positions where the element is visible; `behavior.resolve` only if the element is an attention target or affordance | Shots from positions where it is not visible |
| `continuity_ref` change | `world.plate` and downstream for the scene; the world QC reports | Audio, behavior |

Phase 6 implements these as dirty-set tests.

### 19.6 World continuity QC (`config/qc/world.yaml`)

| Check | Method | Scope |
| --- | --- | --- |
| `world_identity` | `image.embed` similarity of the shot's background (person region masked using face and body landmarks) vs the canonical plate for the bound position and time of day | per shot |
| `world_elements` | VLM structured check: signature elements present, left/right/behind relations correct, element states as bound | per shot |
| `world_lighting` | color-temperature and luminance estimates vs the World DNA plus tolerance; key-light side estimated from face shading (left/right luminance ratio on landmarks) | per shot |
| `background_continuity` | adjacent shots of the same binding: embedding + color statistics + VLM pairwise | within a video |
| `cross_video_world` | the shot vs the world fingerprints and vs the same world's shots in the creator's last N videos | across videos (§20) |

The **environment identity score** is the weighted combination recorded in the QC report. Thresholds are set per world kind. These checks are measured scores with confidence; the system never promises a perfect match. The lighting-direction heuristic and the VLM relation checks start with `reliability: low` [RV]. They only warn until calibrated against human ratings in Phase 11.

### 19.7 Acoustics and B-roll

Each scene's dialogue gets the bound world's room impulse response and ambient bed through `audio.room`. B-roll shots with `world_bound: true` are generated conditioned on the scene's world plate and description, and checked by `world_identity`. Unbound B-roll ignores the world.

---

## 20. Creator consistency across videos

The same creator should feel like the same person across many videos. The system **measures** this and never promises it.

`ConsistencyWorkflow` runs as a job after a version reaches `ready`, outside the build graph. It compares against the creator's **baselines** (`creator_baselines`, per creator version). Baselines are built from the Creator Test and updated from accepted videos (rolling window N). The resulting `ConsistencyReport` is stored in `consistency_reports`:

| Dimension | Metric | Method |
| --- | --- | --- |
| Face identity | similarity distribution vs the canonical face and the baseline | `face.embed` |
| Voice identity | speaker-embedding similarity vs the voice references and baseline | `voice.embed` |
| Speech style | WPM, pause rate, filler rate, sentence length, signature-phrase usage, vocabulary overlap vs the DNA and memory | code over transcripts |
| Behavior patterns | gaze-to-camera ratio, blink rate, head-motion energy, gesture energy, smile frequency vs the baseline distribution (z-scores or a distribution distance) | `behavior.observe` tracks |
| Emotional tendencies | trajectory features (range, transition count, masking frequency) vs the DNA and memory | CBS + observations |
| Worlds | `cross_video_world` scores | §19.6 |
| Wardrobe | VLM/embedding similarity vs the wardrobe references | `image.embed` + VLM |
| Camera habits | face-size ratio (distance), framing and camera-motion energy vs the DNA | landmarks + camera post params |

Each metric has a threshold band (`config/qc/consistency.yaml`) and a confidence. Out-of-band metrics warn by default; the user can promote selected ones to gates. Spec-level `overrides` (another voice, appearance or out-of-character acting) are listed as intentional deviations, not failures.

Automated metrics are insufficient for personality, humor and "same person" judgements. A **human evaluation queue** collects pairwise "same person?" and "in character?" ratings (`human_ratings`) on sampled videos, and the report shows both kinds of evidence.

The Creator Studio's Consistency tab charts these metrics across the creator's videos.

---

## 21. Voice and speech

Implement in `ce_voice` plus the voice plugins.

- **`VoiceVersion`** (`draft → approved`, immutable when approved). It holds:
  - per-language reference recordings (48 kHz master + derivatives) with exact transcripts;
  - a text description (for voice-design engines);
  - measured WPM by language and energy;
  - a pronunciation lexicon (`{term: respelling | phonemes}`);
  - default prosody (`accent` label, `pitch_semitones`, `speed`, `energy`, `expressivity`);
  - `consent_id` for real voices;
  - kind (`designed | cloned | preset`).

  Lexicon edits and WPM calibration happen on drafts. Editing an approved version creates a new draft.

  **Per-engine conditioning** (cached speaker embeddings, prompts) is produced by the `voice.prepare` node (`voice.clone_prepare`) and stored as `voice_conditioning` artifacts keyed by (voice version, adapter, revision), not in the version row.
- **Tag parser** (`ce_voice.tags`, Phase 4) handles the canonical acting tags typed by users: `[excited] [whispers] [laughs] [short pause] [long pause] [confused] [serious] [sad] [angry] [curious] [sigh] [breath] [emphasize] [looks away] [smiles]`.
  - Audio tags become annotations: pauses always as inserted silence, non-verbal sounds, delivery.
  - Emotion tags become acting-state overrides for their span. Each tag maps to an `emotions.yaml` label (`[confused]` → `confused`, `[curious]` → `curious`, …).
  - Visual tags become acting events.

  This one normalizer applies the resolution order in §10.6.
- **ProsodyPlan → engine.** `behavior.compile_voice` produces an abstract ProsodyPlan per segment. Each voice plugin's `BehaviorTranslator` maps it to that engine's native syntax: inline tags, an instruction string, parameters, or inserted silence. Anything it cannot express is reported `UNSUPPORTED` in coverage. Pauses are always rendered deterministically as silence.
- **Text normalization** per language (`en, de, es, fr, it, ru, tr, az, ar`) covers numbers, dates, currencies, abbreviations and lexicon substitution. Turkish and Azerbaijani letters (ı, İ, ş, ğ, ç, ö, ü, ə, x, q) must be preserved correctly.
- **Exact-script verification loop**, per sentence:
  1. normalize → synthesize → ASR transcribe;
  2. compute normalized WER/CER against the script;
  3. if above the threshold, retry with an attempt seed (N times);
  4. if still failing, flag it.

  Then run forced alignment to produce the word-timings artifact. Tags producing non-lexical sounds and `inserted_disfluency` words are handled explicitly in the WER computation.
- **No engine splicing within a voice.** If a character's TTS engine fails irrecoverably, the fallback engine re-synthesizes **all** of that character's segments in the video. Otherwise the segment is flagged `needs_review`. Never mix engines within one character's dialogue, because different engines change timbre and prosody.
- **Language routing.** `config/languages.yaml` sets each language's `support` and its preferred aligner. The router allows an engine's `unvalidated` languages only when the language's support is `beta`, in any mode, and the UI shows a beta badge. Azerbaijani is `unsupported`: no default engine, and the UI shows it as unsupported unless a licensed engine with `az` is installed.
- **Alignment per language:**
  - en, de, es, fr, it, ru → Qwen3-ForcedAligner;
  - tr, ar → a CTC forced-alignment adapter (`ctc_aligner`) over a permissively licensed multilingual CTC model. The candidate is Omnilingual ASR CTC [RV: license and language coverage at the pinned revision]. Fallback: faster-whisper word timestamps, marked `alignment_precision: coarse`, which widens QC timing tolerances and is shown in previz.
- **Accent changes are honest.** Pitch and speed are engine parameters, or post-processing within ±2 semitones / ±10% when the engine lacks them. Accent is part of voice identity: changing it creates a new `VoiceVersion`, via a voice-design engine with a new accent description or a new reference recording. **That changes the voice's timbre**, and the impact preview says so explicitly.

  Because word timings change, the impact preview offers two options:
  - a lip-sync patch, only if the total duration delta is under a configurable threshold (default 5%) and anchored cuts still land inside their words;
  - otherwise, a full re-render.

  The patched region is checked for identity similarity. Coverage reports `accent_control: design_only | prompt_text | none` per engine.
- **Designed voices** for synthetic creators (`VoiceDesignWorkflow`):
  1. Generate candidates with a voice-design engine (VoxCPM2 / Qwen3-TTS VoiceDesign [RV]).
  2. The user picks one.
  3. The chosen sample becomes the reference for cloning engines.

  Producing the *same* timbre in another language is not guaranteed. Speaker similarity across languages is measured in the voice test and the Creator Test. No real-person cloning is allowed until V1 (consent flag).
- **WPM calibration** runs in `VoiceTestWorkflow` and `CreatorTestWorkflow`. It is stored on a draft voice version, which is then approved. The Director uses it for duration fitting. A seeded creator ships with default WPM values, so earlier phases work.

---

## 22. Camera simulation and realism

- `config/camera_profiles/*.yaml`. Every profile has a `maturity`.
  - **Production**: `phone_front_selfie`, `phone_rear_handheld`, `webcam`, `laptop_camera`, `desk_mirrorless`, `dslr`, `cinematic`, `pov`, `screen_webcam_bubble`, `screen_only`.
  - **Experimental** (used by V1/V2 modes): `podcast_two_cam`, `interview`, `street_interview`, `walking_vlog`, `car_vlog`.

  Profiles include subject `distance_m`, lens, framing, motion, stabilization, focus, exposure, depth of field, sensor noise, motion blur and frame rate. A shot's profile must be in its world camera position's `allowed_camera_profiles`.

```yaml
id: phone_front_selfie
label: Smartphone selfie (front camera)
maturity: production
prompt_hints: ["shot on a smartphone front camera at arm's length", "natural window light"]
sensor: { fps: 30, noise_luma: 0.012, noise_chroma: 0.006, rolling_shutter: 0.002 }
lens: { focal_equiv_mm: 23, distortion_k1: 0.03, vignette: 0.15, dof: deep }
framing: { default: medium_close_up, headroom: 0.08, eye_line: 0.38, overscan: 0.12 }
motion: { type: handheld, amplitude_px: 6, freq_hz: [0.3, 1.2], rotation_deg: 0.4, breathing_sway: true, stabilization: 0.6 }
focus: { autofocus_hunts_per_min: 0.5 }
exposure: { auto_exposure_drift: 0.03 }
color: { lut: luts/phone_natural.cube, saturation: 1.02 }
compression: { codec: h264, bitrate_kbps: 8000 }
audio: { mic_profile: phone_front_mic, distance_m: 0.4 }
```

- **Prompt hints are model-agnostic.** `prompt_hints` and framing feed the translators; generation resolution includes the `overscan` margin.
- **Resolution and frame-rate budget per tier.** `config/routing/*.yaml` declares each tier's generation resolution and the final preset resolution.
  - `final`: if generation is below the preset after reframing, `video.upscale` is mandatory.
  - Engine fps (for example 25) is converted to the preset fps (for example 30) by `video.interpolate`, or by frame-rate conversion for draft. The AV-offset QC is measured at the preset fps.
- **Post camera** (`ce_camera`): seeded procedural motion (a sum of filtered noise per axis, rotation, breathing sway); punch-ins and punch-outs at anchored words; optional whip and pan; motion blur proportional to velocity; autofocus breathing (brief blur ramps); exposure drift. All of it is deterministic given the seed.
- **Realism post** (`ce_realism`): LUT, white-balance drift, grain matched to the profile, vignette, lens distortion, platform-like compression. Audio: mic EQ curves (`config/mic_profiles`), world room acoustics (§19.7), the room tone bed, breaths kept, gentle de-essing only.
- **Disabled by default**: face restoration, beauty smoothing, cinematic grading on UGC modes.
- **Reframing** (`ce_camera.reframe`):
  1. Track subjects through the `face.detect`/`face.landmarks` capabilities (CPU adapter) for creators, and VLM bounding boxes for B-roll.
  2. Smooth the crop path (One-Euro filter) and compute per-aspect crop windows and a crop-loss metric.
  3. Above the threshold, fall back to composition layouts (blurred-fill background, stacked split layout, picture-in-picture).
  4. Offer native regeneration in the target aspect only with user approval, because it costs GPU time.

---

## 23. Capabilities, adapters, behavior matrix, router

Implement the interfaces in `ce_contracts` (Python ≥ 3.10, with Pydantic models for every request and result).

```python
class Adapter(Protocol):
    manifest: PluginManifest
    async def load(self, ctx: LoadContext) -> None: ...
    async def unload(self) -> None: ...
    async def health(self) -> HealthStatus: ...
    def estimate(self, request: BaseModel, hw: HardwareInfo) -> Estimate: ...      # seconds, vram_gb, notes
    async def run(self, capability: str, request: BaseModel, ctx: RunContext) -> BaseModel: ...

class BehaviorTranslator(Protocol):              # shipped by behavior-capable plugins only
    version: str
    def translate(self, compiled: CompiledBehavior, base_request: BaseModel) -> BaseModel: ...

class RunContext(Protocol):
    seed: int
    scratch_dir: Path
    logger: BoundLogger
    cancel: CancellationToken
    async def read_artifact(self, ref: ArtifactRef) -> Path: ...
    async def write_artifact(self, path: Path, kind: ArtifactKind, meta: dict) -> ArtifactRef: ...
    async def progress(self, fraction: float, message: str = "") -> None: ...
```

Each interface below adds typed convenience methods (for example `VoiceEngine.tts(req) -> TTSResult`) that delegate to `run`.

| Interface | Capabilities | Request essentials |
| --- | --- | --- |
| `LLMProvider` | `llm.structured` (selected by `LLM_PROVIDER`, not by the router) | messages, JSON schema, temperature, max tokens |
| `EmbeddingEngine` | `embed.text` | texts, language |
| `ResearchEngine` | `research.fetch`, `research.ingest`, `research.search` | URL / asset, chunking params |
| `ImageGenerator` | `image.generate`, `image.edit` | prompt, negative, references, size, steps, seed, LoRAs |
| `VideoGenerator` | `video.t2v`, `video.i2v`, `video.r2v`, `video.edit`, `video.extend`, `video.joint_av` | prompt, first/last frame, references, masks, duration, fps, resolution, seed |
| `AvatarEngine` | `avatar.a2v` | keyframe, audio, translated behavior, resolution, seed, continuation frames |
| `VoiceEngine` | `voice.tts`, `voice.design`, `voice.clone_prepare`, `voice.convert` | text, language, voice conditioning, translated ProsodyPlan, seed |
| `LipSyncEngine` | `lipsync.dub` | video, audio, face region |
| `ASREngine` | `asr.transcribe`, `asr.align`, `asr.lid` | audio, language, known text (for alignment) |
| `MusicEngine` / `SFXEngine` | `audio.music`, `audio.sfx` | description, duration, BPM, instrumental flag, seed |
| `VisionAnalyzer` / `VideoAnalyzer` | `vision.image`, `vision.video`, `vision.ocr` | media, sampling fps, structured question, JSON schema |
| `FaceAnalyzer` | `face.detect`, `face.landmarks`, `face.embed` | frames |
| `BodyAnalyzer` | `body.landmarks` | frames |
| `AudioAnalyzer` | `audio.prosody`, `audio.emotion`, `voice.embed` | audio, word timings |
| `ImageEmbedder` | `image.embed` | images, masks |
| `Upscaler` / `FrameInterpolator` | `video.upscale`, `video.interpolate` | video, scale or target size, target fps |
| `QCMetric` | `qc.lipsync`, `qc.vqa`, `qc.speech_quality` | media (+ references) |
| `CaptionEngine` | `captions.build`, `captions.translate` | word timings, style, safe zones, target language |
| `EffectsEngine` | `effects.transition`, `effects.title`, `effects.overlay`, `effects.disclosure` | effect spec, timing, brand kit |
| `ExpressionEditor` | `expression.edit` (V2; mock pass-through in the MVP) | video, landmark tracks, operations (blink, glance, smile) |
| `IdentityTrainer` | `identity.train` (V1; mock in the MVP) | appearance version, dataset refs, base model |
| `Watermarker`, `ProvenanceSigner` | `provenance.watermark_video`, `provenance.watermark_audio`, `provenance.sign` | media, payload, manifest |
| `GPUProvider` | provision, start, stop, terminate, status, list_offers, price, health | GPU class, region, image, volumes, env |
| `StorageProvider` | put, get, presign_get, presign_put, multipart, exists, delete, list | key, bucket, content type |

The capabilities `video.r2v`, `video.edit`, `video.extend`, `video.joint_av` and `voice.convert` are interfaces only in the MVP. Their node kinds arrive with the roadmap items that use them.

**Closed feature vocabularies.** Each capability declares its feature enum in `ce_contracts`, and features cover only input/output modes. For example, `avatar.a2v` has `i2v`, `v2v_dub`, `long_form_streaming`. Behavior abilities and engine properties (segment control, multi-person, pose guidance, chunk continuation) are declared **only** in the behavior matrix. The router's hard filters read both, and manifest validation rejects a behavior ability listed as a feature.

**Capability / behavior matrix** (manifest field `behavior_matrix`; dimensions from `config/vocab/behavior_dimensions.yaml`). Every behavior-capable adapter declares, per dimension, values like these. They are illustrative placeholders for an audio-driven global-prompt engine; the real values come from the model card and smoke tests [RV]:

```yaml
behavior_matrix:
  emotion_visual:     { control: text_global, temporal_precision: clip, vocabulary: any_text }
  facial_expression:  { control: emergent, temporal_precision: none }
  reaction:           { control: emergent, temporal_precision: none }
  listening:          { control: emergent, temporal_precision: none }
  gaze:               { control: emergent, temporal_precision: none }
  blink:              { control: emergent, temporal_precision: none }
  head_motion:        { control: emergent, temporal_precision: none, knobs: [motion_energy] }
  gesture:            { control: emergent, temporal_precision: none }
  posture:            { control: text_global, temporal_precision: clip }
  camera_awareness:   { control: text_global, temporal_precision: clip }
  segment_control:    { supported: false }
  prosody_coupling:   { lip_sync_from_audio: strong, expression_from_audio: strong }
  continuity:         { first_frame_conditioning: true, chunk_continuation: true, reference_identity: strong, background_preservation: medium }
  object_interaction: { control: none }
  multi_person:       { max_persons: 1 }
  temporal_control:   { max_clip_s: 60, recommended_chunk_s: 25 }
knobs:
  motion_energy: { param: audio_cfg, range: [3.0, 5.0], monotonic: unverified, calibrated: false }
```

- `control` takes one of `none | emergent | text_global | text_segment | native_segment | parametric | keyframe | pose_guided`.
- `temporal_precision` takes one of `none | clip | segment | word | frame`.
- Pose control is expressed as `control: pose_guided` on the `gesture` and `posture` dimensions. Emotion granularity is the combination of `temporal_precision` and `vocabulary` on `emotion_visual` and `emotion_vocal`.

Voice engines declare the vocal dimensions: `emotion_vocal`, `prosody_rate`, `prosody_pitch`, `prosody_energy`, `prosody_emphasis`, `prosody_pause` (always honored deterministically through inserted silence), `nonverbal_audio` (laugh, sigh, breath), `delivery` (whisper, aside) and `accent`.

Declared values are claims [RV] until benchmarks and observations measure them. Measured success rates live in `model_behavior_profiles` (ADR 0027). Model-specific translation stays inside the plugin's `BehaviorTranslator` (I1).

**Analyzers as CPU adapters.** MediaPipe, prosody features, PP-OCR, AuraFace and the image-embedding adapter run as `cpu_inproc` adapters in the orchestrator and render worker. CPU model engines (Kokoro, faster-whisper, the CTC aligner, DNSMOS, the text-embedding engine) run as `cpu_model` adapters on `worker-cpu` through the scheduler. Either way, core packages call them through the router and the contracts, never by importing the library (I14).

**Router** (`ce_router`): `route(node, context) -> RouteDecision{adapter_id, model_id, revision, translator_version, params, score, reasons, fallbacks}`.

1. **Hard filters:**
   - capability and required features;
   - language support (§21 rule: validated languages always; unvalidated only when the language's support is `beta`);
   - resolution, duration and fps limits;
   - **license policy over the model and its dependency closure** for the operator profile;
   - plugin `status` (`production` in normal routing, `sandbox` only in sandbox runs) and `validation` (production requires at least `smoke_passed`, or `MOCK_GPU=true` for mocks). CPU-capable adapters reach `smoke_passed` through CPU smoke tests in CI, with no GPU needed;
   - the manifest's `allowed_envs` includes the current `APP_ENV` (Kokoro, for example, declares `[dev, test]`);
   - the VRAM fits an available or provisionable pool;
   - health.
2. **Score**: a weighted sum from the routing profile (`draft`, `final`, `cheapest`), made of:
   - benchmark quality for capability × language;
   - **measured behavior success for the dimensions the CBS requests, weighted by item priority** (profiles with `source: mock` are ignored unless `MOCK_GPU=true`);
   - historical QC pass rate;
   - estimated cost and latency;
   - user pins (an `engine_hint` overrides if it passes the filters).
3. **Pinning**: reuse the pinned route for clean nodes (§12.4).
4. **Fallbacks**: precompute a fallback chain of other `production` adapters with the same capability. If there are none, the QC ladder skips its fallback step and says so.
5. Persist the decision, with its reasons, in the BuildManifest.

**Route preview.** `preview(capability, language, profile) -> candidates` applies only the capability, language and routing-profile filters. Director stage 8 uses it before a CBS exists, and records it in `director_runs`.

`quality_tier` (`draft | final`) selects the routing profile of the same name. `cheapest` is used only when a user or project budget policy selects it explicitly.

---

## 24. Plugin system and sandbox

- **Discovery**: Python entry points in the group `creator_engine.plugins`. Each plugin package ships a `plugin.yaml`.
- **Manifest schema**: validated with Pydantic. A plugin refuses to load if its manifest is invalid, or if any model or dependency lacks a license block.

```yaml
id: avatar.infinitetalk
name: InfiniteTalk
version: 0.1.0
kind: model_adapter
entrypoint: ce_plugin_infinitetalk.adapter:InfiniteTalkAdapter
behavior_translator: ce_plugin_infinitetalk.translator:InfiniteTalkTranslator
capabilities:
  - id: avatar.a2v
    features: [i2v, v2v_dub, long_form_streaming]   # placeholder values below until verified [RV]
    languages: { mode: audio_driven, validated: [en, zh], unvalidated: [de, es, fr, it, ru, tr, ar] }   # "validated" = by the model authors [RV]; our own validation is the `validation` field
    resolutions: ["480p", "720p"]
    max_duration_s: 60          # placeholder values: fill from the verified model card [RV]
    fps: 25
behavior_matrix: { … see section 23 … }
knobs: { … }
runtime: { family: wan, python: "3.10", cuda: "12.8", requires_gpu: true, min_vram_gb: 24, recommended_vram_gb: 48, supports_cpu: false }   # placeholders [RV]
models:
  - key: infinitetalk-single
    source: { type: huggingface, repo: MeiGen-AI/InfiniteTalk, revision: "<PIN A COMMIT SHA>" }
    files: ["…"]
    sha256: { "…": "…" }
    size_gb: 0
    license: { name: Apache-2.0, url: "…", commercial_use: true, conditions: [], verified_at: "YYYY-MM-DD", verified_by: "claude-code" }
    dependencies:
      - { ref: "Wan-AI/Wan2.1-I2V-14B-480P@<sha>",
          license: { name: Apache-2.0, url: "…", commercial_use: true, conditions: [], verified_at: "YYYY-MM-DD", verified_by: "claude-code" } }
      - { ref: "TencentGameMate/chinese-wav2vec2-base@<sha>",
          license: { name: MIT, url: "…", commercial_use: true, conditions: [], verified_at: "YYYY-MM-DD", verified_by: "claude-code" } }
schemas: { input: schemas/input.json, output: schemas/output.json, config: schemas/config.json }
defaults: { steps: 4, distill_lora: lightx2v_4step, fp8: true, chunk_s: 25 }   # [RV]
allowed_envs: [dev, test, prod]
status: production        # production | sandbox | experimental | research_only | disabled
validation: untested_on_gpu   # untested_on_gpu | smoke_passed | bench_passed | failed
maturity_notes: "Color drift beyond ~60 s; chunk to ≤ 30 s with continuation."   # snapshot-era community reports [RV]
obligations: []           # e.g. attribution text and where it must be shown (UI, docs, export metadata)
```

  The license `conditions` vocabulary is `revenue_cap_usd`, `mau_cap`, `territories_excluded`, `attribution_text`, `maas_restricted` and `non_commercial`.
- **Runtime families**: each GPU family has a Dockerfile `infra/docker/worker-<family>.Dockerfile`. A worker loads only plugins of its family, and plugins never import across families. `cpu_model` runs in `worker-cpu`; `cpu_inproc` runs in the orchestrator and render-worker images (ADR 0007).
- **Mock plugins** (`plugins/mock/`, always installed) cover every capability. Mock model adapters are in the `cpu_model` family on `worker-cpu`; mock analyzers are in the `cpu_inproc` family. They include two avatar engines with different behavior matrices:
  - `mock_avatar_global`: global text emotion, everything else emergent;
  - `mock_avatar_segment`: native segment emotion, parametric gaze and blink.

  Both emit `behavior_track.json` sidecars (§16.7). They are what the I1 model-swap test and the coverage-upgrade test run on.
- **Sandbox and promotion.**
  - `POST /v1/admin/models/{model_id}:benchmark` runs the golden evaluation set (`eval/`, including behavior fixtures) on a sandbox pool. It computes automatic metrics and behavior profiles, creates blind pairwise comparisons against the production default for human rating, and stores a `model_benchmarks` record.
  - `POST /v1/admin/models/{model_id}:promote` sets `status: production`. It requires a verified license closure, `validation ≥ smoke_passed` (Phase 8 smoke runs), and a written promotion note (stored, ADR-style).
  - A full benchmark (`bench_passed`) becomes required for promotion once the benchmark runner exists (Phase 11). Engines promoted on smoke evidence only carry `promotion_basis: smoke`, shown as a "smoke-promoted" badge, until benchmarked.
  - Research-licensed models may run only in the sandbox. Their outputs are watermarked, tagged `non_commercial`, and cannot be exported.

---

## 25. GPU scheduler, fleet, model cache, cost, recovery

**Scheduler data.** `gpu_tasks(id, org_id, node_id, attempt_id, priority, capability, model_key, vram_gb, est_seconds, constraints jsonb, payload jsonb, state queued|leased|running|succeeded|failed|cancelled, lease_worker_id, lease_expires_at, created_at, updated_at)`. `gpu_workers` is defined in section 29.

**Worker protocol** (`/internal/v1/worker/*`, served by `scheduler`, per-worker token):
- `register` → worker id and config (heartbeat interval). Self-managed hosts first exchange a one-time enrollment token (§30).
- `lease` (long-poll; the body carries hardware state, resident models, cached models and free VRAM) → zero or more tasks, each with presigned GET URLs for inputs, presigned PUT URLs for outputs, and the lease expiry. Tasks are batched when they share a model and the adapter supports batching.
- `heartbeat` (task progress, resident models) → extends the lease and returns cancel flags.
- `complete` (output artifact descriptors with SHA-256, metrics, timings) / `fail` (error class `retryable | fatal | oom | timeout | cancelled`, message, logs URI).

**Placement scoring** for a leasing worker W and a candidate task T (weights configurable):

`priority_weight(T) + age_boost(T) + resident_bonus(T.model_key ∈ W.resident) + cached_bonus(T.model_key ∈ W.cached) − load_penalty(model size / disk bandwidth) − cost_penalty(W.price)`

This is subject to `T.vram_gb ≤ W.free_vram` and T's constraints. A stickiness window prefers a worker that just loaded a model for that model's tasks for N seconds. Leasing uses `SELECT … FOR UPDATE SKIP LOCKED`.

**Lease expiry.** A reaper requeues expired leases; the attempt is marked `failed: lease_expired`, and retry counts apply.

**Fleet manager.** Pools are defined in `config/gpu/pools.yaml` (`gpu_classes`, `providers`, `min`, `max`, `idle_timeout_s`, `target_latency_s`, `spot_ok`, `regions`).
- Desired workers = ceil(backlog GPU-seconds for the models the pool serves ÷ target latency), clamped to min/max.
- Provision through `GPUProvider`, and stop idle workers after the timeout.
- Hold `low`-priority tasks when projected daily spend would exceed `BUDGET_DAILY_USD` or a project or video budget cap.
- Emit alerts as `notifications` and SSE events.

**GPUProvider adapters** are registered as plugins. `gpu_providers.kind` is a text key validated against the registered providers, never a DB enum.
- MVP providers: `mock` (simulated workers on CPU with configurable prices, start latency and failure injection), `local_docker` (starts worker containers on a local GPU host through the Docker API), `runpod_pod` and `runpod_serverless`.
- Later: `vast`, `skypilot`.
- Interface: `provision(spec) -> ProviderInstance`, `start`, `stop`, `terminate`, `status`, `list_offers(gpu_class, region)`, `price(instance)`, `health()`.
- Uploads and downloads go through object storage, never through provider-specific copy APIs.

**Model cache** (`ce_worker.model_cache`):
- resolves `hf://repo@revision/path` and `s3://…` into a versioned local cache, downloading once per host;
- verifies SHA-256 on first use and records it in a local manifest;
- reports cached models to the scheduler;
- uses LRU eviction, excluding pinned models;
- supports a shared network-volume path;
- `HF_TOKEN` is set only on workers that need gated models.

**Cost ledger.**
- Record provisioned seconds × the price captured at provision. Busy time is attributed to attempts and idle time to pool overhead.
- LLM token costs and storage/egress estimates also go to `cost_ledger`.
- Roll up per node, version, video, project and day.
- `estimate()` calibration: maintain p50/p90 seconds-per-unit by adapter × GPU class from completed attempts, falling back to the adapter's own estimate.

**Temporal ↔ scheduler handoff.**
- A GPU node runs as an activity that submits the task (with its Temporal task token) to the scheduler and calls `activity.raise_complete_async()`. The scheduler completes or fails it through the client's async activity handle when the worker reports.
- These activities have `maximum_attempts=1` at the Temporal level.
- Retry ownership is explicit:
  - **infrastructure failures** (lease expiry, worker crash, OOM, provider loss) are retried by the scheduler with the same seed;
  - **quality failures** are retried by the QC ladder in the build layer with attempt seeds (§12.5);
  - **Temporal** retries only CPU activities.
- Workflows pass IDs only. Each scene runs as a child workflow, and the parent owns the video-level nodes.

**OOM and capacity.** An `oom` failure retries on the next larger VRAM class in the pool config. If no larger class is allowed, it retries with the adapter's lower-memory settings (offload, lower resolution). Record which was used.

**Checkpoints.** Long talking shots are chunk nodes. Each chunk is a checkpoint, and a failure re-renders from that chunk onward (the cascade in §12.6).

**Failure recovery.**
- CPU activity retries use exponential backoff and per-node-kind timeouts; heartbeat timeouts apply.
- Artifact writes are idempotent: they are content-addressed, so re-uploading an existing hash is a no-op.
- Repeated failure follows the router's fallback chain; provider fallback applies when a provider reports no capacity.
- User cancellation propagates: workflow cancellation → the scheduler marks tasks cancelled → the worker's cancel token.
- Never lose completed artifacts. Partial versions are viewable (`partial` state).

---

## 26. Quality gate and Creative Director

Implement in `ce_qc`. Thresholds live in `config/qc/{draft,final}.yaml`, `config/qc/behavior.yaml`, `config/qc/world.yaml` and `config/qc/consistency.yaml`. **Metric thresholds are keyed by metric adapter id** (for example `qc.lipsync[syncnet_v1].min_confidence`), because scales differ between models. API fields use generic names such as `lipsync_score` and `speech_quality`.

**Node checks.** Starting values, configurable:
- **Lip sync**: the SyncNet-class metric (≥ 5 good, < 3 reject) is **advisory** until its weights license is verified and it is calibrated for non-English. For `beta` languages it only warns. AV offset is within ±1 frame at the preset fps.
- **Identity**: similarity ≥ the creator's baseline minus a margin.
- **Speech**: WER ≤ 5% for English (configurable per language); speech-quality score ≥ the configured minimum.
- **Mix**: −14 LUFS ± 1, true peak ≤ −1 dBTP, music ≥ 15 dB under speech.
- **Captions**: no overlap with platform UI safe zones.
- **Text**: no unintended text in B-roll (OCR).
- **VLM judge**: no critical defects in eyes, teeth, hands, limbs or background warping.
- **Lighting consistency**: per shot vs its keyframe and its scene neighbors, plus `world_lighting` (§19.6).
- **Pacing**: measured WPM and shot lengths vs the mode template's ranges.
- **Subtitles**: OCR on sampled rendered frames vs the expected caption text and safe zones.
- **Scene continuity**: VLM pairwise comparison of adjacent shots for outfit, world and lighting, plus color statistics.
- **Product consistency** (V1): embedding similarity between product crops and the product asset.

**Performance QA** (§16), **world continuity** (§19.6) and **creator consistency** (§20) are part of the gate with their own configs.

Mock versions of every check return seeded values with a configurable failure rate (`MOCK_QC_FAIL_RATE`).

**Decision ladder per failed node:**
1. retry with an attempt seed;
2. retry on the fallback route (skipped when there is none, and the report says so);
3. a cheaper fix (for example a lip-sync patch instead of a full re-render);
4. flag `needs_review` with the best take.

Retry budgets apply per node and per version, in both count and USD. Only failed nodes rerun, and the video always renders with flags visible.

**VLM judge.** Structured prompts with a JSON schema per check; 2–4 fps sampling plus full-resolution crops of faces and hands (from `face.landmarks`/`body.landmarks`), batched per shot. One judge model is the default. An optional cheaper triage tier escalates failures to the main judge when a triage adapter is installed.

**Report types** (`qc_reports.target_type`): `take`, `node`, `shot`, `render`, `consistency`, `creator_test`, `world_plate`.

**Creative Director critique** (`CritiqueWorkflow`, after render). Inputs: a VLM pass over the proxy (about 2 fps), the transcript, the spec, the CBS, the coverage report and the QC metrics. Output:

`Critique{scores: {hook, pacing, emotional_variation, behavior_believability, character_consistency, world_continuity, realism, visual_quality, storytelling, clarity, cta, scene_variety, audio}, findings: [{issue, evidence (time range + coverage item refs), proposed_ops: [EditOperation], impact, estimate}]}`

The critique is shown to the user and applied only at the user's request. Autonomous suggestion (V1) still requires approval.

---

## 27. Rendering, captions, audio, screen recordings

- **Timeline/EDL** (`ce_render.timeline`). Tracks: base video (talking plates and silent holds), overlay video (B-roll, screen, inserts, reaction clip), graphics (logo, titles, disclosure, webcam bubble), captions, dialogue, music, SFX, room tone. The timeline is built from the spec, the BuildManifest and the alignment, with all times resolved from anchors.
- **Compiler.** Render each shot to a mezzanine clip (post camera + realism applied), cached by key. Then concatenate the base track, composite the overlays, burn captions (ASS via libass), mix audio and encode per render preset (`config/platforms/*.yaml` → `render_presets`: resolution, fps, codec, bitrate/CRF, optional `loudness_override`). The proxy preset is 540p and fast, for previews.
- **Reaction layouts.** In `reaction` mode the reaction clip plays as picture-in-picture or a split layout. Its audio is ducked under the creator's speech.
- **Captions.**
  - Build ASS from word timings with styles (`config/caption_styles/*.yaml`: font, size, stroke, colors, active-word highlight, max words per line, animation), positioned inside platform safe zones. Also export SRT and VTT.
  - Intent policies can hide or emphasize keywords (§14).
  - Multilingual captions: same-language captions by default. Translated tracks (`captions.translations[]`) are produced by `captions.translate` with a human review state (`pending | approved`).
  - Right-to-left scripts (Arabic) are rendered with correct shaping. libass must be built with HarfBuzz and FriBidi (verified in Phase 0). RTL tracks fall back to line-level highlight instead of per-word highlight.
  - A bundled font fallback chain (for example the Noto families) covers Latin Extended (ə, ı, ş, ğ), Cyrillic and Arabic. Caption QC verifies glyph coverage, and an Arabic golden render test exists.
- **Audio mix.**
  1. Dialogue stem: the concatenated verified segments with inserted pauses, and per-scene world acoustics (`audio.room`).
  2. Music: per-cue beds, ducked by a gain envelope computed from speech activity (word timings plus padding, configurable attack and release ramps, default −18 dB under speech). The envelope is applied to the music stem before mixing; `sidechaincompress` is not used for depth control.
  3. SFX placed by anchors; the room-tone bed; mic processing.
  4. Two-pass `loudnorm` in linear mode with `TP=-1`, verified with `ebur128=peak=true`.

  **Beat-aware editing**: track music beats on the bed (librosa) and, when the mode enables it, snap cut points within a configurable tolerance to the nearest music beat, never cutting inside a word. QC re-runs ASR on the final mix and compares its WER against the dry voice.
- **Provenance.**
  1. Watermark the frames (`provenance.watermark_video`, VideoSeal adapter) and the final mix (`provenance.watermark_audio`, AudioSeal adapter) with a payload id stored in `renders`, then mux.
  2. Sign a C2PA manifest (c2pa-python) with `digitalSourceType` `trainedAlgorithmicMedia`, or `compositeWithTrainedAlgorithmicMedia` when real footage is included. The manifest lists the routes (adapters + model revisions) as ingredients/assertions and the consent ids.

  Dev uses a generated test CA and a leaf certificate with a C2PA-acceptable key usage; dev verification expects a valid signature and hashes with an untrusted root. `GET /v1/renders/{id}/verify` checks both layers.
- **Screen recordings** (`ScreenAnalysisWorkflow`). On upload: scene-change detection → keyframes → OCR with boxes (PP-OCR) → text diffs between keyframes → a VLM summary of "what happens when", stored as a `screen_analysis` artifact. The Director maps script spans to screen ranges, typed zoom rectangles (from OCR boxes or changed regions), speed segments for dead time, and webcam-bubble placement.

---

## 28. Natural-language editing

`EditOperation` is a closed union and the **only** way to mutate a spec (ADR 0025). Each operation names a target scope (SpecPath, scene, shot, character or time range resolved to anchors):

| Operation | Effect |
| --- | --- |
| `set_intent` | Change video or scene intent fields. Triggers an intent-policy replan of the affected channels (§14) |
| `set_acting` | Change the situation and any state field in scope: internal state, social and audience goals, performance intent, emotion felt/displayed and intensity, masking, confidence delta, attention target, strategies, transition, priority |
| `add_behavior_event` / `remove_behavior_event` / `set_behavior_event` | Events of any requestable dimension: `gaze`, `facial_expression`, `gesture`, `head_motion`, `posture`, `reaction` |
| `add_annotation` / `remove_annotation` / `set_annotation` | Script annotations: `pause`, `emphasis`, `nonverbal_audio`, `delivery`, `pronunciation`, `inserted_disfluency`. For example, "laugh slightly here" = a `small_laugh` event + a `nonverbal_audio` laugh annotation |
| `set_world_binding` | World version, camera position, time of day, weather, `continuity_ref` |
| `set_world_override` | Scene-local overrides: element states, hide, add, move, lighting, acoustics. Never mutates World DNA |
| `set_wardrobe` | Per-scene wardrobe version |
| `set_cast` | Per-video appearance or voice override, `voice_prosody`, role |
| `set_camera` | Profile, framing, angle, moves, world camera position |
| `set_pacing` | `scenes[].pacing` (target WPM delta, cut cadence) and bulk pause adjustments |
| `edit_script` | Text edits with anchor rebase (refused when wording is locked) |
| `add_scene` / `remove_scene` / `move_scene` | Scene structure |
| `add_shot` / `remove_shot` / `split_shot` / `set_shot` | Shot structure: type, layer, span, B-roll prompt, reaction source, screen zooms and speed segments |
| `set_meta` | Quality tier (draft → final re-routes the affected nodes through the new routing profile), platform targets, aspect, target duration, title |
| `set_render_outputs` | Output presets and reframe policy |
| `replace_broll`, `set_music`, `set_sfx`, `set_captions`, `set_effects`, `set_brand`, `set_product`, `set_provenance_label` | As named |
| `regenerate` | Seed override and/or component re-run (§12.7) |
| `reroute` | Explicitly re-route nodes (§12.4) |
| `select_take` | Choose a take |
| `set_lock` | Add or remove locks |
| `refresh_memory` | Pin new MemorySnapshots for some or all of the cast |
| `memory_feedback` | Proposes a Creator Memory item (a side effect outside the spec; confirmed separately, §18.4) |

**Pipeline** (`ProposeEditWorkflow`, asynchronous):
1. Resolve references ("the first 3 seconds", "second scene", "her", "when he looks away") to anchors and keys, using the alignment map and the editor selection.
2. The LLM emits operations (JSON schema, closed vocabularies).
3. Code translates them into a SpecPatch.
4. Validate schema, locks, policy and vocabulary.
5. Run `diff_graph` for the impact: regenerate, keep, cascade, `no_visible_effect`, route changes, estimate.
6. Compute the **predicted coverage delta** (re-resolve the CBS and re-compile for the affected scenes).
7. Offer alternative strategies: editorial-only, lip-sync patch, full re-performance.
8. Store the `edit_proposal` and emit `edit.proposed`.

On apply (`ApplyEditWorkflow`), a new version is created and only dirty nodes are built.

**Example.** "Make him more skeptical" with the second scene selected becomes:
- `set_acting` on that scene's states: displayed emotion moves toward `skeptical` (+0.2 intensity within DNA bounds), prosody strategy `slow_measured`, gaze strategy `side_glance`;
- `add_behavior_event`: an `eyebrow_raise` at the claim word.

The proposal shows the expected coverage. With a global-prompt engine and voice unlocked, the change is mostly delivered through prosody (APPROXIMATED via `prosody_transfer`) plus an editorial punch-in, and the eyebrow raise is UNSUPPORTED.

Regenerate, take-selection and lock endpoints are thin wrappers that create single-operation proposals and auto-apply them after validation.

Test fixtures cover every edit example in section 2 with their expected operation types and dirty sets (§37).

---

## 29. Database schema

PostgreSQL 18 with pgvector.

**Conventions:**
- UUIDv7 primary keys: use `uuidv7()` as the default where available, otherwise generate them in Python.
- **Every tenant-owned table, including child tables, has `org_id`** (indexed). Where practical, add a composite foreign key `(org_id, parent_id)`. Repository methods require an org context (I12).
- `created_at` and `updated_at` everywhere.
- JSONB columns are validated by Pydantic before write.
- **Append-only** tables, with no UPDATE or DELETE grants for the app role: `audit_logs`, `cost_ledger`, `creator_usage_events`, `behavior_observations`, `build_manifest_entries`.
- **Global (platform) tables** have no `org_id` and are writable only by platform admins: `plugins`, `models`, `model_benchmarks`, `benchmark_pairs`, `model_behavior_profiles`, `gpu_providers`, `gpu_workers`, `worker_enrollment_tokens`, `feature_flags`, and global `protected_persons` rows.
- Approved versioned identity rows are immutable: `creator_versions`, `appearance_versions`, `voice_versions`, `wardrobe_versions`, `world_versions`, `product_versions`. Enforce this in repositories and with a trigger that rejects updates when `status = 'approved'`, except for setting `status = 'archived'`.
- `video_versions` is **not** covered by that trigger, because it has a `state`, not a `status`. Its identity columns (`spec`, `spec_hash`, `parent_version_id`, `origin`) are protected by a dedicated trigger; its status columns stay mutable (§12.8).

**Tenancy and auth**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `organizations` | id, name, plan, settings jsonb | — |
| `users` | id, email (unique), name, password_hash, oidc_sub, is_active, **is_platform_admin**, last_login_at | — |
| `memberships` | user_id, org_id, role (`owner, admin, editor, viewer, developer`) | PK(user_id, org_id) |
| `invitations` | id, org_id, email, role, token_hash, expires_at, accepted_at, created_by | — |
| `sessions` | id, user_id, org_id, token_hash, expires_at, ip, user_agent, revoked_at | httpOnly session store |
| `api_keys` | id, org_id, user_id, prefix, hash, scopes text[], expires_at, last_used_at, revoked_at | — |
| `idempotency_keys` | org_id, key, request_hash, status, response jsonb, expires_at | PK(org_id, key); TTL from config |
| `operator_profiles` | org_id (PK), jurisdiction, regions_served text[], revenue_band, mau_band, offers_model_as_service bool, license_confirmations jsonb, updated_by | 1:1 org |

**Projects, videos, versions, projections**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `projects` | id, org_id, name, description, default_creator_id, brand_kit_id, budget_usd, settings jsonb, archived_at | — |
| `videos` | id, org_id, project_id, title, mode, current_version_id, status (`active, archived`), budget_usd | `title` and `mode` are projections of the current version's `meta` |
| `video_versions` | id, org_id, video_id, number, parent_version_id, branch, spec jsonb, spec_hash, spec_content_digest, planned_routes jsonb, plan_report_artifact_id, state (§12.8), origin (`plan, replan, edit, regenerate, reroute, restore, branch, lock_change, take_select, duplicate, variant, remix`), coverage_summary jsonb, flags text[] (`coverage_changed`, `needs_world_approval`, `approximations_stale`), cost_estimate_usd, cost_actual_usd, frozen_at, created_by | unique(video_id, number); self-FK; identity columns immutable |
| `build_manifest_entries` | org_id, version_id, node_key, route jsonb, effective_seed, artifact_id, config_digests jsonb, impl_version, created_at | PK(version_id, node_key); insert-only (§12.3) |
| `scenes` | id, org_id, version_id, scene_key, order, purpose, world_version_id, camera_position_key, time_of_day, weather, start_s, end_s, status | projection; unique(version_id, scene_key) |
| `scene_cast` | org_id, version_id, scene_key, character_key, creator_version_id, appearance_version_id, voice_version_id, wardrobe_version_id | projection for continuity queries ("which videos used world X / outfit Y") |
| `shots` | id, org_id, version_id, scene_key, shot_key, type, layer, start_s, end_s, status, selected_take_key, qc_status | projection |
| `takes` | id, org_id, version_id, shot_key, take_key, take_index, effective_seed, artifact_ids uuid[] (chunks in order), observed_behavior_artifact_id, behavior_signature jsonb, qc_report_id, rank, selected | unique(version_id, shot_key, take_key); `selected` is a projection of the spec |
| `edit_proposals` | id, org_id, version_id, job_id, instruction, selection jsonb, ops jsonb, patch jsonb, impact jsonb, coverage_delta jsonb, alternatives jsonb, status (`proposing, proposed, applied, rejected, superseded, failed`), result_version_id | — |
| `director_runs` | id, org_id, version_id, stage, template_version, provider, model, input jsonb, output jsonb, tokens_in, tokens_out, latency_ms, cost_usd, status | — |
| `critiques` | id, org_id, version_id, render_id, job_id, scores jsonb, findings jsonb | — |
| `packaging` | id, org_id, version_id, platform, title, description, hashtags text[], cta_text, thumbnail_artifact_ids uuid[], status (`draft, approved`) | Director stage 12 |
| `renders` | id, org_id, version_id, preset_id, aspect, is_proxy, artifact_id, c2pa_manifest jsonb, watermark_payload_id, consent_ids uuid[], provenance_mode (`real, mock_dev`), qc_report_id, status | — |
| `exports` | id, org_id, render_id, packaging_id, platform, disclosure_checklist jsonb, created_by | — |
| `captions` | id, org_id, version_id, language, style_id, format (`ass, srt, vtt`), artifact_id, review_state (`n/a, pending, approved`) | — |

**Creators and identity**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `creators` | id, org_id, name, kind (`synthetic, digital_twin`), current_version_id, consent_id, status | root identity; the API's `creator_id` |
| `creator_versions` | id, org_id, creator_id, number, parent_version_id, dna jsonb (CreatorDNA), appearance_version_id, voice_version_id, default_world_ids uuid[], default_wardrobe_version_ids uuid[], status, approved_at, created_by | immutable when approved |
| `appearances` | id, org_id, creator_id, name, current_version_id | — |
| `appearance_versions` | id, org_id, appearance_id, number, parent_version_id, dna jsonb, canonical_face_asset_id, identity_pack jsonb (candidates, asset ids, similarity scores, approvals), lora_artifacts jsonb, age_checks jsonb (VLM estimate, owner attestation), status | — |
| `voices` | id, org_id, creator_id (nullable for org presets), name, kind (`designed, cloned, preset`), current_version_id, consent_id | — |
| `voice_versions` | id, org_id, voice_id, number, parent_version_id, references jsonb (per language: asset_id, transcript), description, wpm jsonb, lexicon jsonb, default_prosody jsonb, status | — |
| `voice_candidates` | id, org_id, voice_id, job_id, artifact_id, description, engine, selected | output of `VoiceDesignWorkflow` |
| `voice_conditioning` | org_id, voice_version_id, adapter_id, revision, artifact_id | per-engine cached conditioning (`voice.prepare`) |
| `wardrobes` | id, org_id, creator_id, name, current_version_id | — |
| `wardrobe_versions` | id, org_id, wardrobe_id, number, parent_version_id, spec jsonb, reference_asset_ids uuid[], status | — |
| `creator_tests` | id, org_id, creator_version_id, appearance_version_id, voice_version_id, world_version_id, job_id, render_artifact_id, scorecard jsonb, coverage_report jsonb, human_rating, same_person_rating, created_by | — |
| `creator_baselines` | id, org_id, creator_version_id, source (`creator_test, rolling`), stats jsonb, window_n, updated_at | — |
| `consistency_reports` | id, org_id, creator_id, creator_version_id, version_id, metrics jsonb, verdict, deviations jsonb | — |

**Creator Memory**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `creator_memory_items` | §18.2 fields, with `embedding vector(EMBEDDING_DIM)` | HNSW index on embedding; partial unique(creator_id, kind, key, value_hash) where status in (`proposed, active`), so conflicting values can coexist (§18.7) |
| `memory_snapshots` | id, org_id, creator_version_id, created_for_version_id, items jsonb (ids + copied values), params jsonb, conflicts jsonb, digest | immutable |
| `creator_usage_events` | id, org_id, creator_id, video_id, version_id, character_key, event (`ready, exported`), hooks jsonb, hook_embedding vector(EMBEDDING_DIM), phrase_fingerprints jsonb, arc_signature jsonb, behavior_signatures jsonb, visual_signature jsonb, world_version_ids uuid[], wardrobe_version_ids uuid[] | append-only |

**Worlds and products**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `worlds` | id, org_id, name, kind, owner_creator_id (nullable), current_version_id, status | — |
| `world_versions` | id, org_id, world_id, number, parent_version_id, dna jsonb (WorldDNA), plate_candidates jsonb (drafts only), plates jsonb `{camera_position_key: {time_of_day: {weather: asset_id}}}`, fingerprints_artifact_id, status, approved_at | immutable when approved; the only store of approved plates |
| `products` | id, org_id, name, current_version_id | — |
| `product_versions` | id, org_id, product_id, number, description, asset_ids uuid[], brand_kit_id, claims_allowed jsonb, status | — |

**Behavior, QC, ratings**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `behavior_observations` | id, org_id, version_id, take_id (null for viewer-level rows), level_scope (`take, viewer`), item_ref, character_key, dimension, requested jsonb, level, method, approximation_executed, verdict, outcome, measures jsonb, confidence, adapter_id, translator_version, model_revision, language, created_at | append-only; source for profiles |
| `model_behavior_profiles` | id, model_id, adapter_id, translator_version, revision, dimension, language, source (`mock, bench, production`), declared jsonb, measured jsonb (success_rate, n, ci), knob_calibration jsonb, updated_at | global |
| `qc_reports` | id, org_id, version_id, target_type (§26), target_id, checks jsonb, verdict (`pass, warn, fail`), thresholds_digest | — |
| `human_ratings` | id, org_id, target_type (`take, render, creator_test, consistency, benchmark_pair`), target_id, question_key, rating jsonb, rater_user_id | — |

**Assets, artifacts, jobs**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `assets` | id, org_id, project_id null, kind (`image, video, audio, screen_recording, logo, product, music, sfx, font, document, reference`), storage_key, mime, bytes, sha256, probe jsonb, rights jsonb (owner, license, consent_id), status (`uploading, ready, rejected`), tags text[], created_by | — |
| `artifacts` | id, org_id, kind (§12.2 vocabulary), storage_key, mime, bytes, sha256, media jsonb, produced_by_node_id, qc_state (`unchecked, accepted, qc_rejected`) | storage deduplicated by sha256 |
| `cache_entries` | org_id, cache_key, artifact_id, effective_seed, created_at | PK(org_id, cache_key); points only to non-rejected artifacts |
| `artifact_refs` | org_id, artifact_id, ref_type, ref_id | GC (§12.10) |
| `generation_jobs` | id, org_id, kind (§9 table), status (`queued, running, succeeded, failed, cancelled, partial`), priority, target_type, target_id, video_version_id null, temporal_workflow_id, parent_job_id, input jsonb, progress, cost_estimate_usd, cost_actual_usd, error jsonb, requested_by | hierarchical |
| `execution_nodes` | id, org_id, job_id, version_id, node_key, node_kind, scene_key, shot_key, chunk_index, take_index, cache_key, route jsonb, effective_seed, status (`pending, cached, queued, running, succeeded, failed, skipped, needs_review, cancelled`), artifact_ids uuid[], qc_status, attempts | — |
| `job_attempts` | id, org_id, node_id, attempt_no, reason (`initial, infra_retry, qc_retry, fallback`), worker_id, adapter_id, model_id, model_revision, translator_version, started_at, ended_at, gpu_seconds, cost_usd, status, error_class, error_message, metrics jsonb, logs_key, seed | — |
| `gpu_tasks` | §25 | 1:1 GPU attempt |
| `notifications` | id, org_id, user_id null, kind (`budget_alert, qc_flag, job_done, memory_conflict, worker_state, consistency_warning`), payload jsonb, read_at | also emitted on SSE |

**Research, templates, brand, consent, safety**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `research_sources` | id, org_id, project_id, kind (`url, pdf, doc, note, transcript, video`), uri, asset_id, title, content_hash, trust (`user_provided, web`), fetched_at, status | — |
| `research_facts` | id, org_id, source_id, text, quote_span jsonb, embedding vector(EMBEDDING_DIM), entities jsonb | HNSW index |
| `claims` | id, org_id, video_id, claim_key, first_version_id, text, verdict (`supported, unsupported, uncertain, conflicting`), evidence_fact_ids uuid[], confidence, override_by, override_reason | unique(video_id, claim_key): stable across versions |
| `spec_templates` | id, org_id, kind (`video, scene, creator_style, camera, caption, brand`), name, body jsonb (partial spec), composes_from uuid[] | composition: deep-merge the bodies in `composes_from` order, then the template itself. Later wins for scalars and objects. Lists are replaced unless the field declares `merge: append`. Conflicts are reported in the apply preview |
| `brand_kits` | id, org_id, name, logo_asset_id, colors jsonb, fonts jsonb, caption_style_id, v1 jsonb (intro/outro, music and SFX libraries, rules — V1, rejected by validators while the feature flag is off) | — |
| `consents` | id, org_id, subject_name, scope (`face, voice, both`), consent_media_asset_id, phrase, statement_text, permitted_uses jsonb, jurisdictions text[], face_match_score, voice_match_score, verified_at, expires_at, revoked_at | referenced by creators, voices, renders |
| `protected_persons` | id, org_id (null = global), name, kind (`public_figure, political, minor_protection`), reference_embeddings jsonb | blocklist (§32) |
| `retention_policies` | org_id, data_kind, retain_days | data lifecycle |

**Models, plugins, GPU, cost, flags, audit**

| Table | Columns (essential) | Notes |
| --- | --- | --- |
| `plugins` | id, plugin_key, version, kind, manifest jsonb, status, validation, runtime_family, enabled | global |
| `models` | id, plugin_id, model_key, display_name, capabilities text[], source_uri, revision, checksums jsonb, size_gb, vram_min_gb, vram_rec_gb, languages jsonb, license jsonb, dependencies jsonb (each with a license), obligations jsonb, allowed_envs text[], status, validation, promotion_basis (`smoke, bench`), quality_scores jsonb | global |
| `model_benchmarks` | id, model_id, eval_set_version, metrics jsonb, behavior_profile jsonb, human_scores jsonb, verdict | global |
| `benchmark_pairs` | id, benchmark_id, item_key, a_artifact_id, b_artifact_id | votes stored in `human_ratings` |
| `gpu_providers` | id, kind (text, validated against registered GPUProvider plugins), name, credentials_ref, regions text[], enabled, budget_daily_usd | global |
| `gpu_workers` | id, provider_id, external_id, runtime_family, gpu_type, gpu_count, vram_gb, region, price_per_hour_usd, state (`provisioning, idle, busy, draining, stopped, failed`), resident_models text[], cached_models text[], token_hash, last_heartbeat_at, started_at, stopped_at | global |
| `worker_enrollment_tokens` | id, provider_id, runtime_family, token_hash, expires_at, used_at, created_by | global |
| `cost_ledger` | id, org_id, project_id, video_id, version_id, node_id, worker_id, kind (`gpu, llm, storage, egress`), quantity, unit, unit_price_usd, amount_usd, created_at | append-only |
| `feature_flags` | key (PK), description, enabled, org_overridable bool, rules jsonb, updated_by | global; safety flags (e.g. `digital_twins_enabled`) are not org-overridable |
| `org_feature_flags` | org_id, key, enabled | per-org overrides |
| `audit_logs` | id, org_id, actor_user_id, actor_kind, action, target_type, target_id, before jsonb, after jsonb, ip, user_agent, created_at | append-only |

Provide:
- Alembic migrations;
- seed scripts (`ce seed dev`);
- repository-level tests that attempt cross-tenant reads and writes on **every** tenant table, all of which must fail;
- immutability tests for approved versioned rows.

---

## 30. API

**Conventions:**
- `/v1` prefix; JSON; OpenAPI published at `/openapi.json`.
- Errors are RFC 9457 `application/problem+json` with stable `type` URIs.
- Cursor pagination (`?cursor=&limit=`).
- An `Idempotency-Key` header is required on POSTs that start work; keys are stored in `idempotency_keys`.
- Long or model-dependent operations return `202 Accepted` with `{job_id, …}`. **The `api` service never calls an LLM or a GPU synchronously.**
- SSE at `GET /v1/events?scope=org|project&project_id=`, resumable via `Last-Event-ID` and backed by Redis Streams. Event types: `job.updated`, `node.updated`, `version.updated`, `previz.ready`, `edit.proposed`, `render.ready`, `qc.flagged`, `coverage.updated`, `memory.proposed`, `memory.conflict`, `budget.alert`, `worker.updated`, `notification`.
- One id name per resource (`{creator_id}`, `{version_id}`, …).
- Every endpoint has request and response Pydantic models, with examples in OpenAPI.

**Endpoints:**

- **Auth and org**
  - `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/me`
  - `POST /v1/api-keys`, `DELETE /v1/api-keys/{key_id}`
  - `GET /v1/members`, `PATCH /v1/members/{user_id}` (role), `DELETE /v1/members/{user_id}`
  - `POST /v1/invitations`, `POST /v1/invitations/{token}:accept`
- **Projects**
  - `POST /v1/projects`, `GET /v1/projects`
  - `GET|PATCH|DELETE /v1/projects/{project_id}`
  - `GET /v1/projects/{project_id}/videos`
- **Videos and planning**
  - `POST /v1/projects/{project_id}/videos` → `202 {video_id, version_id, job_id}`. The body is `{input, input_mode?: auto|idea|structured|exact_script|brain_dump, cast?: [{creator_id, role?, voice_version_id?, wardrobe_version_id?}], world_id?, mode?, platform_targets?, primary_aspect?, target_duration_s?, language?, quality_tier?, style?: {camera_profile_id?, caption_style_id?, music_mood?}, sources?: [source_id], sources_policy?, budget_usd?, advanced?: AdvancedOptions}`. These fields become Director constraints. `AdvancedOptions` is a typed model: takes, routing profile, strategy pack, intent hints, acting hints.
  - `GET /v1/videos/{video_id}`
  - `GET /v1/versions/{version_id}` (spec, state, flags, estimate, claims summary)
  - `GET /v1/versions/{version_id}/previz` (the `plan_report`: durations and drift, predicted coverage, memory used, repetition and contradiction reports, world proposals, cost)
  - `GET /v1/versions/{version_id}/manifest` (the assembled BuildManifest)
  - `POST /v1/versions/{version_id}:replan` `{scope?: scene_keys, instruction?, refresh_memory?: bool}` → job (the previz "Regenerate plan"). It reuses the pinned memory snapshots unless `refresh_memory` is true
  - `POST /v1/versions/{version_id}:approve` → generation job
- **Intent, behavior, coverage**
  - `GET /v1/versions/{version_id}/intent` (video and scene intent with `derived_from` trace)
  - `GET /v1/versions/{version_id}/behavior?scene_key=` (CBS, CompiledBehavior and coverage per scene)
  - `GET /v1/versions/{version_id}/coverage` (requested/compiled/observed report)
  - `GET /v1/takes/{take_id}/observations`
- **Generation**
  - `POST /v1/versions/{version_id}:resume` re-runs failed and cancelled nodes of a `partial`, `failed` or `cancelled` version **in place**. It is allowed only for those states, and only completed nodes' artifacts are added to the BuildManifest.
  - `POST /v1/versions/{version_id}/scenes/{scene_key}:regenerate` and `…/shots/{shot_key}:regenerate` take `{components: [...edit_vocabulary components], seed_policy: same|new, takes?, quality_tier?, strategy?: full|lipsync_patch}` and return `202 {job_id, new_version_id, estimate}`. Locked components are refused, with the lock group named.
  - `POST /v1/versions/{version_id}:reroute` `{node_keys, reason}` → `202`
  - `POST /v1/versions/{version_id}/shots/{shot_key}/takes/{take_key}:select` → `202` (new version)
- **Edits and locks**
  - `POST /v1/versions/{version_id}/edits` `{instruction, selection?: {time_range_s?, scene_keys?, shot_keys?, character_keys?}}` → `202 {edit_proposal_id, job_id}`; the result arrives as `edit.proposed`
  - `GET /v1/edits/{edit_proposal_id}`
  - `POST /v1/edits/{edit_proposal_id}:apply` → `202 {job_id, new_version_id}`
  - `POST /v1/edits/{edit_proposal_id}:reject`
  - `PUT /v1/versions/{version_id}/locks` `{locks: [{group, scope}]}` → `202` (new version)
- **Versions**
  - `GET /v1/videos/{video_id}/versions`
  - `POST /v1/versions/{version_id}:branch` `{name}`
  - `POST /v1/versions/{version_id}:restore` (new version, becomes current)
  - `POST /v1/videos/{video_id}:duplicate` `{version_id, project_id?}`
  - `GET /v1/videos/{video_id}/compare?a=&b=` (spec, intent, CBS and coverage diffs + render refs)
  - `POST /v1/versions/{version_id}:variants` → `501` with a roadmap link (V1)
  - `POST /v1/versions/{version_id}:remix` `{transform: shorten|lengthen|translate|retarget_creator|retarget_platform}` → `501` (V1)
- **Creators**
  - `POST /v1/creators` `{name, kind: synthetic, dna}`, `GET /v1/creators`, `GET /v1/creators/{creator_id}`
  - `POST /v1/creators/{creator_id}/versions` `{from_version_id?, dna?, appearance_version_id?, voice_version_id?, defaults?}` → draft
  - `GET|PATCH /v1/creator-versions/{creator_version_id}` (PATCH on drafts only)
  - `POST /v1/creator-versions/{creator_version_id}:approve`
  - `POST /v1/creator-versions/{creator_version_id}/tests` → job; `GET /v1/creator-tests/{creator_test_id}`; `POST /v1/creator-tests/{creator_test_id}/ratings`
  - `GET /v1/creators/{creator_id}/baselines`
  - `GET /v1/creators/{creator_id}/consistency?since=`
- **Appearances**
  - `POST /v1/creators/{creator_id}/appearances`, `GET /v1/appearances/{appearance_id}`
  - `POST /v1/appearances/{appearance_id}/versions` → draft
  - `POST /v1/appearance-versions/{appearance_version_id}/identity-pack:generate` `{candidates: 12}` → job
  - `POST …/identity-pack:choose` `{asset_id}`; `POST …/identity-pack:review` `{approve: [], reject: []}` (drafts only)
  - `POST /v1/appearance-versions/{appearance_version_id}:approve` (verifies that the recorded age checks and identity scores exist and passed)
- **Wardrobes**
  - `POST /v1/creators/{creator_id}/wardrobes`, `GET /v1/creators/{creator_id}/wardrobes`
  - `POST /v1/wardrobes/{wardrobe_id}/versions` → draft (+ reference generation job)
  - `POST /v1/wardrobe-versions/{wardrobe_version_id}:approve`
- **Voices**
  - `POST /v1/voices` `{name, kind: designed, description, language, creator_id?}` → `202` (`VoiceDesignWorkflow`)
  - `GET /v1/voices`, `GET /v1/voices/{voice_id}`
  - `GET /v1/voices/{voice_id}/candidates` (from `voice_candidates`); `POST /v1/voices/{voice_id}/candidates/{candidate_id}:select` → draft voice version
  - `GET|PATCH /v1/voice-versions/{voice_version_id}` (PATCH on drafts: lexicon, description, prosody defaults)
  - `POST /v1/voice-versions/{voice_version_id}:test` `{text, tags?, language?, engine_hint?}` → `202 {job_id}`; the result holds `{audio_artifact_id, wer, speech_quality, wpm, speaker_similarity}`
  - `POST /v1/voice-versions/{voice_version_id}:approve`
- **Worlds**
  - `POST /v1/worlds` `{name, kind, dna}`, `GET /v1/worlds`, `GET /v1/worlds/{world_id}`
  - `POST /v1/worlds/{world_id}/versions` `{from_version_id?, patch?}` → draft (also used to promote scene overrides)
  - `GET|PATCH /v1/world-versions/{world_version_id}` (drafts)
  - `POST /v1/world-versions/{world_version_id}/plates:generate` `{camera_position_keys?, times_of_day?, weather?}` → job; `POST …/plates:choose` `{camera_position_key, time_of_day, weather, asset_id}` → records the choice and enqueues fingerprinting (job)
  - `POST /v1/world-versions/{world_version_id}:approve` (verifies that the required plates and fingerprints exist)
  - `GET /v1/world-versions/{world_version_id}/diff?against=`
  - `GET /v1/worlds/{world_id}/continuity` (usage across videos with world QC scores)
- **Products**
  - `POST /v1/products`, `GET /v1/products`
  - `POST /v1/products/{product_id}/versions`, `POST /v1/product-versions/{product_version_id}:approve`
- **Creator Memory**
  - `GET /v1/creators/{creator_id}/memory?category=&kind=&status=&q=`
  - `POST /v1/creators/{creator_id}/memory` (authored item)
  - `PATCH /v1/memory-items/{memory_item_id}` with an action from:
    - `{action: pin|unpin|forget|activate|dismiss}`;
    - `{action: supersede, by}`;
    - `{action: resolve_conflict, keep}`;
    - `{action: edit, value, text}` (edit is for authored items only).
  - `GET /v1/memory-items/{memory_item_id}/history`
  - `DELETE /v1/memory-items/{memory_item_id}` → `202` (`DeletionWorkflow`, leaves a tombstone)
  - `GET /v1/creators/{creator_id}/usage?limit=`
  - `GET /v1/versions/{version_id}/memory-snapshots` (one per cast member)
- **Consent** (schema and endpoints exist in the MVP; cloning and twin flows stay disabled by feature flag until V1)
  - `POST /v1/consents:start` → `{consent_id, phrase, statement_text, upload_urls}`
  - `POST /v1/consents/{consent_id}:submit`, `POST /v1/consents/{consent_id}:revoke`
  - `GET /v1/consents`, `GET /v1/consents/{consent_id}`
- **Assets**
  - `POST /v1/assets:initiate-upload` `{filename, mime, bytes, kind, project_id?}` → `{asset_id, upload: {multipart presigned parts}}`
  - `POST /v1/assets/{asset_id}:complete` → validation job
  - `GET /v1/assets`, `GET /v1/assets/{asset_id}`, `DELETE /v1/assets/{asset_id}`
  - `GET /v1/assets/{asset_id}/screen-analysis`
- **Research**
  - `POST /v1/projects/{project_id}/sources` `{kind, uri|asset_id}` → ingest job; `GET /v1/projects/{project_id}/sources`
  - `GET /v1/versions/{version_id}/claims`; `POST /v1/claims/{claim_id}:override` `{reason}`
- **Rendering, packaging, export**
  - `POST /v1/versions/{version_id}/renders` `{preset_ids: [], proxy?: bool}` → job
  - `GET /v1/versions/{version_id}/renders`, `GET /v1/renders/{render_id}`
  - `GET /v1/renders/{render_id}/download` → presigned URL; `GET /v1/renders/{render_id}/verify`
  - `GET /v1/versions/{version_id}/captions`, `GET /v1/captions/{caption_id}/download`
  - `POST /v1/versions/{version_id}/captions:translate` `{language}` → `202` (a `set_captions` operation adding the language, auto-applied; the new version builds a `captions.translate` node); `POST /v1/captions/{caption_id}:approve`
  - `POST /v1/versions/{version_id}:package` `{platforms}` → job; `GET /v1/versions/{version_id}/packaging`; `PATCH /v1/packaging/{packaging_id}`
  - `POST /v1/renders/{render_id}/exports` `{packaging_id, platform, disclosure_checklist}`
- **Critique**
  - `POST /v1/versions/{version_id}:critique` → job; `GET /v1/versions/{version_id}/critiques`
  - Applying a finding goes through `POST /v1/versions/{version_id}/edits` with `{from_critique_finding: {critique_id, finding_index}}`.
- **Templates and brand**
  - `POST /v1/spec-templates` `{from_version_id, kind, paths?}`, `GET /v1/spec-templates`
  - `POST /v1/spec-templates/{spec_template_id}:apply` `{version_id}` → edit proposal
  - `POST /v1/spec-templates:compose` `{template_ids}` → preview
  - `GET|POST /v1/brand-kits`, `PATCH /v1/brand-kits/{brand_kit_id}`
- **Estimates**: `POST /v1/estimates` `{version_id?, edit_proposal_id?, regenerate?: {...}}`. This is computed synchronously from cost tables and calibration; it makes no model calls.
- **Jobs**
  - `GET /v1/jobs?status=&kind=`
  - `GET /v1/jobs/{job_id}` (nodes, attempts, routes, costs)
  - `POST /v1/jobs/{job_id}:cancel`
  - `GET /v1/jobs/{job_id}/logs` (SSE)
- **Observability for users**
  - `GET /v1/cost?group_by=day|project|video&since=`
  - `GET /v1/versions/{version_id}/director-runs`
  - `GET /v1/notifications`, `POST /v1/notifications/{notification_id}:read`
- **Ratings**: `POST /v1/ratings` `{target_type, target_id, question_key, rating}`; `GET /v1/rating-queue?kind=same_person|in_character|benchmark_pair`
- **Settings**
  - `GET|PUT /v1/settings/operator-profile`
  - `GET /v1/feature-flags` (effective values for the org); `PUT /v1/feature-flags/{key}` (org override, only when `org_overridable`)
  - `GET /v1/audit-logs`
- **Models and GPU, read** (org members): `GET /v1/models`, `GET /v1/models/{model_id}`, `GET /v1/gpu/pools`, `GET /v1/gpu/workers`, `GET /v1/gpu/offers?gpu_class=&region=`
- **Platform admin** (`/v1/admin/*`, platform admins only)
  - `POST /v1/admin/models` (register from a manifest; entry-point discovery has precedence for installed plugins)
  - `POST /v1/admin/models/{model_id}:benchmark|promote|disable|calibrate`
  - `POST /v1/admin/plugins/{plugin_id}:enable|disable`
  - `GET|POST /v1/admin/gpu/providers`, `PATCH /v1/admin/gpu/providers/{provider_id}`
  - `POST /v1/admin/gpu/workers:provision` `{provider_id, gpu_class, runtime_family, count}`, `POST /v1/admin/gpu/workers/{worker_id}:stop`
  - `POST /v1/admin/gpu/workers:enroll` `{provider_id, runtime_family}` → a one-time enrollment token for a self-managed GPU host
  - `GET|PUT /v1/admin/feature-flags/{key}`
  - `GET /v1/admin/gpu/queue` (live `gpu_tasks`)
- **Internal worker API** (served by `scheduler`; per-worker tokens): `POST /internal/v1/worker/register|lease|heartbeat|complete|fail`.

Generate the TypeScript client from OpenAPI in CI, and fail the build if it is stale.

---

## 31. Frontend

Next.js 16 App Router, TypeScript strict, Tailwind + shadcn/ui, TanStack Query for server state, Zustand for editor state, EventSource for SSE. The UI is desktop-first (≥ 1280 px), with keyboard shortcuts in the studios, and accessible (WCAG 2.1 AA contrast, focus states, labels). No business rules live in the frontend beyond display logic.

**Navigation**: Dashboard, Create, Projects, Creators, Worlds, Assets, Templates, Models, GPU, Jobs, Settings, Developer.

**Progressive disclosure.** Every studio has a default **Simple** view and an **Advanced** toggle, remembered per user. The Simple view uses plain language and natural-language edits, so a user can type "make him more skeptical". The Advanced view exposes the structured fields. **Developer** details (CBS JSON, compiled directives, routes, seeds) live in a Developer drawer.

- **Dashboard**: recent videos with states, running jobs, spend today vs budget, fleet status, QC and consistency flags needing review, unresolved memory conflicts.
- **Create** is a stepper:
  1. Idea/Script: auto-detects the mode, shows the detected mode with an override; the exact-script toggle shows "wording locked".
  2. Creator (cast).
  3. World (the creator's defaults are preselected).
  4. Format: mode, aspect, platform.
  5. Style: camera profile, caption style, music mood.
  6. Voice.
  7. Duration.
  8. Language, with production, beta and unsupported badges.
  9. Advanced: quality tier, takes, sources and closed-book, budget cap, intent and acting hints.
  10. Plan, then **Previz review**:
      - the script with annotation chips;
      - storyboard keyframes per shot with world plates;
      - the **Intent summary** and **Performance Timeline preview** (estimated seconds);
      - **predicted coverage** (honored / approximated / unsupported, with reasons);
      - memory used, repetition and contradiction reports, fact-check flags;
      - measured vs target duration, cost and time estimate;
      - actions: Approve / Edit / Regenerate plan.
- **Video Studio** (per video):
  - **Player** (proxy video + DOM caption overlay preview).
  - **Timeline** with tracks: base, overlay, captions, dialogue, music, SFX. It also has a **Performance lane** (acting states as colored bands with emotion labels and transition markers, plus event markers) and a music-beat lane. Drag on it to select ranges.
  - **Scene list** with status, QC, coverage and world badges.
  - **NL edit panel**: an instruction becomes a proposal card with operations, diff, impact (regenerate/keep/cascade/no visible effect), predicted coverage delta, alternatives and cost; then Apply.
  - **Versions panel**: a tree with branches; restore; compare with synchronized side-by-side players plus spec, intent, CBS and coverage diffs.
  - **Export dialog**: packaging fields, presets, aspect reframing preview, disclosure checklist, provenance status.
  - **Panels** (Simple → Advanced):

    | Panel | Simple view | Advanced view |
    | --- | --- | --- |
    | Intent | per-scene cards ("Goal: challenge a belief; effect: curiosity") | intent fields with vocabulary dropdowns and `derived_from` links |
    | Performance | the trajectory in plain words ("confident → realizes → serious") | per-state emotion felt/displayed, intensity, gaze, gesture, posture, prosody, attention target, camera awareness, transitions; events editor |
    | Behavior coverage | a badge per scene ("4 honored, 6 approximated, 2 unsupported") | the requested → compiled → observed table per item, with methods and confidence |
    | Observed / QC | flags with plain explanations and suggested fixes | metric values, thresholds, take ranking, analyzer details |
    | Creator & World | the bound creator, wardrobe and world with thumbnails (read-only) | versions, overrides, continuity refs |

  - **Shot inspector**: takes gallery with QC and observation scores and selection; lock toggles; camera profile and moves; world camera position; route info; compiled directives (read-only, Developer drawer).
- **Creator Studio** (per creator). The "Creator Studio" in the product brief spans three connected views: this per-creator studio (Creator DNA, Creator Memory, Worlds, Behavior history); World Studio (World DNA editing); and the Video Studio panels (Director Intent, Performance Timeline, Behavior Coverage, Observed Behavior/QC) for a specific video. Each links to the others. Tabs:
  - **Overview**.
  - **Creator DNA** editor with tabs: Identity & Canon, Personality, Speech, Behavior, Gesture & Posture, Gaze, Camera, Fashion, Worlds, Editing, Avoidances. Edits always happen on a draft version, and approval is explicit.
  - **Creator Memory**: lists by category with source, confidence, recency and status; pin / unpin / forget / supersede / resolve conflict; proposed items to confirm or dismiss; provenance links to videos.
  - **Appearance**: the identity pack gallery (candidates → canonical → expansions with similarity scores → approve or reject).
  - **Voice**: design candidates, test bench (type text with tags; hear the engine output; see WER, WPM and speaker similarity), lexicon editor.
  - **Wardrobe**.
  - **Creator Test**: runner, scorecard (including requested vs observed), history and ratings.
  - **Behavior history**: coverage and observation outcomes across this creator's videos, habit evidence behind memory items, and measured behavior by engine.
  - **Worlds**: the creator's default and recently used worlds, linking to World Studio.
  - **Consistency**: metric charts across videos, deviations, the human rating queue.
  - **Consent** status.
- **World Studio** (Worlds page): world list and the World DNA editor:
  - elements table with positions on a 2D floor-plan view;
  - camera positions with permitted/forbidden status and allowed camera profiles;
  - lighting and time-of-day presets, acoustics;
  - the plates gallery per camera position × time of day (generate, choose, approve);
  - versions with diffs;
  - the continuity report (usage across videos with world QC scores).
- **Assets**: upload (multipart with progress), library with filters, rights metadata, screen-recording analysis view.
- **Templates**: save from a version (video, scene, creator style, camera, caption, brand), apply (as an edit proposal), compose.
- **Models**: registry with capabilities, languages, license block including dependencies (shown prominently), status and validation, behavior matrix (declared vs measured), benchmarks, sandbox runs and the pairwise rating UI. Promote and disable are platform-admin only.
- **GPU**: providers, pools, workers (VRAM, resident models, price, state), offers, budgets, cost charts. Mutations are platform-admin only.
- **Jobs**: list and detail (a graph of nodes with states, attempts, logs, routes with reasons, costs).
- **Settings**: organization, members, roles and invitations; API keys; operator profile; feature flags (org overrides); brand kits; retention.
- **Developer**: live queue, node inspector (seeds, params, translated prompts, model revisions), director runs (prompt + output), raw spec / CBS / BuildManifest viewers, cost ledger, error explorer.

Coverage and observation badges must follow invariant I9: a behavior is shown as delivered only when its outcome is `*_CONFIRMED`.

Use Playwright for e2e flows in mock mode.

---

## 32. Safety, rights, provenance, license policy

Implement in `ce_policy`. Rules and lists live in `config/policy/`, and every decision is logged to `audit_logs`.

- **Creator kinds** are enforced end to end (§17.3). Synthetic creators must present as adults (three-part check). Digital twins require a valid consent and are disabled by the platform flag `digital_twins_enabled=false` until V1, regardless of how complete the consent subsystem is.
- **Cloning detection.** Any uploaded face or voice used as an appearance or voice reference counts as cloning. It requires consent, and is therefore blocked until V1.
- **Blocklists.** Public figures, political candidates and officials (realistic depictions blocked by default), and minors. Name and likeness checks run at planning (the Director's output). When configured, similarity checks run against `protected_persons` embeddings.
- **Testimonial guard.** In UGC, testimonial and product modes with synthetic creators, the guard detects first-person experiential claims about products ("I've used this for a month") using rules plus `llm.structured` classification. Such a claim then requires one of two things:
  - a real customer quote with a consent reference (segment field `quote_source {consent_id, asset_id}`);
  - a rewrite as non-experiential, or a dramatization with disclosure. `testimonial_dramatization` mode adds an on-screen `disclosure` effect.

  Exact-script mode cannot bypass the guard.
- **License policy engine**: `evaluate(license_closure, operator_profile, request_context) -> allow | deny(reason) | allow_with_obligations(obligations)`. The closure is the model plus every dependency. The rules are:
  - any `commercial_use: false` → deny outside the sandbox;
  - `territories_excluded` ∩ (operator jurisdiction ∪ regions served) → deny;
  - revenue or MAU caps vs the operator's bands → deny, unless the operator profile records a license confirmation;
  - `maas_restricted` while the operator offers model-as-a-service → deny.

  Obligations (attribution strings and their placement) are rendered automatically in the UI, the docs and the export metadata. **`enabled=false` on a plugin is a product decision independent of `evaluate()`**: LTX ships disabled even for operators the license allows.
- **Provenance** (ADR 0013, §27):
  - In `prod`, invisible watermarks and C2PA are always on. Production refuses to start if `PROVENANCE_MODE` is not `real`.
  - In `dev`/`test`, `PROVENANCE_MODE=mock_dev` is allowed until Phase 7 makes the real layers available. It burns a visible "MOCK PROVENANCE — NOT FOR DISTRIBUTION" label, and such renders are recorded as `provenance_mode=mock_dev` and cannot be exported.
  - The visible AI label defaults to `auto`: on for EU audiences or regions, or when the operator profile says so.
- **Export checklist** per platform (`config/platforms/*.yaml`): YouTube altered/synthetic content disclosure, TikTok AI-generated label (and the AI tag for ads), Meta AI info. It also warns about mass-produced templated output.
- **Data lifecycle.**
  - Retention settings (`retention_policies`) cover consent media, voice and face references, and Creator Memory.
  - `DeletionWorkflow` removes references and blocks dependent generation. Memory deletion leaves tombstones (§18.9).
  - Consent media is encrypted at rest with envelope encryption, behind a KMS-compatible key-provider abstraction (with a local dev key).
- **Share links and takedown** are V1. The public share-link feature, the unauthenticated report form and the 48-hour takedown workflow are built together.

---

## 33. Security

- **Accounts and sessions**: argon2id password hashing; httpOnly, Secure, SameSite=Lax session cookies backed by the `sessions` table; CSRF protection for cookie-authenticated mutations; hashed API keys with scopes and prefixes.
- **Authorization**: RBAC by org role, plus the **platform-admin** role for the global resources (models, plugins, GPU providers and workers, enrollment, global feature flags). Org scoping is mandatory in repositories and covered by tests.
- **Workers and storage**: per-worker tokens (hashed in the DB, rotatable) and one-time enrollment tokens; presigned URLs with a short TTL and exact keys.
- **Uploads**: size limits, magic-byte sniffing, ffprobe in a subprocess with timeouts, rejection of unexpected containers.
- **SSRF guard** for all URL fetching, from Phase 4 on:
  - block private, loopback, link-local and metadata ranges;
  - resolve, then check the address, then connect;
  - size and time limits.
- **Prompt-injection hygiene**: sources, uploads, Creator Memory text and model outputs are wrapped as data. Director instructions never come from them (I10). Fixtures with injected instructions must not change Director behavior.
- **Operations**:
  - rate limiting (Redis);
  - secrets only from the environment or a secret manager;
  - dependency audit in CI (`pip-audit`, `pnpm audit`);
  - security headers on web;
  - audit logging for consent, cloning attempts, exports, model promotion, policy overrides, role changes, memory forget and delete, persona-contradiction overrides, and out-of-character approvals.

---

## 34. Observability

- **Logs**: structlog JSON with `trace_id`, `span_id`, `org_id`, `job_id`, `node_id`, `attempt_id`, `worker_id`.
- **Tracing**: OpenTelemetry across web → api → Temporal → orchestrator activities → scheduler → worker. Propagate the context in lease payloads.
- **Prometheus metrics**:
  - queue depth by pool and priority, and lease wait time;
  - node duration by adapter × GPU class, model load time, cold-start time;
  - cache hit rate;
  - QC pass rate by adapter × language, and retries;
  - **coverage outcome rates by adapter × dimension**, `NOT_MEASURABLE` rate by analyzer;
  - world QC pass rate, consistency out-of-band rate per creator;
  - memory conflicts open;
  - cost per output minute, spend vs budget;
  - API latency and error rates.
- **GPU hosts**: the DCGM exporter.
- **Logs per attempt**: uploaded as artifacts and streamed to the UI.
- **Dashboards and alert rules** are kept as code in `infra/observability/` (compose profile `observability`, internal only).

---

## 35. Configuration and environment variables

**Layered config**: `config/default.yaml` → `config/env/<APP_ENV>.yaml` → environment variables (pydantic-settings, names exactly as listed below) → DB runtime settings (feature flags, operator profile).
- `ce config validate` checks every YAML, including the vocabularies, intent policies, edit vocabulary and observation proxies, against its schema, at startup and in CI.
- `ce_config` computes a `config_digest` per file for cache keys (§12.2).
- There are no magic numbers in code for anything that may change: QC thresholds, prices, budgets, model limits, timeouts, platform rules, routing weights, memory budgets and decay, repetition thresholds, consistency bands. Each lives in config with a schema and a default.

Environment variables are listed below. Document all of them in `docs/ENVIRONMENT_VARIABLES.md` and `.env.example`.

`.env.example` must be **dev-safe**: copying it must give a working `make dev`. Settings whose correct value depends on the environment are left empty in it, and resolve from `config/env/<APP_ENV>.yaml`:
- `PROVENANCE_MODE`: `mock_dev` in dev until Phase 7, then `real`; always `real` in prod;
- `COOKIE_SECURE`: false in dev and test, true in prod.

Production startup checks enforce the prod values.

```
APP_ENV=dev|test|prod
LOG_LEVEL=info
SECRET_KEY=
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
TEMPORAL_ADDRESS=temporal:7233
TEMPORAL_NAMESPACE=default
S3_ENDPOINT_URL=http://seaweedfs:8333
S3_REGION=us-east-1
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_BUCKET_ASSETS=ce-assets
S3_BUCKET_ARTIFACTS=ce-artifacts
PUBLIC_BASE_URL=http://localhost:3000
API_BASE_URL=http://localhost:8000
SCHEDULER_PUBLIC_URL=http://localhost:8100
MOCK_GPU=true                   # registers the mock GPU provider and mock adapters (run on worker-cpu)
MOCK_QC_FAIL_RATE=0.0
CPU_REAL_ENGINES=auto           # auto = use real CPU engines when `make fetch-cpu-assets` has run: cpu_model (Kokoro, faster-whisper, CTC aligner, DNSMOS) on worker-cpu and cpu_inproc analyzers (MediaPipe, PP-OCR, prosody, AuraFace, image embedding) in-process; mocks otherwise
LLM_PROVIDER=fixture            # plugin key under plugins/providers/llm: fixture | anthropic | openai_compatible; fixture is the dev/test default
LLM_MODEL=                      # set in config/env, never in code
ANTHROPIC_API_KEY=
OPENAI_COMPAT_BASE_URL=
OPENAI_COMPAT_API_KEY=
EMBEDDING_MODEL=                # model registry key of the embed.text adapter
EMBEDDING_DIM=1024
HF_TOKEN=
MODEL_CACHE_DIR=/models
RUNPOD_API_KEY=
VAST_API_KEY=                   # V1 provider
WORKER_TOKEN=                   # for a statically configured worker in dev
BUDGET_DAILY_USD=20
SMOKE_SPEND_CAP_USD=5           # hard cap for any paid GPU smoke test
OPERATOR_JURISDICTION=EU        # conservative default
OPERATOR_REVENUE_BAND=lt_1m
PROVENANCE_MODE=                # real | mock_dev; empty = from config/env/<APP_ENV>.yaml. mock_dev only in dev/test; prod refuses to start otherwise
C2PA_SIGNING_CERT_PATH=
C2PA_SIGNING_KEY_PATH=
VISIBLE_LABEL_DEFAULT=auto      # auto | on | off — default for new videos; the visible label only
KMS_PROVIDER=local              # plugin key under plugins/providers/kms
KMS_LOCAL_KEY_PATH=
COOKIE_SECURE=                  # empty = from config/env/<APP_ENV>.yaml (false in dev/test, true in prod)
IDEMPOTENCY_TTL_S=86400
SSE_STREAM_RETENTION_S=86400
ENROLLMENT_TOKEN_TTL_S=3600
FIXTURE_CLIPS_DIR=              # licensed face/behavior clips for real-analyzer tests (§16.7); tests skip if absent
OTEL_EXPORTER_OTLP_ENDPOINT=
SENTRY_DSN=                     # optional
```

---

## 36. Docker, CUDA, deployment

- **Dockerfiles** (multi-stage, non-root, pinned base digests): `api`, `orchestrator` (includes the `cpu_inproc` plugins), `scheduler`, `render-worker` (FFmpeg with libass built with HarfBuzz and FriBidi, plus the `cpu_inproc` plugins), `web`, `worker-cpu` (the `cpu_model` family: mock adapters and CPU engines), and `worker-<family>` for `image`, `wan`, `tts`, `asr`, `audio`, `vllm`, `post`, `lipsync`, `vision`.
- **GPU bases**: a CUDA 12.8 runtime for Ampere, Ada and Hopper; `-blackwell` tags on CUDA 13 for NVFP4 paths.
  - Each family pins PyTorch and an attention backend (FA3 on Hopper, SageAttention on Ada/Blackwell when validated, SDPA fallback).
  - B200 (sm_100) is validated separately from RTX 5090 / PRO 6000 (sm_120).
  - Host requirements: NVIDIA driver ≥ 570 for CUDA 12.8 images and ≥ 580 for CUDA 13 images, plus nvidia-container-toolkit. The fleet manager filters provider offers by driver version.
- **Weights** are not baked into general images; workers use the model cache (network volume or local disk). Single-model serverless images may bake weights, with an ADR.
- **FFmpeg distribution**: follow ADR 0017. Images ship license notices, and a corresponding-source offer when the GPL build is used.
- **Compose** (`infra/compose/docker-compose.yml`) profiles, with healthchecks on every service:
  - `core`: postgres, redis, temporal, temporal-ui, seaweedfs, api, orchestrator, scheduler, render-worker, web;
  - `mock-gpu`: worker-cpu loading the mock adapters;
  - `cpu-real`: worker-cpu also loading the CPU engines (in-process analyzers follow `CPU_REAL_ENGINES` in every profile);
  - `observability`: otel-collector, prometheus, grafana, loki, tempo.
- **Makefile targets**: `bootstrap`, `dev`, `infra-up`, `infra-down`, `migrate`, `seed`, `fetch-cpu-assets`, `test`, `test-unit`, `test-invariants`, `test-behavior`, `test-api`, `test-workflows`, `test-render`, `test-e2e`, `e2e-mock`, `lint`, `typecheck`, `gen-client`, `gen-schema`, `build-images`, `smoke-gpu PLUGIN=…`, `bench PLUGIN=…`, `calibrate PLUGIN=…`.
- **Deployment guides**:
  - single node: one CPU VM with Compose + Caddy TLS; GPU workers on RunPod or a local GPU host, dialing out;
  - scale-out outline: Kubernetes/Helm for the control plane, Temporal Cloud or a self-hosted cluster, R2, the SkyPilot provider.
- **CI** (GitHub Actions): lint, typecheck, unit, invariants, behavior suites, API, workflow tests, render golden tests, client freshness, e2e mock (compose), image builds for CPU images. GPU smoke tests are manual workflows that require a GPU runner.

---

## 37. Testing and mock mode

**Mock mode** (`MOCK_GPU=true`, `LLM_PROVIDER=fixture`): everything runs on a laptop, and mock adapters produce **real media** with FFmpeg and Pillow.
- **Image mock**: a PNG with a simple drawn face-like figure, the creator name, wardrobe, world and camera-position labels, and seeded colors.
- **World plate mock**: labeled room-like gradients with element labels at their floor-plan positions.
- **Avatar mocks** (`mock_avatar_global`, `mock_avatar_segment`): loop the keyframe and overlay an audio-driven waveform at the mouth area. They burn in the shot key, the acting-state label and the take number. The duration equals the audio. Both write `behavior_track.json` according to their behavior matrix and the compiled directives, with configurable failure injection.
- **B-roll mock**: a labeled moving gradient for the requested duration.
- **TTS mock**: tone or noise bursts with the estimated duration per word, honoring inserted pauses, plus synthetic word timings. When CPU engines are available, real Kokoro is used instead.
- **ASR/align mock**: returns the script with synthetic timings; faster-whisper on CPU when available.
- **Music and SFX mocks**: generated tones or noise with envelopes.
- **Observer mock**: reads `behavior_track.json`, adding noise. VLM, QC and consistency mocks return seeded scores with a configurable failure rate.
- **LLM fixture provider**: replays JSON responses keyed by `stage + scenario_id`, not by prompt hash, so template edits don't break fixtures. Fixtures are either `recorded` (captured from a real provider when a key is present) or `authored` (hand-written and labeled as such).
- **Free-form input in dev**: for inputs that match no scenario, a labeled, deterministic **template Director** per mode builds a valid plan from the input text (sentence-split script, mode-template scenes, default intent and acting). This keeps `make dev` usable with arbitrary ideas. Its plans are marked `planner: template`.

**Test layers:**
- **Unit**: domain; tokenizer; timeline math (hypothesis); anchor resolution and rebase; SpecPath parsing and globbing; cache keys and config digests; router scoring and pinning; scheduler scoring; policy rules; tag parser and translators (golden files); normalizers; intent policies; memory retrieval and merge; contradiction checker; repetition guard.
- **Invariants** (`tests/invariants/`): one or more tests per invariant I1–I14, each added in the phase that builds what it protects. I2 (projection rebuild) and I12 come in Phase 1; I5, I11 and I14 in Phase 2; I1 and I4 in Phase 3; I7, I10 and I13 in Phase 4; I9 in Phase 5; I3 for identity rows in Phase 1 and for video versions in Phase 6; I6 for scene overrides and I8 in Phase 6; I6 for World Studio promotion in Phase 10.
- **Schema**: JSON Schema snapshots and round-trips for the VideoSpec, CBS, CompiledBehavior, ObservedBehavior, BehaviorCoverageReport, PlanReport, BuildManifest, CreatorDNA, WorldDNA and MemoryItem; vocabulary validation and lint; migrations up and down.
- **API**: httpx, auth, tenancy, idempotency, problem+json, async-only rules (no LLM or GPU in `api`).
- **Workflow**: Temporal test environment; cancellation; retries; partial failure; resume.
- **Adapter contract suite**: shared tests that every adapter must pass, run against the mocks always and against real adapters when available. It includes translator conformance: every abstract directive is either translated or reported unsupported.
- **Render golden**: ffprobe (duration, streams, fps, resolution); ebur128 loudness and true peak; caption presence via OCR on frames; Arabic RTL caption render; C2PA signature and hashes valid (Phase 7+).
- **Failure injection**: worker crash mid-task, lease expiry, provider with no capacity, LLM returning invalid JSON, storage outage, QC failures, behavior-observation failures.
- **e2e** (Compose + Playwright): create → previz → approve → generate → play → NL edit → compare → export.

**Behavior, world and memory suites** (required; the phase is in brackets):

| Suite | What it proves |
| --- | --- |
| World DNA schema validation [1] | Valid and invalid worlds; element, zone and camera-position references; tolerances |
| World versioning [1, 10] | Drafts editable, approved immutable, promotion of overrides creates a new version, diffs |
| Environment lock tests [6] | `world` lock blocks `set_world_binding`, `set_world_override` and `background` regenerate in scope; allows them out of scope |
| Director Intent schema [1, 4] | Vocabulary validation; intent policies deterministic; `derived_from` present |
| Situational acting compilation [3] | Each acting-chain link compiles into CBS fields and then into methods for both mock matrices |
| Emotional trajectory [3, 4] | States tile; transitions need triggers; masking; the seconds-based request maps to states with reported drift |
| CanonicalBehaviorSpec [3] | Schema; deterministic resolution; resolution order (§10.6); digest stability; no engine fields (lint) |
| Adapter behavior translation [3, 8] | Golden translations per translator; unsupported items reported, never dropped |
| Requested vs compiled vs observed [3, 7, 11] | Mock tracks with injected failures yield the right per-take verdicts and viewer-level outcomes (all 12); editorial items are judged on `approximation_executed`; retry targets follow the method table; real CPU analyzers run on fixture clips |
| Model swap (I1) [3] | `build_graph()`/`compile()` run twice on one version with two router configurations, persisting nothing: identical spec content, CBS content and DNA digests; only compiled outputs, coverage and routes differ; coverage of the segment engine ≥ the global engine; approximation elements are flagged for re-proposal |
| Coverage upgrade [3] | Enabling `mock_avatar_segment` upgrades coverage without any Director change |
| Creator Memory retrieval [4, 12] | Filters, budgets, pinned-first, forgotten excluded, version scope, recency decay, snapshot pinning (I7) |
| Memory contradictions [4, 12] | Script vs canon, memory vs memory, DNA vs memory; blocking only for pinned or canon items |
| Repetition guard [4, 12] | Hooks, phrases, arcs, visual patterns, signature-phrase exemptions |
| Dirty analysis: behavior-only changes [6] | Exact dirty sets with voice unlocked vs locked; `no_visible_effect` when the compiled digest is unchanged |
| Dirty analysis: World DNA changes [6] | The exact sets from §19.5 |
| Creator continuity [11] | Consistency metrics computed vs baselines; overrides listed as deviations; human-rating queue |
| Environment continuity [11] | `world_identity`, `world_elements`, `world_lighting`, background continuity within and across videos (mock and CPU) |
| Acceptance: human behavior examples [4] | Every request in §15.5 and the trajectory example in §15.4 produces the expected acting structure (situation, states, masking, triggers, events), CBS items and predicted coverage for both mock matrices |

**Acceptance fixtures (plans).** Every creation example in section 2 must produce a valid plan in fixture mode. When an LLM key is present, CI also runs them against the real provider, recording new fixtures and reporting differences. The assertions cover:
- the detected input mode;
- exact-script byte equality where relevant;
- scene, intent, acting and world structure;
- coverage report entries.

**Acceptance fixtures (edits).** Every edit example in section 2 maps to its expected operation types and dirty sets.

**GPU validation.** Each GPU plugin has:
- `scripts/smoke/<plugin>.py`: download pinned weights, run a minimal job, write outputs, print timings and VRAM;
- `scripts/bench/<plugin>.py`: the golden set, including behavior fixtures;
- `scripts/calibrate/<plugin>.py`: knob calibration.

Results go to `docs/GPU_VALIDATION.md` with the date, GPU type, driver, CUDA, timings, peak VRAM, measured behavior profile and status.

---

## 38. Documentation deliverables (keep current)

- **Root files**: `README.md` (what it is, quick start in mock mode), `TODO.md`, `ROADMAP.md`, `CHANGELOG.md`.
- **This prompt**: `docs/MASTER_BUILD_PROMPT.md`.
- **Architecture and setup**:
  - `docs/ARCHITECTURE.md` (services, layer stack, data flow, Mermaid diagrams);
  - `docs/INVARIANTS.md` (I1–I14 with their tests);
  - `docs/SETUP.md`, `docs/ENVIRONMENT.md` (from Phase 0), `docs/ENVIRONMENT_VARIABLES.md`.
- **Models and GPUs**:
  - `docs/GPU_SETUP.md` (local GPU host, RunPod, drivers, CUDA, images);
  - `docs/MODEL_INSTALLATION.md` (model cache, pinned revisions, HF tokens, license verification steps including dependencies);
  - `docs/MODELS.md` (registry, licenses, statuses, validation, declared vs measured behavior matrices);
  - `docs/GPU_VALIDATION.md`.
- **Extending the system**:
  - `docs/API.md` (generated from OpenAPI + guides);
  - `docs/PLUGINS.md` (manifest reference, writing a plugin and a BehaviorTranslator);
  - `docs/ADAPTERS.md` (interfaces, contract tests).
- **Domain documentation**:
  - `docs/VIDEOSPEC.md` (schema reference, anchors, SpecPaths, locks, resolution order);
  - `docs/BEHAVIOR.md` (acting chain, vocabularies, CBS, compiler methods, coverage, observation, honesty limits);
  - `docs/INTENT.md` (intent vocabulary, intent policies, traceability);
  - `docs/CREATORS.md` (Creator DNA, appearance, voice, wardrobe, Creator Test);
  - `docs/MEMORY.md` (kinds, write paths, retrieval, snapshots, repetition, contradictions);
  - `docs/WORLDS.md` (World DNA, plates, overrides, continuity QC);
  - `docs/CONSISTENCY.md`;
  - `docs/VOCABULARIES.md` (how to extend the closed vocabularies safely);
  - `docs/QC.md`;
  - `docs/POLICY_AND_COMPLIANCE.md`.
- **Operations**: `docs/TROUBLESHOOTING.md`, `docs/DEPLOYMENT.md`, `docs/DEVELOPMENT.md` (workflow, testing, conventions, rule-20 checklist).
- **Decisions and progress**: `docs/adr/*`, `docs/DECISIONS.md`, `docs/progress/phase-N.md`.

---

## 39. MVP scope, extension points, roadmap, default model map

### 39.1 Architectural foundations that must exist and work in the MVP (real, tested)

- The VideoSpec, including intent, acting, world bindings, cast and SpecPaths.
- Immutable versions, the BuildManifest, route and seed pinning, the content-addressed build graph, dirty analysis with cascade and `no_visible_effect`.
- Word anchors with rebase.
- The previz gate.
- NL editing through `EditOperation`, the lock system, the impact preview.
- The Director with all stages, the intent policy engine, the situational acting model, closed vocabularies.
- Creator DNA, appearance, voice and wardrobe versions; Creator Memory (store, snapshots, retrieval, write paths, repetition guard, contradiction checker); World DNA with versions, plates, overrides and continuity QC.
- The CanonicalBehaviorSpec, the behavior compiler, behavior translators, coverage, and the requested/compiled/observed triad, with mock and CPU analyzers.
- Creator consistency reports.
- The plugin system, capability matrices, the router, the scheduler, the model cache, cost controls.
- The QC gate; the deterministic realism layer; camera and acoustic profiles; captions.
- Creative Director critique, screen-recording analysis and world continuity QC as **pipelines**. Their VLM judgments are mocked until a `vllm` worker is validated (39.2, 39.3).
- Provenance and C2PA, the license policy engine with dependency closure, mock mode, the test suites.

### 39.2 Capabilities that run as mocks or fixtures where the real thing is unavailable

- **GPU engines.** Every GPU engine has a mock; the mock avatar engines carry behavior tracks.
- **The Director LLM.** Fixture replay is used in dev and test.
- **The VLM judge.** It is mocked when no `vllm` worker exists.
- **Audio emotion.** It runs in the sandbox only.
- **Expression editing (`post.expression`).** It is a labeled pass-through until V2.
- **Identity training.** It is a mock until V1.
- **Provenance.** `mock_dev` is allowed in dev and test until Phase 7.

### 39.3 Capabilities that need real GPU validation before anyone may claim them

These capabilities are real adapters with declared matrices. Each one carries `validation: untested_on_gpu` until it has been smoke-tested and benchmarked:
- talking-head quality, lip sync and identity stability per language;
- every declared behavior control (emotion via text, segment control, knobs);
- B-roll quality;
- TTS expressiveness and verbatim accuracy per language;
- tr/ar alignment precision;
- VLM judge agreement with human ratings;
- world-plate consistency across times of day;
- cost per output minute.

### 39.4 MVP product scope

Modes, camera profiles and languages carry a design maturity. What users see is capped by route validation (§10.4), so nothing is advertised as production until its engines are smoke-promoted.

**Internal milestones:**
- **M1, after Phase 7**: the complete mock + CPU product. Every flow works end to end, with mocks only where a GPU is needed.
- **M2, after Phase 9**: the first GPU-validated video on smoke-promoted routes. Only behavior validated at M2 may back a production mode.

**In:**
- **Input**: input modes idea, structured, exact script and brain dump.
- **Modes**: the production modes and the beta modes of §13.
- **Templates and packaging**: spec templates (save, apply, compose); per-platform packaging (titles, descriptions, hashtags, thumbnails).
- **Director**: the full Director with strategy packs, mode templates, Director Intent and situational acting; fact checks against user sources and closed-book mode.
- **Creators**:
  - synthetic creators with Creator DNA, appearance identity pack, wardrobes, designed voices, Creator Memory and the Creator Test;
  - worlds with World DNA, plates, overrides and continuity QC.
- **Voice and language**:
  - acting tags, exact-script verification, lexicon, WPM calibration;
  - English at production grade;
  - de, es, fr, it, ru, tr and ar at beta (TTS available, lip sync unvalidated, tr/ar alignment via the CTC aligner or coarse);
  - Azerbaijani shown as unsupported.
- **Shots**:
  - talking head (default InfiniteTalk; LongCat-Video-Avatar 1.5 as the production fallback once smoke-validated);
  - B-roll (Wan 2.2 via LightX2V distilled);
  - user assets and screen segments with zooms; takes;
  - the performance timeline, behavior compiler and coverage with observation; the deterministic camera and realism post; mic and world acoustics.
- **Editing**: regeneration by scene, shot or component; locks; NL edits with impact and coverage preview; versions (branch, restore, compare).
- **Quality**: the QC gate with Performance QA, world continuity and creator consistency; Creative Director critique; draft and final tiers.
- **Output**:
  - captions; music (ACE-Step 1.5) and SFX (MOSS-SoundEffect) with ducking and loudness;
  - 9:16 primary, with reframing to 16:9, 1:1 and 4:5; platform render presets.
- **Policy and provenance**: C2PA, VideoSeal and AudioSeal; the visible label option, testimonial guard, blocklists, license policy engine, audit log.
- **Infrastructure**:
  - GPU providers mock, local Docker and RunPod (pods + serverless);
  - a scheduler with affinity, priorities, idle shutdown and budget caps; the model cache; cost estimates and actuals;
  - the model registry, plugins, and the sandbox with golden set and promotion.
- **UI**: the pages listed in §31.

**Out of the MVP** (interfaces and mocks are kept; these are not built yet):
- digital twins and voice cloning;
- multi-character scenes;
- walking and car vlogs;
- products held in hand;
- LoRA training;
- A/B variants and content remix;
- full brand kits beyond logo, colors, fonts and caption style;
- Azerbaijani speech;
- autonomous suggestion;
- share links and takedown;
- real-time preview;
- a public API marketplace;
- mobile layouts;
- expression post-editing;
- segment-control avatar engines in production.

### 39.5 Extension points that exist from the start (stub + mock + test, with a named phase)

| Extension point | Stub | Phase |
| --- | --- | --- |
| Multi-character | `cast[]` with several entries; `speaker_key` per segment; optional `overlap_with_previous_ms` on segments (validated; the mixer honors it in mock); `character_key` on every acting state, event, CBS item, observation and `behavior_observations` row; the CBS `content.cast` list; shot type `two_shot` in the enum (rejected while `multi_character_enabled=false`); the behavior matrix `multi_person` dimension | 1, 2, 3 |
| Variants | `POST …:variants` → 501; origin `variant` | 1 (enum), 6 (stub) |
| Remix | `POST …:remix` → 501; origin `remix` | 6 |
| Autonomous suggestion | `AutonomousSuggestWorkflow` behind `autonomous_suggest_enabled=false`; produces critique-style proposals that need approval | 11 |
| Digital twins and cloning | consents tables, endpoints, policy checks; flag `digital_twins_enabled=false` | 1, 10, 13 |
| LoRA training | job kind `lora_train`, `IdentityTrainer` interface + mock | 2 |
| Expression post-edit and pose-guided gestures | compiler methods `post_expression`, `pose_guided`; `ExpressionEditor` mock pass-through; node `post.expression` | 3 |
| Segment-control avatar engines | `mock_avatar_segment` proves the upgrade path; real engines arrive as sandbox plugins | 3 |
| OIDC | `AuthProvider` interface with a password implementation; OIDC adapter V1 | 1 |
| More GPU providers (Vast.ai, SkyPilot, Modal) | plugins only | 9 |
| Share links and takedown | feature flag `share_links_enabled=false`; no tables until V1 | V1 |

### 39.6 Roadmap after the MVP (populate `ROADMAP.md`)

**V1:**
- digital twins with verified consent, and consent-gated voice cloning;
- Azerbaijani TTS (fine-tune or licensed engine), and a per-language lip-sync bake-off for tr, ru, ar and az;
- per-creator LoRAs (image + Wan 2.2) to raise identity and world fidelity;
- hook, CTA and caption variants; content remix;
- walking and car vlogs (image-to-video + lip-sync patch);
- product shots by compositing and reference-conditioned generation;
- Wan VACE outfit and background edits;
- full brand kits and a shared spec-template library;
- Vast.ai and SkyPilot providers;
- autonomous suggest-then-approve;
- share links with takedown;
- the OIDC adapter.

**V2:**
- two-person podcast and interview (shot/reverse-shot first, then true two-shots);
- gesture-guided shots from a licensed motion library (pose-conditioned engines);
- an expression keyframe pass (blink, glance, smile) with a MediaPipe-based LivePortrait-style engine, which makes `post_expression` HONORED;
- segment-level emotion and action engines (SoulX-LiveAct- and AptAvatar-class) promoted after sandbox validation of their measured behavior profiles;
- listening and idle behavior engines for silent reactions;
- long-form 5–20 minutes with drift management;
- joint audio-video engines for reactions and ambient B-roll;
- collaborative editing; a public API and SDK; a plugin marketplace; real-time preview.

**Experimental (sandbox only, never promised):**
- products held in hand with legible labels;
- overlapping speech and interruptions;
- frame-precise micro-expressions;
- natural walking in long takes;
- 4K talking heads;
- EU-licensable video-synced foley;
- commercial co-speech gesture generation;
- learned "creator behavior models" trained on accepted takes.

**Promotion gates:**
- **MVP → V1**: the golden set passes; cost per clip is measured; mock-mode CI is green; coverage and observation reports exist for every golden item.
- **V1 → V2**: the consent flow has been audited; per-language bake-offs pass; LoRAs measurably lift identity and world scores.
- **Experimental → roadmap**: a sandboxed engine meets QC thresholds, measured-behavior targets and human-rating parity on the golden set.

### 39.7 Default model map

These defaults are config only. Verify every license, including dependencies, at the pinned revision before enabling anything. The "Built" column gives the phase that builds the adapter; "roadmap" means not built in the MVP.

| Capability | Default | Alternates | Built |
| --- | --- | --- | --- |
| Director LLM | hosted model via `LLMProvider` (structured outputs) | self-hosted Apache/MIT LLM via vLLM/SGLang | 4 |
| Text embeddings | BGE-M3 or multilingual-E5-large [RV license] | — | 12 (keyword retrieval before) |
| Keyframes, portraits, world plates | Z-Image-Turbo (Apache-2.0) | Qwen-Image-2512 (Apache-2.0) — roadmap | 8 |
| Identity pack, wardrobe and world edits | Qwen-Image-Edit-2511 (Apache-2.0) | FLUX.2 klein 4B (Apache-2.0 [RV]) — roadmap | 8 |
| Talking head | InfiniteTalk (Apache-2.0; dependencies Wan2.1-I2V-14B Apache-2.0, chinese-wav2vec2-base MIT) with a 4-step distillation LoRA and fp8 [RV: availability and quality] | LongCat-Video-Avatar 1.5 (MIT [RV]) — built, production fallback after smoke; EchoMimicV3 Flash (Apache-2.0) — roadmap; SoulX-LiveAct / AptAvatar (Apache-2.0 [RV]) — V2 sandbox | 8 |
| B-roll | Wan 2.2 A14B via LightX2V 4-step (Apache-2.0) [RV] | Wan 2.2 TI2V-5B — built (draft/fallback); LTX-2.5 — license-gated, disabled, roadmap | 8 |
| Lip-sync patch | MuseTalk 1.5 (MIT) | X-Dub (Apache-2.0 [RV]) — roadmap; LatentSync 1.6 only after the license conflict is resolved | 8 |
| TTS English | Chatterbox / Chatterbox-Turbo (MIT) | Qwen3-TTS 1.7B (Apache-2.0); Kokoro (CPU dev only, see §5) | 8 (Kokoro 7) |
| TTS de/es/fr/it/ru | Qwen3-TTS 1.7B [RV: per-language quality] | VoxCPM2 (Apache-2.0) [RV] | 8 |
| TTS tr/ar | Chatterbox Multilingual V3 (MIT) [RV: tr/ar coverage] | MOSS-TTS (Apache-2.0) — roadmap | 8 |
| Voice design | VoxCPM2 | Qwen3-TTS VoiceDesign | 8 |
| ASR verification | Qwen3-ASR (Apache-2.0) | faster-whisper large-v3-turbo (MIT) | 8 (faster-whisper CPU 7) |
| Alignment en/de/es/fr/it/ru | Qwen3-ForcedAligner (Apache-2.0) | faster-whisper word timestamps (coarse) | 8 |
| Alignment tr/ar | `ctc_aligner` over Omnilingual ASR CTC [RV license and coverage] | faster-whisper word timestamps (coarse) | 8 (coarse 7) |
| Music | ACE-Step 1.5 (MIT), instrumental mode [RV] | user library | 8 |
| SFX / room tone | MOSS-SoundEffect v2.0 (Apache-2.0) [RV: room-tone suitability] | user library | 8 |
| VLM judge and screen understanding | Qwen3.6-35B-A3B (about 24 GB at 4-bit [RV]) or Qwen3.8-27B (about 48–80 GB [RV]) (Apache-2.0 [RV per checkpoint]) via vLLM | triage tier: MiniCPM-V 4.6 [RV license] — roadmap; Gemma 4 [RV] — roadmap | 8 |
| OCR | PP-OCRv6 (Apache-2.0) | PaddleOCR-VL-1.6 — roadmap | 7 |
| Caption translation | `captions_llm` (through the configured LLM provider) with human review | — | 12 |
| Face detect/landmarks | MediaPipe Face Landmarker (Apache-2.0) | — | 7 |
| Body landmarks | MediaPipe Pose + Hand Landmarkers (Apache-2.0) | — | 7 |
| Prosody features | `prosody_features` (librosa, ISC) | — | 7 |
| Face identity embedding | AuraFace (Apache-2.0 [RV bundle]) | — | 7 (CPU) |
| Voice identity embedding | an Apache/MIT speaker-verification checkpoint, selected and license-verified at integration [RV] | — | 7 or 8 |
| Image/background embedding | an Apache-2.0 image embedding checkpoint (candidates DINOv2, SigLIP 2) [RV] | — | 7 |
| Audio emotion | emotion2vec+ (license unresolved [RV]) — sandbox only | — | 11 (sandbox) |
| Upscale / interpolation | SeedVR2-3B (Apache-2.0) / Practical-RIFE 4.25 (MIT) | FlashVSR v1.1 (Apache-2.0) — roadmap | 8 |
| Lip-sync metric | SyncNet (code MIT; weights license unspecified [RV]) — advisory only | — | 8 |
| VQA / speech quality | FAST-VQA or UVQ (Apache-2.0) / DNSMOS (CC-BY-4.0, attribution obligation), UTMOS (MIT) | — | 8 (DNSMOS CPU 7) |
| Watermark / provenance | VideoSeal, AudioSeal (MIT) / c2pa-python | Resemble Perth (audio) — roadmap | 7 |

**Rented fleet for the MVP:**
- An RTX PRO 6000 96 GB pool for the families `wan` and `vllm`.
- An RTX 4090/5090 pool for `image`, `tts`, `asr`, `audio`, `post`, `lipsync` and `vision`, including the GPU QC metric plugins.
- Both on RunPod with scale-to-zero, plus a local GPU host if available.
- Use multi-GPU sequence parallelism only for latency-sensitive previews: it costs more GPU-seconds per clip (about 11–26% in snapshot-era reports [RV]).

---

## 40. Implementation phases (with Definitions of Done)

Work strictly in order. Every phase ends with five things:
- all tests green, including `tests/invariants/` for the invariants that exist so far;
- `make dev` working in mock mode;
- docs updated;
- `docs/progress/phase-N.md` written;
- TODO and ROADMAP updated, and commits made.

### Phase 0 — Environment and bootstrap

**Scope:**
- Inspect the environment (rule 1). Verify that the available FFmpeg/libass has HarfBuzz and FriBidi.
- Create the repository layout (§8), the uv and pnpm workspaces, the Makefile, pre-commit (ruff, mypy, eslint, prettier), the CI skeleton, the PR template with the rule-20 checklist, and `.env.example`.
- Bring up Compose `core` infra (postgres, redis, temporal + UI, seaweedfs) with healthchecks.
- Write ADRs 0001–0027, `TODO.md`, `ROADMAP.md` and `docs/ENVIRONMENT.md`.
- If Docker or network access is unavailable, use the native fallback stack: local Postgres, `temporal server start-dev`, and the local-filesystem `StorageProvider`. Provide `make fetch-cpu-assets`. Tests that need missing assets skip with an explicit reason. Record which path ran.

**DoD:** `make bootstrap && make infra-up && make test` pass (or the native-fallback equivalents), and the services are healthy.

### Phase 1 — Domain core, vocabularies, persistence, API skeleton

**Scope:**
- **`ce_core`**:
  - the VideoSpec (intent, acting, world binding, cast, products, shot layers, SpecPath, anchors, tokenizer);
  - the CBS, CompiledBehavior, ObservedBehavior and BehaviorCoverageReport models;
  - CreatorDNA, AppearanceDNA, VoiceDNA, WardrobeSpec, WorldDNA, MemoryItem and MemorySnapshot;
  - validators; JSON Schema export and TS type generation.
- **Vocabularies**: `config/vocab/*` at `vocab_version 2026.10.1`, with a loader and validator.
- **`ce_config`**: loader, schemas and digests for every YAML in §8, including routing, rooms, mic profiles, LUTs, edit vocabulary, intent policies, memory, languages, QC files and platforms (with `verified_at`).
- **`ce_db`**: all tables in §29, an Alembic migration, org-scoped repositories, and the immutability trigger.
- **Storage and observability**: `ce_storage` (S3 and local) with contract tests; `ce_obs`.
- **Approvals in Phase 1** only check that recorded results exist (identity scores, age checks, plates, fingerprints). The checks that produce those results arrive with their workflows (mock in Phase 2, real in Phases 7–10).
- **`ce seed dev`** creates pre-approved fixtures:
  - an org, and a user who is a platform admin in dev;
  - the creator "Alex": an approved creator version with DNA and canon, an appearance version with a placeholder identity pack, and a designed-voice version with default WPM;
  - the wardrobe "grey hoodie";
  - the world "Alex's home office" v1, approved, with placeholder plates for `cam_desk_front` and `cam_side_wide`;
  - a few authored memory items.
- **`api`**:
  - an `AuthProvider` interface with the password implementation (the OIDC adapter is V1);
  - auth with the sessions table (`COOKIE_SECURE=false` in dev/test; Next.js rewrites proxy `/v1/*` so SSE is same-origin), members, projects;
  - videos and versions (read);
  - creators, worlds, wardrobes and memory CRUD (drafts and approve);
  - multipart asset upload with validation;
  - problem+json, OpenAPI, an SSE skeleton.

**DoD:**
- unit tests pass, plus schema tests (VideoSpec, CBS, World DNA and intent round trips; vocabulary validation and lint), migration up and down, API tests, tenancy tests over every tenant table, and immutability tests;
- World DNA validation and world-versioning tests (drafts editable, approved immutable) pass, and so do the I2 and I12 invariant tests;
- an upload round-trips through SeaweedFS, or through local storage in the native fallback.

### Phase 2 — Execution backbone in mock mode

**Scope:**
- **`ce_contracts`**: all interfaces including `run()`, `BehaviorTranslator`, the behavior-matrix schema, feature enums and the manifest schema with license closure.
- **Plugin loader** with manifest validation.
- **Mock plugins** for every capability on `worker-cpu`, including the two mock avatar engines with `behavior_track.json`, the mock observer and the `IdentityTrainer` mock.
- **Every node kind of §12.1 exists in the graph**, backed by a mock where no real implementation exists yet: `world.plate`, `voice.prepare`, `behavior.keyframe_state`, `behavior.observe`, `qc.world`, `behavior.coverage` and `post.expression`.
- **`ce_worker`**: register, lease long-poll, heartbeat, presigned I/O, cancellation, model-cache skeleton.
- **`scheduler`**: tasks, `SKIP LOCKED` leasing, scoring, priorities, the lease reaper, the mock GPUProvider, a basic fleet manager, the cost ledger.
- **`ce_build`**: node keys, cache keys with config digests and `impl_version`, the BuildManifest, route and seed pinning, dirty analysis.
- **Policy and routing**: `ce_policy.license.evaluate` with the operator profile; `ce_router` with hard filters (license closure, language support rule, status/validation), scoring, pinning and fallbacks.
- **`orchestrator`**: `GenerateVersionWorkflow`, `SceneBuildWorkflow` and `RenderWorkflow` from a fixture spec. Behavior nodes use a **pass-through stub** that reads a fixture CBS and fixture compiled behavior; Phase 3 replaces it.
- **`render-worker`**: real FFmpeg assembly (base and overlay tracks, ASS captions, ducked music, two-pass loudnorm, encode, proxy), with `PROVENANCE_MODE=mock_dev` and the burned label. SSE progress events.

**DoD:**
- `make e2e-mock` produces a playable MP4 with captions and music from a fixture spec in under 3 minutes on CPU;
- a second run is 100% cache hits;
- killing a worker mid-task still completes after lease expiry;
- route pinning holds: changing router weights does not re-route clean nodes;
- the production config refuses to start with `PROVENANCE_MODE=mock_dev`;
- scheduler and router scoring tests pass.

### Phase 3 — Behavior backbone

**Scope:**
- Acting-model validators.
- **`behavior.resolve`**: the CBS resolver, implementing the resolution order of §10.6, DNA bounds, habits from a fixture memory snapshot, and the world behavior digest.
- **The behavior compiler**: methods, preference order, plan-time and build-time passes, editorial proposals, abstract ProsodyPlan and VisualPlan, predicted coverage.
- **Translators** for the mock engines, with golden tests.
- **Observation**: `behavior.observe` with the mock observer; triad comparison and outcomes; the `config/qc/behavior.yaml` policy wired to take ranking and to QC retry decisions (mocked QC ladder); `behavior_observations` rows; `model_behavior_profiles` aggregation from mock data.
- **Invariant tests**: I1 model swap, coverage upgrade, and the CBS no-engine-fields lint. Plus a test that a re-route flags the `compiler_approximation` elements for re-proposal.

**DoD:**
- the behavior suites marked [3] in §37 are green;
- `make e2e-mock` produces a coverage report with requested, compiled and observed entries for every CBS item of the fixture spec.

### Phase 4 — AI Director, intent, acting, memory retrieval, previz

**Scope:**
- **`ce_llm` providers**: hosted, OpenAI-compatible, and fixture replay with a recorder. Prompt templates.
- **Director stages 1–11** (stage 12 lands in Phase 12), including:
  - the **tag parser** and exact-script extraction with byte-equality checks;
  - duration fitting with seeded WPM;
  - mode templates and strategy packs;
  - the intent vocabulary and the intent policy engine with traceability;
  - the situational acting stage.
- **Memory**:
  - `MemoryRetriever` (structured + keyword) and MemorySnapshots;
  - the repetition guard (keyword and n-gram);
  - the contradiction checker (canon and persona memory).
- **Safety and research**:
  - blocklist and testimonial-guard rules (the classifier runs through the LLM provider, with fixtures in tests);
  - research and fact check on pasted text and URLs fetched through the **SSRF-guarded fetcher**, with keyword retrieval.
- **Workflows**: `PlanVideoWorkflow` → `PrevizWorkflow` → `previz_ready` → `:approve` → `GenerateVersionWorkflow`; `director_runs` persistence; the route preview for stage 8.
- **Usage events** are written when a version reaches `ready`, because the repetition guard reads them from this phase on.
- **The template-Director fallback** for free-form dev input (§37).
- **Phase split**: this phase builds the testimonial-guard *detection* (rules + LLM classification) and in-memory research on pasted text and fetched URLs; persistent ingestion and the claim ledger in Phase 12.

**DoD:**
- acceptance fixtures pass in fixture mode for all creation examples of §2, the human behavior examples of §15.5 and the trajectory example of §15.4;
- exact-script property tests (Unicode, punctuation, Turkish and Azerbaijani letters) pass;
- the invalid-JSON repair test, prompt-injection fixtures, the memory retrieval, contradiction and repetition tests [4], and the SSRF tests pass.

### Phase 5 — Frontend MVP shell

**Scope:**
- The Next.js app with auth and navigation.
- Dashboard and Projects.
- The Create wizard with previz review: intent summary, performance preview, predicted coverage, memory, repetition and contradiction reports.
- Video Studio v1: player, scene list, read-only performance lane, Simple coverage badges, job progress via SSE, versions list.
- A Creators list with read-only DNA; a Worlds list, read-only.
- Jobs; Developer basics (spec and CBS viewers); the generated client.

**DoD:**
- a Playwright e2e test in mock mode: create → previz → approve → progress → play;
- the I9 coverage-badge contract test passes.

### Phase 6 — Incremental editing

**Scope:**
- The `EditOperation` union and SpecPatch; `ProposeEditWorkflow` and `ApplyEditWorkflow` (async); reference resolution; anchor rebase.
- Locks with scopes and route pinning; takes and selection; regenerate and reroute.
- Versions: branch, restore, compare (spec, intent, CBS and coverage diffs); resume; estimates; the `:variants` and `:remix` 501 stubs.
- Impact with cascade, `no_visible_effect` and the coverage delta.
- The Studio UI for all of this, including the Advanced performance and intent editors and the NL edit panel.

**DoD:**
- exact dirty-set tests pass for:
  - behavior-only edits, with voice locked and unlocked;
  - every World DNA change in §19.5;
  - accent, camera and wardrobe changes;
  - pacing, editorial vs re-performance;
- environment lock tests pass, and locked-path edits are refused with explanations;
- restore creates a new version;
- the edit acceptance fixtures for every §2 edit example pass (accent changes use fixture voice versions until voice design lands in Phase 10);
- a re-route auto-proposes removing stale `compiler_approximation` elements;
- e2e covers "make him more skeptical" and a compare.

### Phase 7 — Real CPU pipeline: voice, captions, post, analyzers, provenance

**Scope:**
- **Voice**:
  - CPU TTS (Kokoro, dev only) and CPU ASR (faster-whisper) behind `CPU_REAL_ENGINES`, with coarse alignment;
  - language normalizers, the exact-script verification loop, ProsodyPlan translators for the CPU TTS.
- **Captions**: caption styles, safe zones, RTL (with the Arabic golden test), SRT/VTT.
- **Audio and picture post**:
  - the full audio mix chain, including per-scene world acoustics;
  - camera post and realism post;
  - reframing through the face capability.
- **Screen analysis**: scene detection, PP-OCR, the VLM mock, and zoom planning.
- **Real CPU analyzers**: MediaPipe face and body, prosody features, image embedding, AuraFace, DNSMOS, and speaker embedding if it is CPU-feasible. CPU smoke tests in CI give them `validation: smoke_passed`. With them, `behavior.observe` and the CPU parts of world QC (identity, lighting) become real.
- **Calibration**: the initial reliability classes of the observation proxies (§16.2) are calibrated on the fixture clips, and the measured values are stored per analyzer revision.
- **Real provenance**: C2PA signing with a test certificate, and VideoSeal/AudioSeal adapters (on CPU if feasible). If CPU watermarking is too slow for the golden tests, keep `mock_dev` in dev with an ADR until the `post` GPU family exists in Phase 8. Production still refuses mock provenance.

**DoD (milestone M1):**
- render golden tests pass: loudness −14 ± 1 LUFS, true peak ≤ −1 dBTP, captions present, aspect variants, Arabic captions, C2PA signature and hashes valid with an untrusted dev root;
- the exact-script loop passes with CPU TTS;
- the analyzer tests pass on the fixture clips (or skip with a reason);
- with real CPU analyzers on mock video, the triad reports `NOT_MEASURABLE` honestly where no face is detected.

### Phase 8 — Real GPU adapters (sandbox until validated)

**Scope:**
- Runtime-family Dockerfiles.
- Adapters for every row marked "8" in §39.7. For each one:
  - read the LICENSE of the model **and its dependencies** at pinned revisions and fill the license blocks;
  - declare the behavior matrix [RV];
  - implement the `BehaviorTranslator` with golden tests and `estimate()`;
  - write the smoke, bench and calibrate scripts;
  - write the contract tests with heavy calls mocked.
- A minimal golden set (`eval/smoke/`, including behavior fixtures) and the smoke-promotion flow (`promotion_basis: smoke`).
- `CalibrationWorkflow` with `POST /v1/admin/models/{model_id}:calibrate`; the `voice.prepare` conditioning for each TTS adapter.

**DoD:**
- all adapters import and pass the contract tests;
- if a GPU is present: smoke tests run, `docs/GPU_VALIDATION.md` records the results and the measured behavior profiles, and engines that pass may be smoke-promoted;
- otherwise every adapter stays `validation: untested_on_gpu`, and nothing claims otherwise.

### Phase 9 — Fleet and providers

**Scope:**
- The `local_docker`, `runpod_pod` and `runpod_serverless` GPUProviders.
- Fleet autoscaling, idle shutdown, budget caps and holds.
- The model cache with checksums and network-volume support.
- Cold-start and load-time metrics; cost actuals.
- Worker enrollment; the GPU UI (admin); the provider-plugin path proven with a third-party stub (Vast.ai and SkyPilot themselves are V1).

**DoD:**
- simulated-provider tests pass: scale up and down, budget hold, provider failure fallback;
- if `RUNPOD_API_KEY` is present: provision → smoke task → stop, with costs recorded. Never exceed `SMOKE_SPEND_CAP_USD`, and ask before any further spend;
- milestone M2 is reached if a GPU is available: one golden video renders end to end on smoke-promoted routes, with its coverage report and costs recorded.

### Phase 10 — Creator Studio and World Studio

**Scope:**
- **Creators**:
  - the Creator DNA editor (drafts and approve);
  - the Creator Memory UI (pin, forget, supersede, conflicts, proposed items);
  - the identity-pack workflow and UI; wardrobes;
  - voice design and the test bench; WPM calibration; the lexicon.
- **Worlds**: the World DNA editor with the floor-plan view and camera positions; the plates workflow; fingerprints; versions and diffs; promoting overrides.
- **Testing and consent**: the Creator Test workflow and scorecard, including the triad and baselines; the consent data model and UI (flows remain flag-off).

**DoD:**
- e2e tests in mock mode: creator → test → approve; world → plates → approve → used in a video;
- the world versioning tests pass;
- real GPU runs are recorded in GPU_VALIDATION when available.

### Phase 11 — QC gate, Performance QA, continuity, Creative Director, benchmarks

**Scope:**
- Per-node checks with thresholds keyed by metric adapter; the decision ladder and budgets.
- Performance QA wired to retries and fallbacks.
- World continuity QC (VLM element checks + embeddings).
- `ConsistencyWorkflow`, the reports and the human rating queue.
- The QC report UI, including requested/compiled/observed; the VLM judge prompts.
- Critique → proposals → edits; `AutonomousSuggestWorkflow` stub (flag off).
- The golden evaluation set in `eval/`, including behavior fixtures; the benchmark runner; the pairwise rating UI; model behavior profiles from benchmarks; bench-based promotion.
- The `audio_emotion` adapter in the sandbox.
- Calibration of the low-reliability checks (VLM window questions, the lighting-direction heuristic, relation checks) against human ratings.

**DoD:**
- failure-injection tests prove that only failed nodes rerun and that budgets cap retries;
- behavior QC actions follow the outcome matrix;
- the creator and environment continuity suites pass;
- critique proposals apply cleanly.

### Phase 12 — Memory loop, research, templates, brand, packaging and exports

**Scope:**
- **Memory loop**:
  - `MemoryUpdateWorkflow` with every write path in §18.4, and the usage log;
  - the `embed.text` adapter and embeddings for memory, hooks and research;
  - the repetition guard with embeddings; the contradiction-resolution flows.
- **Research**: ingestion (URL, PDF, docs, transcripts), the claim ledger UI with overrides, closed-book enforcement.
- **Templates and brand**: spec templates (save, apply, compose); brand-kit basics.
- **Caption translation**: the `captions_llm` adapter with the review state.
- **Packaging and exports**: Director stage 12 (Packaging) with thumbnails; exports and the export dialog.

**DoD:**
- the memory suites [12] pass;
- closed-book tests pass (no unsupported claims get through);
- the template composition, export preset and packaging-limit tests pass.

### Phase 14 — Hardening and documentation

**Scope:**
- The observability profile and dashboards.
- A scheduler load test (thousands of tasks, many simulated workers).
- The security review checklist, executed.
- A backup and restore guide; the deployment guides.
- The full docs set (§38); a demo script.

**DoD:**
- the docs are complete and all tests are green;
- a fresh clone reaches a working mock-mode demo by following the README alone.

After Phase 14, continue with the V1 items in `ROADMAP.md`.

---

## 41. Decisions that may need the product owner (ask only when you reach them)

| Decision | Default until answered |
| --- | --- |
| Operating jurisdiction and audience regions (affects MiniMax H3, Tencent Hunyuan models, LTX) | Treat as EU-serving; exclude territory-restricted models |
| Company revenue band (LTX at $10M; Stability/Krea at $1M) | Below $1M; license-gated models stay disabled by default |
| Hosted LLM provider and API key for the Director | Fixture replay in dev/test; a hosted provider when a key is supplied |
| GPU provider account(s) and budget caps | Mock provider; RunPod when a key is supplied; `BUDGET_DAILY_USD=20` |
| Buying commercial licenses (Higgs TTS 3, Fish S2 Pro [RV: Azerbaijani support]) for Azerbaijani and richer acting tags | Not purchased; Azerbaijani unsupported |
| Priority languages beyond English | de, es, fr, it, ru, tr, ar as beta |
| FFmpeg distribution for self-hosting customers (ADR 0017) | GPL build with license notices and a source offer; an LGPL build is an alternative |
| Fixture clips for real-analyzer tests | Skip those tests until the owner supplies a consented recording or approves a permissively licensed clip |
| Audio-emotion model license (emotion2vec+) | Sandbox only |
| Brand name, domain, design system | A neutral placeholder |

Spending money always requires explicit approval. That includes provisioning paid GPUs beyond a smoke test and buying licenses.

---

## 42. Start now

1. Run the Phase 0 environment inspection and write `docs/ENVIRONMENT.md`.
2. Create the repository skeleton, tooling and Compose infrastructure.
3. Write ADRs 0001–0027 from section 6, and `docs/INVARIANTS.md` from section 4.
4. Write `TODO.md` with the Phase 0–3 tasks broken down, and `ROADMAP.md` with all phases plus the V1, V2 and Experimental items.
5. Begin Phase 1. Report progress at the end of each phase.
