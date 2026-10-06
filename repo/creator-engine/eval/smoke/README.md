# Smoke golden set (`smoke-v1`)

The minimal golden set of Phase 8 (§24, §40): one or more cases per capability, the media they use and
the behavior fixtures (compiled directives) that exercise each adapter's translator. It answers one
question per adapter — *does the real model, loaded from its pinned weights, produce well-formed output for
every capability it declares?* — and its passing run is the evidence for a **smoke promotion**. Quality,
behavior success rates and human ratings belong to the full evaluation set and the benchmark runner
(Phase 11).

| Path | What |
| --- | --- |
| `cases.yaml` | The cases: capability, contract request (`$media`, `$ref:<lang>`, `@behavior/…`), `checks` (pass/fail) and `measure` (recorded only) |
| `media/` | Inputs, each pinned by sha256 in `cases.yaml` and verified before every run |
| `behavior/` | Behavior fixtures: `avatar_emotion_glance.json` (an emotion trajectory, a glance, an unsupported gesture) and `tts_pause_rate.json` (a 400 ms pause, a slower rate, an emphasis) |

## Media and their origin

| File | Origin | License |
| --- | --- | --- |
| `portrait_keyframe.png` | Synthetic drawing generated for this repository (Phase 8 WIP) | Project-owned |
| `speech_en_4s.wav` | Synthetic 4 s speech-like signal generated for this repository | Project-owned |
| `speech_ref_en.wav` | `REFERENCE_EN` spoken by the Kokoro CPU engine (`scripts/gen_smoke_media.py --speech`) | Our synthetic output (Kokoro weights Apache-2.0; the GPL phonemizer is a tool, its output is not a derivative) |
| `speech_ref_tr.wav`, `speech_ref_ar.wav` | The Turkish and Arabic references spoken by eSpeak NG (formant synthesis) | Our synthetic output (eSpeak NG is GPL; its output audio is not a derivative work) |
| `clip_small.mp4`, `talking_still_4s.mp4` | FFmpeg renders of the portrait (and `speech_en_4s.wav`) | Project-owned |

All of them are synthetic: they show that a pipeline runs, not how well a model handles a real face or a
real voice. The portrait is a drawing, so a face analyzer may not detect a face in it; that is reported as
measured (`face_detected_ratio`), never hidden. Owner-supplied, consented recordings can replace or extend
them (§16.7, `FIXTURE_CLIPS_DIR`); new files get a new set version and new sha256 pins.

`scripts/gen_smoke_media.py` regenerates the FFmpeg clips byte for byte (same FFmpeg build); `--speech`
regenerates the speech references (needs `make fetch-cpu-assets` and `espeak-ng`).

## Checks

| Check | Passes when |
| --- | --- |
| `image` / `video` / `audio` | The artifact exists and is non-empty; size, duration, fps, mean volume and luma range within the bounds given |
| `artifact` | The named output artifact exists and is non-empty |
| `candidates` | `voice.design` returned the requested number of playable candidates |
| `transcript` | WER (`max_wer`) or CER (`max_cer`) against the reference text. The Turkish case uses CER because eSpeak NG's formant voice is hard to recognize: faster-whisper-base measured CER 0.32 (WER 1.0) on it, the bound is 0.4 |
| `language` | Language identification returns the expected language |
| `alignment` | Words returned, start times monotonic, every end ≥ its start, all within the clip |
| `answer` | A VLM answer carries the keys of the requested JSON schema |
| `score` | A QC metric returned a finite score |
| `vectors` | Embeddings have at least the given dimension and a consistent length |
| `watermark_roundtrip` | The adapter's (or a sibling's) `provenance.verify` detects the watermark it just embedded |

Measurements (`measure: [face, head_motion, pauses]`) use the analyzers installed beside the adapter
(MediaPipe face landmarks, silence detection) and are stored with bench runs as the start of the measured
behavior profile; they never fail a smoke run.

## Running

```bash
# On a GPU host with the adapter's runtime family installed (its worker image):
make smoke-gpu PLUGIN=infinitetalk ARGS="--fetch --out reports/infinitetalk-smoke.json"
make bench     PLUGIN=infinitetalk ARGS="--seeds 11,12,13 --out reports/infinitetalk-bench.json"
make calibrate PLUGIN=infinitetalk ARGS="--points 5"

# Anywhere: a dry run on the CPU stand-in (manifest `test_backend`) — exercises the harness and the
# adapter's own code; its verdict is `stand_in_only` and it can never be recorded (rule 5).
make smoke-gpu PLUGIN=infinitetalk ARGS="--backend test"
```

Real CPU engines run for real anywhere their assets are fetched (`faster_whisper_cpu`, `kokoro_cpu`).

## The smoke-promotion flow

1. **Smoke run** on real weights: `make smoke-gpu PLUGIN=<id> ARGS="--fetch --record"` with a platform
   admin's API key in `CE_API_KEY`. `--record` posts the report to
   `POST /v1/admin/models/{model_id}/validations` for every model of the adapter. The API refuses a report
   from a stand-in (`backend: test`), for another adapter, or claiming `smoke_passed` while a case failed;
   a `failed` run is always recorded and takes the adapter out of production routing.
2. **Record the run** in `docs/GPU_VALIDATION.md` (host, GPU, date, commit, per-case results).
3. **Optional bench and calibration**: `make bench … ARGS="--record"` stores timings, peak VRAM and the
   measurements as a `model_benchmarks` row with verdict `pending`; `make calibrate … ARGS="--record"`
   stores knob curves, and only knobs that calibrate monotonic reach the compiler.
4. **Promotion**: `POST /v1/admin/models/{model_id}:promote` with a written note. It requires
   `validation ≥ smoke_passed` and a license closure that the operator profile allows in production.
   The model gets `status: production` and `promotion_basis: smoke` (the "smoke-promoted" badge) until a
   Phase 11 benchmark passes.
5. **Routing**: the orchestrator loads the registry overlay at startup (`ce_db.registry.load_overlay`) and
   on calibration jobs; production routing then admits the adapter. Versions already built keep their
   pinned routes (§12.4).
