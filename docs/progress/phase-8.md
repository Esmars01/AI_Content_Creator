# Phase 8 — Real GPU adapters (sandbox until validated): progress report

**Date:** 2026-10-04 · **Status:** DoD passed for the no-GPU branch — every adapter imports and passes the contract tests; **no GPU was available, so every GPU adapter stays `validation: untested_on_gpu` and nothing claims otherwise.** · **Path taken:** a new container (4 vCPUs, 15 GiB, no GPU); Docker Compose `core` + `mock-gpu` with images pulled through `mirror.gcr.io` (Docker Hub rate-limited this host); real CPU engines from `make fetch-cpu-assets`; fixture LLM. No money was spent, no paid GPU was provisioned and no hosted LLM was called.

This report finishes the Phase 8 session that was stopped by the owner; that session's handoff is kept unchanged in `docs/progress/PHASE_8_HANDOFF.md`. The project was reconstructed from `bundles/creator-engine-phase8.bundle` (history of Phases 0–7 plus the WIP snapshot `e490262`, tree-identical to `phase8-wip` `f3825f6`) into the archive repository, and Phase 8 was completed on top of it.

## Definition of Done

> all adapters import and pass the contract tests; if a GPU is present: smoke tests run, `docs/GPU_VALIDATION.md` records the results and the measured behavior profiles, and engines that pass may be smoke-promoted; otherwise every adapter stays `validation: untested_on_gpu`, and nothing claims otherwise. (§40)

| DoD item | Where | Result |
| --- | --- | --- |
| All adapters import and pass the contract tests | `plugins/mock/tests/test_mock_contract.py` (parametrized over every installed adapter), plugin test suites | pass: 28 GPU adapters on their CPU stand-ins (`test_backend`, ADR 0051), every real CPU engine on its real weights, every mock |
| GPU present → smoke runs recorded | — | **not applicable: no GPU.** No GPU smoke, bench or calibration run exists; `docs/GPU_VALIDATION.md` says so |
| Otherwise every adapter stays `untested_on_gpu` | manifests, `docs/MODELS.md` (generated), `docs/GPU_VALIDATION.md`, this report | pass: all 28 GPU adapters are `status: sandbox`, `validation: untested_on_gpu`; the API refuses evidence from stand-in runs |

The phase's scope items beyond the DoD line — runtime-family Dockerfiles, license blocks, behavior matrices, translators with goldens and `estimate()`, smoke/bench/calibrate scripts, the minimal golden set and the smoke-promotion flow, `CalibrationWorkflow` with `:calibrate`, `voice.prepare` for every TTS adapter — are built; their test state is below.

## What was built

| Area | Result | State |
| --- | --- | --- |
| Plugin SDK `ce_plugin_kit` (ADR 0051) | `EngineAdapter` with a swappable backend, CPU stand-ins, `ModelPaths`, media/speech/align (CTC Viterbi)/translate/gpu helpers | implemented and tested (CPU) |
| 28 GPU adapters (§39.7 "8" rows) | z_image_turbo, qwen_image_edit, infinitetalk, longcat_avatar, wan22_a14b_t2v/i2v, wan22_ti2v_5b, chatterbox/_turbo/_multilingual, qwen3_tts, voxcpm2, musetalk_v15, qwen3_asr, qwen3_forced_aligner, ctc_aligner, acestep_v15, moss_sfx_v2, vlm_qwen36_35b_a3b, vlm_qwen38_27b, seedvr2_3b, rife_425, videoseal, audioseal, speaker_ecapa, syncnet_v1, uvq_15, utmos_v2 — license blocks for the model and every dependency at pinned revisions, behavior matrices [RV], translators with golden tests, `estimate()`, `voice.prepare` conditioning; `size_gb` measured at the pins (~637 GB in total) | adapter code implemented and tested on stand-ins; **real backends implemented but untested (no GPU)** |
| Model cache | `hf`, `url` (sha256 pin mandatory), `s3` fetchers; subset-aware cache dirs; `ensure_plugin_models` → `model_paths`; `CE_ADAPTER_DEFAULTS` | implemented and tested (fakes; real HF fetch exercised by the CPU assets) |
| Scheduler | OOM escalation to the next VRAM class | implemented and tested |
| Registry (ADR 0053) | `ce_db.registry`: manifest sync, promote, disable, evidence, knob calibrations; overlay aggregated per adapter over its models; orchestrator sync at startup with a 30 s refresh; **per-version registry snapshots** (I5) | implemented and tested |
| API | `GET /v1/models`, `GET /v1/models/{id}` (evidence, knob curves, benchmarks); `POST /v1/admin/models/{id}:promote`, `:disable`, `:calibrate`, `/validations`, `/calibrations`, `/benchmarks`; rule-5 guards | implemented and tested |
| Calibration | `ce_behavior.knobs`, `ce_exec.calibration`, `CalibrationWorkflow`; GPU families get `needs_gpu_host` | implemented and tested (mock avatar end to end through Temporal) |
| Golden set `eval/smoke` (`smoke-v1`) | cases for every capability; sha256-pinned synthetic media (drawn portrait, FFmpeg clips, Kokoro English and eSpeak NG Turkish/Arabic speech); behavior fixtures; README with the smoke-promotion flow | implemented and tested |
| Harness | `ce_worker.validation`, `scripts/{smoke,bench,calibrate}/run.py`, `make smoke-gpu|bench|calibrate PLUGIN=…`, `--record` | implemented and tested (stand-ins; real CPU engines) |
| GPU family images (ADR 0052) | `infra/docker/families.yaml` → `scripts/gen_worker_dockerfiles.py` → 9 `worker-<family>.Dockerfile`, 21 build targets; `python -m ce_worker` entrypoint | generated, consistency-tested and lint-checked; **not built** (no GPU, disk allowance) |
| Licenses | `scripts/verify_licenses.py` (offline in the test suite; `--online`), Phase 7 branch URLs re-pinned (D100) | implemented and run |
| OCR | PP-OCRv6 small through RapidOCR (D99) | implemented and tested |
| I11 | production needs a production-routable watermarker per provenance capability | implemented and tested |
| Plan change | Phase 13 removed (ADR 0050, SPEC_ERRATA P1); `docs/MASTER_BUILD_PROMPT.md` = v2_private + E1 | done |
| Docs | ADRs 0050–0053; DECISIONS D90–D102; MODELS (generated), GPU_VALIDATION, GPU_SETUP, MODEL_INSTALLATION; ADAPTERS, PLUGINS, ENVIRONMENT, INVARIANTS, ROADMAP, TODO, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `ruff check .`, `ruff format --check .` | pass |
| `mypy` (651 files) | pass (the handoff's 15 errors fixed) |
| `eslint .`, `prettier --check .`, `tsc` (api client, web) | pass |
| `uv run pytest --ignore=tests/e2e` (infra up, CPU assets present) | 1644 passed, 1 skipped (owner fixture clips), 2 failed → both fixed and re-run green: the I14 lint now allows exactly the worker runtime to use the plugin SDK; the ADR file test knows 0050–0053 |
| Vitest (`apps/web`) | 50 passed |
| `make e2e-mock` | pass: fixture build −13.9 LUFS, 25/25 coverage entries; warm rerun 41/41 cache hits with an identical render; text → previz (53 s) → approve → video; edit → apply → compare → restore (all cache hits); worker kill recovered 53 s after the kill |
| `pytest tests/e2e` (Compose stack) | 11 passed (9 min 57 s), including the real-engine test (Kokoro + faster-whisper + MediaPipe + C2PA) and the screen-recording e2e |
| `make test-e2e` (Playwright, Google Chrome 154 via `CE_E2E_CHROME`) | 2 passed (4.3 min): create → previz → approve → progress → play; make him more skeptical → proposal → apply → compare |
| `scripts/verify_licenses.py --online` | 101 license blocks: 89 evidence checks re-verified at the pinned revisions, 0 mismatches, 5 warnings (3 PyPI packages without license metadata, fairseq2's hash recorded from a non-file URL, SyncNet's unlicensed weights) |
| `scripts/smoke/run.py <id> --backend test` × 28 GPU adapters | every report `stand_in_only`; 26 adapters pass every case on their stand-ins; expected stand-in limits: `qwen3_asr` (the stand-in does not recognize speech: WER 1.0) and `audioseal` (stand-in tones do not survive AAC under speech) |
| `scripts/smoke/run.py faster_whisper_cpu` / `kokoro_cpu` (real weights) | both `smoke_passed`: WER 0 on English, Turkish CER 0.32 on eSpeak speech, en/tr/ar alignment monotonic; Kokoro's requested 400 ms pause measured as a 540 ms silence |
| `scripts/bench/run.py kokoro_cpu --seeds 1,2` | verdict `pending`; p50 7.9 s for the 12-word case |
| `scripts/calibrate/run.py infinitetalk --backend test` | stand-in shows no knob effect → `calibrated: false` with both reasons (honest) |
| `docker build --check` on the 9 family Dockerfiles | no warnings |

New tests (Phase 8 completion): models API (8), registry (6), registry snapshots (3), knob calibration (6), harness (11), router overlay (1), I11 promotion (1), phase-8 tooling (4: license blocks, Dockerfile freshness and coverage, generator refusal, MODELS freshness); updated: I11, I12 route coverage, I14 SDK host, phase-0 docs and Makefile tests, render golden OCR (PP-OCRv6 assets).

### E2E

`make build-images` rebuilt the api, services and web images from this tree; `make dev` brought the stack up healthy in mock mode; `make e2e-mock`, `pytest tests/e2e` and Playwright then passed as above. Node is 22.22 in this container (the workspace asks for ≥ 24; pnpm warns, nothing failed).

## What is untested, and why

- **Every GPU adapter's real backend** — no GPU. Their adapter logic is tested on stand-ins; their behavior, quality, speed and VRAM are unknown (`untested_on_gpu`). Declared behavior matrices are model-card claims [RV].
- **The family images** — generated and lint-checked, never built with their CUDA stacks (no GPU; this container's disk allowance cannot hold a CUDA base plus a torch stack). Torch pins marked [RV] in `families.yaml` are confirmed by the first build.
- **Fetching the GPU weights** — the Hugging Face pins were checked through the HF API (every repo resolved at its revision, sizes measured), but no GPU weights were downloaded.
- **Owner inputs:** RIFE 4.25 weights (Google Drive only, D95), SyncNet weights' license (D94), MuseTalk's OpenRAIL-M use restrictions before promotion, real consented clips for visual calibration (D89).

## Important decisions

ADR 0050 (phase mapping; Phase 13 removed), ADR 0051 (engine adapter/backend split), ADR 0052 (family images generated from the manifests, `python -m ce_worker`), ADR 0053 (smoke golden set, evidence rules, registry overlay and per-version snapshots); D90–D102 (InfiniteTalk bf16 + LoRA, LongCat separator, UVQ over FAST-VQA, MediaPipe faces for MuseTalk/SyncNet, SyncNet and RIFE owner inputs, VideoSeal 0.0, `bench_passed` only from Phase 11, synthetic smoke media, PP-OCRv6, pinned license URLs, the family entrypoint, one runner per kind).

## Known issues and limitations

- The handoff said "31 plugin ids"; its own list (and the registry) has 28 GPU adapters.
- Stand-in limits in dry runs are expected and documented (ASR recognition, AudioSeal under AAC).
- `eval/smoke` is synthetic: it proves pipelines run, not quality on real people.
- Calibration of GPU engines runs on a host with `scripts/calibrate`; dispatching calibration renders to the fleet is later work.
- Bench-based promotion (`bench_passed`) waits for the Phase 11 benchmark runner.

## Final Phase 8 state and what Phase 9 inherits

- 28 sandbox GPU adapters ready for smoke runs the moment a GPU host exists; the harness, the evidence API and the promotion flow are in place.
- Phase 9 builds the providers that create such hosts (`local_docker`, `runpod_pod`, `runpod_serverless`, a third-party stub), the fleet's autoscaling, budgets and holds, enrollment, cost actuals and the GPU admin UI. Milestone M2 (a GPU-validated golden video) needs a GPU, which this environment does not have; any paid run needs the owner's explicit approval.

## Post-audit corrections (2026-10, after Phase 14)

- **W8** — hashing model weights blocked the worker's event loop and its heartbeats. **D6** — none of the Phase 8 GPU adapter plugins was installed in the Compose services image, so a GPU worker registering with that scheduler would have had every adapter dropped.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-8`).
