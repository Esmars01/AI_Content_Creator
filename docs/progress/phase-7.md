# Phase 7 — Real CPU pipeline: voice, captions, post, analyzers, provenance: progress report

**Date:** 2026-10-04 · **Status:** DoD passed (milestone **M1** reached) · **Path taken:** Docker (Compose `core` + `mock-gpu`), 2 vCPUs, no GPU, fixture LLM, real CPU engines from `make fetch-cpu-assets` (0.57 GB, pinned and checksummed). No money was spent, no paid GPU was provisioned and no hosted LLM was called.

## Definition of Done

> render golden tests pass: loudness −14 ± 1 LUFS, true peak ≤ −1 dBTP, captions present, aspect variants, Arabic captions, C2PA signature and hashes valid with an untrusted dev root; the exact-script loop passes with CPU TTS; the analyzer tests pass on the fixture clips (or skip with a reason); with real CPU analyzers on mock video, the triad reports `NOT_MEASURABLE` honestly where no face is detected. (§40)

Verified on a **fresh clone** of the committed code (commit `33fe47b`): `make bootstrap`, `make build-images`, `make e2e-mock`, `make test-e2e` (both Playwright specs, Google Chrome), `make lint`, `make typecheck`, `make fetch-cpu-assets` (all 14 files from their pinned URLs, checksums verified) and `CE_REQUIRE_INFRA=1 CE_REQUIRE_CPU_ASSETS=1 make test` all exited 0 — **1477 pytest tests passed and 1 skipped** (the analyzer fixture-clip test: owner-supplied clips are not present, §16.7), **50 Vitest tests passed**. The real engines also ran on the Compose stack built from a clone at the same commit (a fixture video spoken by Kokoro, verified by faster-whisper, observed by MediaPipe and signed with C2PA).

| DoD item | Where | Result |
| --- | --- | --- |
| Loudness −14 ± 1 LUFS, true peak ≤ −1 dBTP | `packages/py/ce_render/tests/test_render_golden.py::test_loudness_and_true_peak` | pass: the delivered (AAC) file measures within ±1 LU of −14 and ≤ −1 dBTP; the mix targets TP − 1.5 dB because AAC added ~0.7 dB of inter-sample peak (D83) |
| Captions present | `…::test_captions_present_via_ocr`, `tests/e2e/test_screen_mock.py` | pass: PP-OCR reads the burned caption words back from every aspect variant; builds record ASS, SRT and VTT as `captions` rows |
| Aspect variants | `…::test_streams_resolution_fps_duration`, `…::test_aspect_variant_uses_the_blurred_fill_layout` | pass: 9:16, 16:9 and 1:1 at preset resolution, 30 fps, H.264/AAC, full duration; the 16:9 variant of a portrait take uses the blurred-fill layout (a blurred picture on the sides, the sharp take in the middle) |
| Arabic captions | `…::test_arabic_captions_are_shaped_and_right_to_left` | pass: Noto Sans Arabic picked by glyph coverage; contextual forms join the letters of a word (fewer ink blobs than the isolated letters) and the first letter sits on the right |
| C2PA signature and hashes valid with an untrusted dev root | `…::test_c2pa_signature_and_hashes_valid_with_untrusted_dev_root`, `tests/e2e/test_cpu_real.py`, `apps/api/tests/test_jobs_api.py` | pass: Valid signature and BMFF hash match; the only failure code is `signingCredential.untrusted`; a tampered copy is `invalid`; `GET /v1/renders/{id}/verify` answers `mock_dev` (watermark layers mocked, ADR 0047) |
| The exact-script loop passes with CPU TTS | `tests/e2e/test_cpu_real.py` | pass: Kokoro (CPU) speaks both segments, faster-whisper verifies them (WER/CER within thresholds) and aligns them coarsely; the version is `ready` |
| Exact-script loop: retries and `needs_review` | `tests/e2e/test_exact_script.py` | pass: a persistent mock misread is re-synthesized `retries_per_node` times with `attempt_seed(base, n)` (attempt reason `qc_retry`), then the verify nodes and the version are `needs_review` with the render kept for review; matching segments pass on the first attempt |
| Analyzer tests on the fixture clips (or skip with a reason) | `plugins/analysis/*/tests`, `plugins/mock/tests/test_mock_contract.py` | pass: contract suite on real weights for every CPU engine; synthetic behavior tests (pitch 140/220 Hz, pauses, a drawn face vs an empty room, embedding similarity); the fixture-clip test **skips with the reason** — owner-supplied consented clips are not present (§16.7) — and its code path was exercised on a synthetic manifest |
| Real analyzers on mock video: `NOT_MEASURABLE` where no face | `tests/e2e/test_cpu_real.py` | pass: with faceless mock takes, MediaPipe reports `face_detected_ratio = 0`; every face-dependent item is `NOT_MEASURABLE` with the method `… (face not detected)`, none passes |

## What was built

| Area | Result | State |
| --- | --- | --- |
| Real CPU engines (ADR 0042) | `CPU_REAL_ENGINES` auto/on/off, manifest `assets`, startup checks, router `cpu_unavailable` filter, real beats mock, `prefer_mock`, `scripts/cpu_assets.yaml` + `make fetch-cpu-assets`, `gpu.local` / `cpu_local` pool, copyleft license conditions, OpenCV override | implemented and tested |
| Voice (ADR 0043, 0044) | Kokoro CPU TTS (dev only, `kokoro_v1` translator: rate and pauses parametric), faster-whisper transcribe / coarse align / LID, normalizers for 9 languages, WER/CER comparison, the exact-script loop with attempt-seed retries and `needs_review` | implemented and tested |
| Captions | Noto fonts with coverage fallback and missing-glyph QC, RTL, safe zones, ASS/SRT/VTT, `captions` rows, list and download endpoints | implemented and tested |
| Post (ADR 0048) | `ce_camera` motion, focus hunts, exposure drift, motion blur, One-Euro subject tracks, crop plans and crop loss with blurred fill; `ce_realism` picture plans; mic chain, room IR, de-esser, room-tone beds; true-peak headroom | implemented and tested |
| Analyzers | MediaPipe face and body, prosody features, DINOv2, AuraFace, DNSMOS, PP-OCR (all `smoke_passed`); real `behavior.observe`; CPU `qc.world` (identity, lighting) | implemented and tested |
| Calibration (ADR 0045) | `ce_behavior.calibration`, `ce_db.calibration`, `make calibrate-analyzers` (`--load` for recorded runs); speech-rate proxies F1 0.91 / 0.96 (n = 30) → `high` (`eval/calibration/speech-v1.json`); visual proxies wait for owner clips | implemented; visual part needs owner input |
| Provenance (ADR 0046, 0047) | C2PA signer with a generated dev CA, `provenance.verify`, `classify_c2pa`, `GET /v1/renders/{id}/verify`; VideoSeal/AudioSeal measured on CPU (20 s per second of 1080×1920 video; 101 s per 30 s of audio) and kept `mock_dev` in dev until Phase 8 | implemented and tested; watermarks deferred with an ADR (allowed by §40) |
| Screen recordings (ADR 0049) | `ScreenAnalysisWorkflow` (scdet cuts, keyframes, PP-OCR, text and pixel diffs, dead time, mock VLM summary) → `screen_analysis` artifact + `GET /v1/assets/{id}/screen-analysis`; planning of edit-added screen shots (anchors, retiming, zooms, bubble corner); real `screen.prepare` (speed segments, `zoompan` windows, letterbox); webcam bubble in `render.final` | implemented and tested |
| CI | `cpu-engines-smoke` job: fetches the assets (cached) and runs the contract suite on real weights with `CE_REQUIRE_CPU_ASSETS=1`, plus analyzer, golden and screen tests | added |
| Docs | ADRs 0042–0049, DECISIONS D78–D89, PLUGINS, ADAPTERS, EDITING, BEHAVIOR, ENVIRONMENT, ENVIRONMENT_VARIABLES, INVARIANTS, FRONTEND, plugin READMEs, TODO, ROADMAP, CHANGELOG | written |

## Tests run and results

Development tree (this environment; real CPU engines present in `.cache/models`):

| Command | Result |
| --- | --- |
| `make lint` (ruff, eslint, prettier) | pass |
| `make typecheck` (mypy 495 files, tsc for the API client and the web app) | pass |
| `uv run pytest` (whole repository, e2e included) | 1478 tests: 1476 passed, 1 skipped (the analyzer fixture-clip test: no owner clips), 1 failed — `tests/e2e/test_plan_mock.py` hit its 300 s generation timeout (5 min 34 s: a dozen shots now get camera and realism post at 1080×1920 on 2 vCPUs). Realism post was made ~35 % cheaper (chroma tables instead of an RGB `colorbalance`) and the timeout raised to 600 s; re-run: passed (4 min 51 s including planning, previz and two replans) |
| Vitest (`apps/web`) | 50 passed (9 files) |
| `tests/e2e/test_cpu_real.py` (real engines) | passed (2 min): Kokoro + faster-whisper exact-script loop, MediaPipe `NOT_MEASURABLE` on faceless takes, C2PA verified through `verify_render_file` |
| `tests/e2e/test_screen_mock.py` | passed (2 min): upload → validation → analysis → planned edit → render with zoom and webcam bubble, caption rows |
| `apps/api/tests/test_assets_api.py -k screen` | passed: real PP-OCR (CPU_REAL_ENGINES=auto) reads both lines and the diff names the new one |
| `scripts/calibrate_analyzers.py --dry-run --json` | visual proxies skipped (no owner clips); speech-rate proxies F1 0.91 / 0.96 at n = 30 → `high`; stored into the dev database with `--load` |
| VideoSeal/AudioSeal feasibility (scratch venv, deleted afterwards) | measured as in ADR 0047 |
| `scripts/gen_openapi.py --check`, `scripts/gen_schema.py --check` | fresh |

New tests (Phase 7): normalizers (8 groups), CPU-engine routing (5), camera (5), realism (3), fonts (2), render goldens (6), screen media (7), C2PA verdicts (2), calibration (3), screen planner (4), analyzers (5), API verify/captions/screen analysis (5), I12 cases for the 4 new routes, e2e exact script (2), real engines (1), screen (1); updated: mock contract (real engines, `CE_REQUIRE_CPU_ASSETS`), phase-0 doc and layout tests, config tests, the plan e2e timeout.

## Fresh-clone verification

| Command (fresh clone, commit `33fe47b`) | Result |
| --- | --- |
| `make bootstrap` | pass |
| `make build-images` | api, services and web rebuilt from the clone |
| `make e2e-mock` (no CPU assets yet: mock mode) | pass: cold fixture build 75.0 s (< 180 s; Phase 6 took ~23 s — camera, realism and acoustics post are now real), 1080×1920 H.264/AAC, −14.1 LUFS, true peak −2.5 dBTP, coverage 25/25; 41/41 cached rerun with the identical render; text → previz 9.0 s → approved video (242 s in total); **edit**: proposal (set_acting + add_behavior_event, voice lock keeps the audio) → apply → the reveal re-performed, the hook 16/16 cache hits → compare (8 spec differences, CBS `scn_reveal` only) → restore v3 all cache hits; worker kill recovered 77.0 s after the kill |
| `make test-e2e` (Playwright, Google Chrome 154 via `CE_E2E_CHROME`) | 2 passed (7.2 min): create → previz → approve → progress → play (5.2 min); make him more skeptical → proposal → apply → compare (2.0 min) |
| `make lint` / `make typecheck` | pass / pass (mypy 495 files, tsc) |
| `make fetch-cpu-assets` | 14/14 files downloaded from the pinned URLs and checksum-verified (546 MB) |
| `CE_REQUIRE_INFRA=1 CE_REQUIRE_CPU_ASSETS=1 make test` | 1477 pytest passed, 1 skipped (owner fixture clips), 27 min 16 s, including the real-engine e2e and every CPU engine's contract test on its weights + 50 Vitest passed |
| Compose stack with the real engines (a clone at the same commit: images rebuilt, services recreated after the fetch; `ce video generate-fixture`) | `ready` in 104 s: `tts.segment` on `kokoro_cpu`, `asr.verify`/`align.segment` on `faster_whisper_cpu` (both segments WER 0), `behavior.observe` on `mediapipe_face`, `provenance.sign` on `c2pa_signer`; the delivered file's C2PA manifest: signature and hashes valid, only `signingCredential.untrusted`, ingredients list the real CPU engines |

Findings during verification (fixed before the final commit):

- On the first clone (commit `1a47435`) the Compose run with real engines ended **`needs_review`**: Whisper heard "But here's the thing" as "about it here's the thing" on the second segment and the retry misheard it too. The exact-script loop did its job; the cause was Kokoro's abrupt onset. `faster_whisper_cpu` now pads each clip with 0.5 s of silence and shifts the word times back (`asr.verify` / `align.segment` → impl 3); the same check then passed with WER 0.
- That clone's full suite without assets: 1467 passed, 11 skipped (10 real-engine tests skip with the missing files, 1 owner-fixture test); with assets the real-engine subset passed (68 passed, 1 owner-fixture skip).
- `/opt/google/chrome/chrome` in this environment is Playwright's Chromium (no H.264), so the play step failed until Google Chrome 154 was unpacked to `/opt/chrome-stable` (docs/ENVIRONMENT.md).
- The image build ran out of the disk allowance once (Docker build cache at 17 GB); `docker builder prune` freed it.

## Files changed

Added: plugins `kokoro_cpu`, `faster_whisper_cpu`, `mediapipe` (face, body), `prosody_features`, `image_embed`, `auraface`, `ppocr`, `dnsmos`, `c2pa`, `gpu/local`; `ce_voice/{normalize,numbers,metrics}.py`, `ce_camera/{motion,reframe}.py`, `ce_realism/{video,audio}.py`, `ce_render/{fonts,screen}.py`, `ce_core/boxes.py`, `ce_behavior/calibration.py`, `ce_db/calibration.py`, `ce_policy/c2pa_verify.py`, `ce_director/screen.py`, `ce_exec/{post,worldqc,screen}.py`, `apps/api/src/ce_api/provenance.py`, `assets/fonts` (Noto Sans, Noto Sans Arabic, OFL), `scripts/{cpu_assets.yaml,calibrate_analyzers.py}`, `eval/calibration/`, `eval/fixtures/analyzer_clips/README.md`, tests (`ce_voice`, `ce_router` CPU engines, `ce_camera`, `ce_realism`, `ce_render` fonts/goldens/screen, `ce_policy` C2PA, `ce_behavior` calibration, `ce_director` screen plan, analyzer tests, `tests/e2e/test_{exact_script,cpu_real,screen_mock}.py`), ADRs 0042–0049, this report.

Changed: `ce_contracts` (manifest assets and CPU engines, `provenance.verify`, TTS/align request fields, contract suite with model cache), `ce_router` (CPU filter, real beats mock, `prefer_mock`), `ce_config` (pools, `max_cer`, calibration, render headroom and screen config), `ce_build` (graph: normalizer params, metric routes, calibration sha, `screen.prepare` deps; kinds' impl versions), `ce_exec` (requests, results, runtime retries and `needs_review`, executors for captions/mix/render/sign/QC, behavior calibration, editing with asset references and screen planning, jobs, caption rows), `ce_render` (video: camera/realism post, placements, bubble; audio beds), `ce_worker`, `ce_db` (calibration, captions), `ce_core` (edit ops `webcam_bubble`, vocab calibrated confidence), `ce_behavior` (judge), `ce_policy` (copyleft), `ce_testing` (stack env, real engines), apps (api: verify, captions, screen analysis endpoints; orchestrator: retry loop, `ScreenAnalysisWorkflow`; gpu-worker: CPU engine checks), mock plugins (misreads, faceless keyframes, schema-shaped VLM events, verify), config (pools, QC, behavior, default), Compose and the services image (model cache volume, fonts, MediaPipe libraries), CI, the TS client and schemas, docs.

## Known issues and limitations

- **Watermarks stay `mock_dev` in dev/test** (ADR 0047): measured too slow on CPU; VideoSeal/AudioSeal adapters come with the `post` GPU family in Phase 8. Dev renders verify as `mock_dev` (C2PA valid with the untrusted dev root) and are not exportable; production still refuses mock provenance.
- **Visual proxy calibration and the analyzer fixture-clip test need owner-supplied clips** (consent or a permissive license, §16.7): until then the visual proxies keep their initial §16.2 classes and the test skips with that reason. Speech-rate proxies are calibrated on synthetic speech.
- **Speaker embedding deferred** (D80): no license-verified checkpoint at a pin; `voice.embed` stays mocked.
- **PP-OCRv4 instead of v6** (D78); Latin and CJK text only (Arabic/Cyrillic OCR needs other recognition models).
- **Coarse alignment only** (faster-whisper word times); the forced aligners are Phase 8.
- **Kokoro is dev-only** (GPL phonemizer, ADR 0043); English validated, es/fr/it unvalidated; no emotion/pitch/energy control (honestly compiled as `none`).
- **The Director does not create screen shots from a plan request** (D86): screen shots come from edits (or hand-written specs); a create-body field for a screen asset is a product decision. Reaction clips reuse their asset as is (range and layout not applied).
- **The VLM is a labelled mock** (§39.2): screen summaries and VLM window proxies carry no model judgement until a vLLM worker is validated.
- **DNSMOS runs in-process** rather than on `worker-cpu` (D79).
- **Real-engine tests are slow on 2 vCPUs** (Kokoro ≈ 2.4× slower than real time): `tests/e2e/test_cpu_real.py` takes about two minutes; the deterministic suites stay on mocks.

## Next phase and actions

**Phase 8 — Real GPU adapters (sandbox until validated)**, §40:

1. Runtime-family Dockerfiles (`image`, `wan`, `tts`, `asr`, `audio`, `lipsync`, `vision`, `post`, `vllm`).
2. Adapters for every row marked "8" in §39.7 — for each: read the LICENSE of the model and its dependencies at pinned revisions and fill the license blocks; declare the behavior matrix; implement the `BehaviorTranslator` with golden tests and `estimate()`; write smoke, bench and calibrate scripts; contract tests with heavy calls mocked. Includes VideoSeal/AudioSeal (ADR 0047), the forced aligners, the speaker embedding, PP-OCRv6 and the VLM.
3. A minimal golden set (`eval/smoke/`, with behavior fixtures) and the smoke-promotion flow (`promotion_basis: smoke`).
4. `CalibrationWorkflow` with `POST /v1/admin/models/{model_id}:calibrate`; the `voice.prepare` conditioning for each TTS adapter.
5. DoD: all adapters import and pass the contract tests; with no GPU in this environment every adapter stays `validation: untested_on_gpu` and nothing claims otherwise. No paid GPU is provisioned without explicit owner approval.

## Post-audit corrections (2026-10, after Phase 14)

- **W12** — the mock adapters' FFmpeg was not killed on cancellation. **D14** — the API container received a host path as `MODEL_CACHE_DIR`.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-7`).
