# Production model and GPU operations

This document describes how Creator Engine prepares, runs and pays for GPU models in production. It is written for the engineer who has to operate the system. Each statement is either:

- taken from the code at the commit named in `docs/MANIFEST.txt`;
- taken from the plugin manifests; or
- a source with a date (section N).

Values that were **estimated** rather than measured are labeled as estimates.

> **Validation status.** No real GPU was rented while this was written. No Vast API key was available, and no money could be spent. Every behavior below is covered by mocked tests: an httpx fake of the Vast API, a mock GPU provider, and a real Postgres, Redis and S3 stack. Live tests on an A100 80 GB at Vast have **not** been run. Chatterbox and InfiniteTalk are still `status: sandbox`. Read *Before the first live run* (section M) before you call anything validated.

---

## A. Architecture

```
Browser ──HTTPS──▶ Web (Next.js) ──▶ API (FastAPI, ce_api) ──▶ Postgres / Redis / Temporal
                                        │  admin routes /v1/admin/gpu/*  (proxied to the scheduler)
                                        ▼
                                  Scheduler (ce_scheduler) ── fleet manager ──▶ GPU providers (Vast, RunPod, mock in dev)
                                        ▲  public worker endpoint  /internal/v1/worker/*  (HTTPS, bearer tokens)
                                        │
                          GPU instance (Vast)  ──  ce_worker / ce_worker.multi  ──▶ object storage (presigned GET/PUT only)
                                                     model cache  /models  (container disk or Vast volume)
```

The pieces and what each does:

- **Web.** It talks only to the API. It never receives a provider key, a worker token or a presigned URL to the model cache. Provider secrets are stored as references (`env:NAME` or `file:/path`). API responses never echo their values.
- **API.**
  - Tenant routes: creators, uploads, jobs and outputs.
  - Platform-admin GPU routes (section I): these forward to the scheduler's admin interface.
  - The job stage resolver (`ce_api/job_stages.py`), which turns queue, fleet and heartbeat records into a single stage.
- **Scheduler.** It is the only authority over the fleet. It:
  - owns the GPU task queue (`ce_db.queue`, `SELECT … FOR UPDATE SKIP LOCKED`);
  - leases tasks to workers;
  - provisions, starts, stops, restarts and terminates instances through providers;
  - reconciles the database with the provider's view every `scheduler.fleet.reconcile_interval_s` (120 s);
  - finds labeled instances it lost track of ("orphans");
  - records costs and enforces budgets.
- **Worker.** One process per runtime family, or one per component of a colocated profile through `python -m ce_worker.multi`. It:
  - dials out to the scheduler (no inbound port, no SSH);
  - registers with a one-time enrollment token;
  - long-polls for leases and heartbeats while it works;
  - downloads inputs and uploads outputs through presigned URLs only;
  - keeps its weights in the model cache.

  **A worker never connects to Postgres, Redis or Temporal.**
- **Object storage (S3-compatible).** Every input and every output lives here. The GPU disk holds only:
  - the reproducible model cache;
  - per-task scratch space, deleted after each task.

### Connectivity (cutover §16), as implemented

| Path | Mechanism | Production requirement |
|---|---|---|
| Worker → scheduler | `SCHEDULER_URL` in the instance environment = `scheduler.public_url`. HTTPS POSTs to `/internal/v1/worker/{register,lease,heartbeat,status,upload,complete,fail}`. | Must be reachable from the public internet over TLS. The startup check warns in production when `scheduler_public_url` is a private, loopback or `localhost` host. |
| Enrollment | The fleet mints a one-time enrollment token per provisioned worker (per family for a colocated profile: `WORKER_TOKEN_<FAMILY>`). Only its hash is stored. Registration trades it for a worker token, again stored only as a hash. | The token expires after `provision_timeout_s` (900 s). `:start` and `:restart` re-arm it, because a stopped host keeps its environment. |
| Heartbeat / lease | `heartbeat_s` 5 s, `lease_s` 30 s, long poll 20 s. Idle workers also send `status` every 3 s while preparing models. | A lease that is not extended for 30 s is reaped and requeued under an `infra_retry` attempt with the same seed, up to `max_infra_retries` (3). |
| Completion / failure | `complete` with output digests. The scheduler verifies every output object in storage before it accepts the task. `fail` carries `error_class` (`retryable`, `oom`, `timeout`, `cancelled`, `fatal`). | `retryable`, `oom` and `timeout` are infrastructure classes: they are retried, and an `oom` escalates the VRAM class. |
| Object storage | Presigned URLs are issued against `S3_WORKER_ENDPOINT_URL`. It defaults to the internal endpoint, which is right for compose and wrong for a rented GPU. | Set `S3_WORKER_ENDPOINT_URL` to the public HTTPS endpoint of the bucket. The startup check warns in production when it is a private host. |
| Output registration | The scheduler, not the worker, writes the asset rows after it verifies the outputs. The orchestrator (Temporal) continues the job. | — |
| Scheduler restart | Worker tokens are checked against `gpu_workers.token_hash` when the in-memory cache misses, so a worker keeps working across a scheduler restart. Leases, the queue and the fleet are in Postgres. Leader election (Redis) resumes the fleet loop. | — |
| API restart | Stateless. Jobs are Temporal workflows and queue rows, so nothing is lost. | — |
| Worker reconnect | Transport errors back off (up to 30 s) and retry. A `401` (the scheduler no longer knows the token, e.g. the row was terminated) makes the worker register again. A restarted process resumes with its stored worker token (recovery case 1, section J). | A fleet worker whose row was failed or terminated needs a re-armed enrollment token (`:start`/`:restart`) or is replaced. |

---

## B. Model registry

The **plugin manifests** (`plugins/**/plugin.yaml`) are the source of truth. Each model declares:

- its source, a repository pinned to a commit;
- the files it fetches;
- its dependencies;
- its declared size;
- minimum and recommended VRAM.

The table below was generated from the manifests at this commit. It lists every GPU model with a fetchable source.

**Sizes.** Every size is **declared**: it is the manifest's `size_gb`, which was taken from the upstream listing when the manifest was written. None was measured here, because the egress proxy blocks `huggingface.co` in this environment.

- A declared `0` means the manifest has no size yet. The profile sizer then treats that model as contributing nothing, so add a size before you put such a model in a profile.
- Workers measure the real byte counts as they download. The listing uses `?blobs=true`, so the Hugging Face fetcher learns each file's size and LFS sha256 before downloading. The measured sizes are reported per model on the GPU page (`model_states[*].size_bytes`).

**VRAM** values are manifest estimates. **"In profile"** shows whether a model is fetched at boot by a profile or only on first use.

**Status:**

- `sandbox`: the adapter exists but has not passed the promotion gates (GPU smoke run, golden comparison, admin promotion).
- **No model in this table is `production`.**

| Model key | Plugin | Capabilities | Source @ pinned revision | Files fetched | Dependencies | Size GB | VRAM min / rec. GB | Family:variant | Profile | In profile | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `acestep-v15` | `acestep_v15` | audio.music | `ACE-Step/Ace-Step1.5@19671f406d60` | `config.json`, `acestep-v15-turbo/*`, `vae/*`, `Qwen3-Embedding-0.6B/*`, `acestep-5Hz-lm-1.7B/*` | code `ACE-Step-1.5@ca1e85f` (in the image) | 9.4 (declared) | 12 / 24 (manifest) | audio:acestep | — | — | sandbox |
| `audioseal` | `audioseal` | provenance.verify, provenance.watermark_audio | `facebook/audioseal@3c19eba53390` | `generator_base.pth`, `detector_base.pth` | — | 0.09 (declared) | 2 / 8 (manifest) | post:watermarks | — | — | sandbox |
| `chatterbox-en` | `chatterbox` | voice.clone_prepare, voice.tts | `ResembleAI/chatterbox@5bb1f6ee58e5` | `ve.safetensors`, `t3_cfg.safetensors`, `s3gen.safetensors`, `tokenizer.json`, `conds.pt` | code `chatterbox@5de7a54` (in the image); code `Perth@ff1c8ac` (in the image) | 2.97 (declared) | 8 / 16 (manifest) | tts:chatterbox | talking_head_a100_80gb | optional, fetched on first use | sandbox |
| `chatterbox-multilingual-v3` | `chatterbox_multilingual` | voice.clone_prepare, voice.tts | `ResembleAI/chatterbox@5bb1f6ee58e5` | `ve.pt`, `t3_mtl23ls_v3.safetensors`, `s3gen.pt`, `grapheme_mtl_merged_expanded_v1.json`, `Cangjie5_TC.json`, `conds.pt` | code `chatterbox@5de7a54` (in the image); code `Perth@ff1c8ac` (in the image) | 2.99 (declared) | 8 / 16 (manifest) | tts:chatterbox | talking_head_a100_80gb | required, prewarmed at boot | sandbox |
| `chatterbox-turbo` | `chatterbox_turbo` | voice.clone_prepare, voice.tts | `ResembleAI/chatterbox-turbo@749d1c1a46eb` | `*.safetensors`, `*.json`, `*.txt`, `*.yaml`, `conds.pt` | code `chatterbox@5de7a54` (in the image); code `Perth@ff1c8ac` (in the image) | 3.77 (declared) | 8 / 16 (manifest) | tts:chatterbox | talking_head_a100_80gb | required, prewarmed at boot | sandbox |
| `omniasr-ctc-1b` | `ctc_aligner` | asr.align | `facebook/omniASR-CTC-1B@8c22e3ffdaa4` | `omniASR-CTC-1B.pt`, `omniASR_tokenizer.model` | — | 0 (declared) | 6 / 12 (manifest) | asr:omnilingual | — | — | sandbox |
| `infinitetalk-single` | `infinitetalk` | avatar.a2v | `MeiGen-AI/InfiniteTalk@d59847ebdacf` | `single/infinitetalk.safetensors` | base_weights: `Wan-AI/Wan2.1-I2V-14B-480P@6b73f84e6637`; audio_encoder: `TencentGameMate/chinese-wav2vec2-base@3991242c8069`; distill_lora: `lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v@fef288b326f4`; code `InfiniteTalk@50aa0a9` (in the image) | 86.92 (declared) | 24 / 48 (manifest) | wan:infinitetalk | talking_head_a100_80gb | required, prewarmed at boot | sandbox |
| `longcat-video-avatar-1.5` | `longcat_avatar` | avatar.a2v | `meituan-longcat/LongCat-Video-Avatar-1.5@92016c71d5d3` | `config.json`, `model_index.json`, `scheduler/*`, `base_model_int8/*`, `lora/dmd_lora.safetensors`, `whisper-large-v3/*.json`, `whisper-large-v3/*.txt`, `whisper-large-v3/model.safetensors` | base_weights: `meituan-longcat/LongCat-Video@03b55529b1d1`; code `LongCat-Video@6b3f4b8` (in the image) | 41.67 (declared) | 32 / 80 (manifest) | wan:longcat_avatar | — | — | sandbox |
| `moss-soundeffect-v2` | `moss_sfx_v2` | audio.sfx | `OpenMOSS-Team/MOSS-SoundEffect-v2.0@e35df4d82fbe` | `model_index.json`, `scheduler/*`, `text_encoder/*`, `tokenizer/*`, `transformer/*`, `vae/*` | code `MOSS-TTS@934d682` (in the image) | 10.46 (declared) | 10 / 24 (manifest) | audio:moss_sfx | — | — | sandbox |
| `musetalk-v15` | `musetalk_v15` | lipsync.dub | `TMElyralab/MuseTalk@3ef28bc5cff0` | `musetalkV15/unet.pth`, `musetalkV15/musetalk.json` | code `MuseTalk@0a89dec` (in the image); vae: `stabilityai/sd-vae-ft-mse@31f26fdeee13`; audio_encoder: `openai/whisper-tiny@169d4a4341b3`; face_landmarker: pinned URL (sha256) | 3.62 (declared) | 6 / 24 (manifest) | lipsync:musetalk | — | — | sandbox |
| `qwen3-asr-1.7b` | `qwen3_asr` | asr.lid, asr.transcribe | `Qwen/Qwen3-ASR-1.7B@7278e1e70fe2` | `*.json`, `*.txt`, `*.safetensors` | forced_aligner: `Qwen/Qwen3-ForcedAligner-0.6B@c7cbfc2048c4` | 6.09 (declared) | 8 / 16 (manifest) | asr:qwen_asr | — | — | sandbox |
| `qwen3-forced-aligner-0.6b` | `qwen3_forced_aligner` | asr.align | `Qwen/Qwen3-ForcedAligner-0.6B@c7cbfc2048c4` | `*.json`, `*.txt`, `model.safetensors` | — | 1.71 (declared) | 4 / 8 (manifest) | asr:qwen_asr | — | — | sandbox |
| `qwen3-tts-base` | `qwen3_tts` | voice.clone_prepare, voice.design, voice.tts | `Qwen/Qwen3-TTS-12Hz-1.7B-Base@fd4b25438912` | `*.json`, `*.txt`, `model.safetensors`, `speech_tokenizer/*` | — | 4.23 (declared) | 12 / 24 (manifest) | tts:qwen_tts | — | — | sandbox |
| `qwen3-tts-voicedesign` | `qwen3_tts` | voice.clone_prepare, voice.design, voice.tts | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign@5ecdb67327fd` | `*.json`, `*.txt`, `model.safetensors`, `speech_tokenizer/*` | — | 4.21 (declared) | 12 / 24 (manifest) | tts:qwen_tts | — | — | sandbox |
| `qwen-image-edit-2511` | `qwen_image_edit` | image.edit | `Qwen/Qwen-Image-Edit-2511@6f3ccc0b56e4` | `model_index.json`, `processor/*`, `scheduler/*`, `text_encoder/*`, `tokenizer/*`, `transformer/*`, `vae/*` | — | 53.76 (declared) | 48 / 80 (manifest) | image:image_diffusers | — | — | sandbox |
| `rife-4.25` | `rife_425` | video.interpolate | url | `train_log/flownet.pkl`, `train_log/RIFE_HDv3.py`, `train_log/IFNet_HDv3.py` | — | 0 (declared) | 4 / 8 (manifest) | post:rife | — | — | sandbox |
| `seedvr2-3b` | `seedvr2_3b` | video.upscale | `ByteDance-Seed/SeedVR2-3B@37255ff8cccf` | `seedvr2_ema_3b.pth`, `ema_vae.pth`, `pos_emb.pt`, `neg_emb.pt` | code `SeedVR@e4de8c2` (in the image); code `apex@575968b` (in the image) | 13.57 (declared) | 24 / 48 (manifest) | post:seedvr2 | — | — | sandbox |
| `spkrec-ecapa-voxceleb` | `speaker_ecapa` | voice.embed | `speechbrain/spkrec-ecapa-voxceleb@0f99f2d0ebe8` | `hyperparams.yaml`, `embedding_model.ckpt`, `mean_var_norm_emb.ckpt`, `classifier.ckpt`, `label_encoder.txt` | — | 0 (declared) | 1 / 2 (manifest) | asr:speechbrain | — | — | sandbox |
| `utmosv2` | `utmos_v2` | qc.speech_quality | `sarulab-speech/UTMOSv2@506474f2b33d` | `fold0_s42_best_model.pth` | ssl: `facebook/wav2vec2-base@0b5b8e868dd8`; code `UTMOSv2@cc2700d` (in the image) | 0 (declared) | 2 / 4 (manifest) | asr:utmos | — | — | sandbox |
| `uvq-1.5` | `uvq_15` | qc.vqa | `https://github.com/google/uvq@c204ecc04f09` | `uvq1p5_pytorch/checkpoints/content_net.pth`, `uvq1p5_pytorch/checkpoints/distortion_net.pth`, `uvq1p5_pytorch/checkpoints/aggregation_net.pth` | — | 0 (declared) | 2 / 4 (manifest) | vision:uvq | — | — | sandbox |
| `videoseal-0.0` | `videoseal` | provenance.verify, provenance.watermark_video | `facebook/video_seal@8037ef59ba2b` | `checkpoint.pth` | — | 0.15 (declared) | 4 / 16 (manifest) | post:watermarks | — | — | sandbox |
| `qwen3.6-35b-a3b-fp8` | `vlm_qwen36_35b_a3b` | vision.image, vision.video | `Qwen/Qwen3.6-35B-A3B-FP8@95a723d08a94` | `*.json`, `*.jinja`, `*.txt`, `*.safetensors` | — | 34.92 (declared) | 48 / 96 (manifest) | vllm:vllm | — | — | sandbox |
| `qwen3.8-27b` | `vlm_qwen38_27b` | vision.image, vision.video | `Qwen/Qwen3.8-27B@1d4bf0f2ff60` | `*.json`, `*.jinja`, `*.txt`, `*.safetensors` | — | 51.77 (declared) | 80 / 96 (manifest) | vllm:vllm | — | — | sandbox |
| `voxcpm2` | `voxcpm2` | voice.clone_prepare, voice.design, voice.tts | `openbmb/VoxCPM2@32279effe8c1` | `audiovae.pth`, `config.json`, `model.safetensors`, `special_tokens_map.json`, `tokenization_voxcpm2.py`, `tokenizer.json`, `tokenizer_config.json` | — | 4.62 (declared) | 10 / 24 (manifest) | tts:voxcpm | — | — | sandbox |
| `wan2.2-i2v-a14b` | `wan22_a14b_i2v` | video.i2v | `Wan-AI/Wan2.2-I2V-A14B@206a9ee1b7bf` | `*.json`, `*.pth`, `google/umt5-xxl/*`, `high_noise_model/*`, `low_noise_model/*` | distill_lora: `lightx2v/Wan2.2-Distill-Loras@570044187a52`; code `LightX2V@8a97c75` (in the image) | 118.82 (declared) | 48 / 96 (manifest) | wan:lightx2v | — | — | sandbox |
| `wan2.2-t2v-a14b` | `wan22_a14b_t2v` | video.t2v | `Wan-AI/Wan2.2-T2V-A14B@c8c270b13ee0` | `*.json`, `*.pth`, `google/umt5-xxl/*`, `high_noise_model/*`, `low_noise_model/*` | distill_lora: `lightx2v/Wan2.2-Distill-Loras@570044187a52`; code `LightX2V@8a97c75` (in the image) | 118.68 (declared) | 48 / 96 (manifest) | wan:lightx2v | — | — | sandbox |
| `wan2.2-ti2v-5b` | `wan22_ti2v_5b` | video.i2v, video.t2v | `Wan-AI/Wan2.2-TI2V-5B-Diffusers@b8fff7315c76` | `model_index.json`, `scheduler/*`, `text_encoder/*`, `tokenizer/*`, `transformer/*`, `vae/*` | — | 31.85 (declared) | 24 / 48 (manifest) | wan:wan_diffusers | — | — | sandbox |
| `z-image-turbo` | `z_image_turbo` | image.generate | `Tongyi-MAI/Z-Image-Turbo@f332072aa78b` | `model_index.json`, `scheduler/*`, `text_encoder/*`, `tokenizer/*`, `transformer/*`, `vae/*` | — | 30.59 (declared) | 16 / 24 (manifest) | image:image_diffusers | — | — | sandbox |

The table is regenerated from the manifests with the same query the profile sizer uses (`ce_scheduler.profiles`). A model that is absent here either has no fetchable weights (CPU tools, API providers) or is a mock.

### The initial production profile: `talking_head_a100_80gb`

This profile is defined in `config/gpu/profiles.yaml`. It runs **one A100 80 GB instance** that serves both the voice and the avatar.

| Component | Family:variant | Adapters | Prepared at boot | Declared size |
|---|---|---|---|---|
| Avatar | `wan:infinitetalk` | `infinitetalk` | `infinitetalk-single`: InfiniteTalk single plus Wan2.1-I2V-14B-480P base, chinese-wav2vec2-base, and the lightx2v step-distill LoRA | 86.92 GB |
| Voice | `tts:chatterbox` | `chatterbox_turbo`, `chatterbox_multilingual`, `chatterbox` | `chatterbox-turbo` (3.77 GB), `chatterbox-multilingual-v3` (2.99 GB) | 6.76 GB |
| Voice (optional) | `tts:chatterbox` | `chatterbox` | `chatterbox-en`, fetched only when first used | 2.97 GB |

How the disk is sized (`ce_scheduler.profiles.size_profile`, shown on the GPU page):

| Item | Value |
|---|---|
| Models (all declared sizes, optional model included) | 96.65 GB |
| Staging headroom (15 %) | 14.50 GB |
| Scratch | 40 GB |
| Image | 35 GB (**estimate**: the image was not built here) |
| **Total** | **186.15 GB → 190 GB** (rounded up to the next 10 GB) |
| VRAM | minimum 40 GB together (24 + 8 + 8), recommended 80 GB |

Other settings:

- `concurrency: 1`. InfiniteTalk alone recommends 48 GB.
- `resident_together: true` and `prewarm: boot`.

The disk size is passed to Vast as the instance's `disk`: the larger of the computed size and the provider's configured `storage.disk_gb`.

A single-family worker (not a profile) gets its own family disk: `variant_disk_gb` = declared models × 1.15 + 60 GB, rounded up to 10. For `wan:infinitetalk` alone that is 160 GB. RunPod ignores the disk size, so its behavior is unchanged.

### Upstream reconciliation (checked 2026-10-08, section N)

- **InfiniteTalk upstream** pins torch 2.4.1 (cu121), xformers 0.0.28 and flash_attn 2.7.4.post1. **Chatterbox 0.1.7** pins torch 2.6.0 and transformers 5.2.0. The two cannot share a Python environment. The profile image therefore keeps one virtual environment per variant (`/opt/env/<variant>`), and `ce_worker.multi` starts each worker with its own interpreter.
- **wav2vec2 weights format.** Upstream InfiniteTalk loads chinese-wav2vec2-base `model.safetensors` from revision `refs/pr/1`. Our manifest pins `pytorch_model.bin` at commit `3991242c`. This is marked **[RV] — reverify on the first GPU run**. Both describe the same weights, but the format the adapter loads must be confirmed before promotion.
- **Low-VRAM flag.** Upstream documents `--num_persistent_param_in_dit 0` for low VRAM. It is not needed on 80 GB, and the adapter does not set it.

---

## C. Installation strategy

Nothing is installed by hand on a rented GPU. The cutover removes the "SSH in and pip install" step, and no part of the normal workflow needs SSH.

1. **Code and dependencies are built into the image.**
   - Each family has a generated Dockerfile: `infra/docker/worker-<family>.Dockerfile`, with one build target per variant.
   - Each colocated profile has one too: `infra/docker/worker-profile-<image>.Dockerfile`.
   - Both are generated by `scripts/gen_worker_dockerfiles.py` from `config/gpu/families.yaml`. CI checks that they are current (`--check`).
   - Each upstream code dependency is cloned at its pinned commit in the build: InfiniteTalk `50aa0a9`, chatterbox `5de7a54`, Perth `ff1c8ac`.
2. **Images are published by the manual workflow** `.github/workflows/worker-images.yml`, run with `workflow_dispatch`.
   - Targets: `profile:talking_head`, `wan:infinitetalk` or `tts:chatterbox`.
   - Each image gets two tags: an immutable `<variant>-<sha12>` and a moving `<variant>`.
   - **Point providers at the immutable tag.**
   - Profile image: `ghcr.io/<owner>/creator-engine-worker-profile:talking_head-<sha12>`.
   - The workflow has never been run, because this environment has no registry credentials. Its first run records the real image size. Put that size into `image_gb` in `profiles.yaml` to replace the 35 GB estimate.
3. **Weights are not in the image.** The model cache downloads them (section D), pinned by revision and verified by sha256.
4. **Vast starts the image as is.** It uses `runtype: args`, so the image's own entrypoint runs (`python -m ce_worker` or `python -m ce_worker.multi`). There is no SSH, no Jupyter and no onstart script.
   - For a private registry, set `image_login_ref` (`env:VAST_IMAGE_LOGIN` or `file:/run/secrets/...`). The scheduler resolves it into Vast's `image_login` when it rents. The value is never stored or returned.

---

## D. Download lifecycle

The worker downloads model files through the model cache (`ce_worker.model_cache.ModelCache`) and its fetchers (`ce_worker.fetchers`). Nothing else downloads them.

1. **Plan.** `fetch_plan(manifest)` lists the cache entries: each `(source, revision, files)` of the model and its dependencies. Each entry gets a cache key and a path under `MODEL_CACHE_DIR` (`/models`). The path is content-addressed by URI, revision and file set. Entries shared by several models are fetched once.
2. **Already cached?** The cache index (`index.json`, guarded by file locks) is read again for each request. Any entry in it is reused **without downloading it again**, whether this worker or an earlier one on the same disk or volume finished it. Models are fetched once per cache, not once per job.
3. **Space.** `ensure_space(need)` runs before a download:
   - It frees least-recently-used entries that are unpinned, unused and older than the grace period.
   - It never frees the model being fetched or anything a profile pinned.
   - When there is still not enough room it raises **`NotEnoughDiskError`**: "needs X GiB (+5 GiB reserve), Y GiB free".
   - Setting: `MODEL_CACHE_RESERVE_GB` (default 5).
4. **Fetch into staging.** Files download into `<target>.partial-<pid>-<random>`.
   - Hugging Face: `hf://org/repo@<commit>`.
     - Listing: `GET /api/models/{repo}/tree/{revision}?recursive=true&blobs=true`, which returns sizes and LFS sha256.
     - Files are fetched with `MODEL_FETCH_CONCURRENCY` (default 4) at a time, streamed in 1 MiB chunks.
     - Timeouts: 60 s connect, 600 s read.
     - Gated models use `HF_TOKEN`, which the scheduler resolves from `scheduler.fleet.worker_secret_refs` (e.g. `HF_TOKEN: env:HF_TOKEN`) and never stores.
   - `url://` sources must have sha256 pins.
5. **Verify.** The staging tree is hashed **in a thread** (`asyncio.to_thread`), so heartbeats and status reports keep running while large files are hashed. Two checks follow:
   - **Manifest sha256 pins.** A mismatch fails as `ModelCacheError`, class `fatal`. The pin or the source is wrong, and retrying would only download the same bytes again.
   - **Hugging Face LFS sha256 for unpinned files.** A mismatch fails as `ModelFetchError`, class `retryable`: a corrupt transfer.

   The index records how each file was verified: `pinned`, `upstream` or `recorded`.
6. **Commit.** `os.replace(staging, target)` is atomic. The entry is then added to the index.
7. **Failure or cancel.** The staging directory is removed (`rmtree`), so no partial tree is left behind. At boot, `clean_staging()` removes `*.partial-*` directories older than 6 h that a crash left behind.
8. **Progress.** Each fetch has a `FetchProgress`: bytes done and total, files, speed in MB/s and ETA. Progress travels to the scheduler on each heartbeat, `status` report and lease (`model_states`), and from there to the GPU page and to the job's stage (`downloading_model` with bytes and ETA).

**Interrupted downloads.** Recovery works **per file entry, not per byte range**:

- Completed cache entries survive.
- A partly downloaded entry starts again from zero on the next attempt.
- There is no HTTP `Range` resume inside a file. For the 86.92 GB InfiniteTalk tree, an interruption late in a single large file costs that file again.

This is a known limit. A possible improvement: keep staging per file across attempts and resume with `Range`, validated against the LFS sha256.

---

## E. Startup lifecycle (an instance boots)

1. The fleet rents the instance with this environment:
   - `SCHEDULER_URL`
   - `WORKER_RUNTIME_FAMILY` (or `WORKER_COMPONENTS` for a profile)
   - the one-time `WORKER_TOKEN[_<FAMILY>]`
   - `MODEL_CACHE_DIR`
   - `WORKER_PREPARE[_<FAMILY>]`
   - `APP_ENV=prod`
   - resolved worker secrets

   The worker row is `provisioning`, with `provisioned_at` set and a label `ce-worker-<worker_id>`.
2. The provider reports `running`, so the job stage shows **booting**. The image is still pulling or the entrypoint is starting.
3. `python -m ce_worker` runs its startup checks. It refuses to start when:
   - `MOCK_GPU` is set in production;
   - a mock adapter is installed;
   - `WORKER_TOKEN` is missing.

   It then registers, and retries every 2 s until the scheduler answers. The row becomes `idle` and the cold start (registration − provision) is recorded.
4. It runs `clean_staging()`, then `check_cache()`. Each declared model is `installed` or `not_installed` according to what the disk already holds.
5. **Prewarm** (`WORKER_PREPARE`, which a profile sets from its `prepare` list): fetch → verify → load into VRAM, in the background. The lease loop keeps running.

   States per model: `not_installed → downloading → verifying → installed → loading → ready`, or `failed` with the error. `ready` means it is loaded in GPU memory.
6. The first job finds the models `ready`. Without prewarm, the first job pays the cost of fetching and loading, and its stage shows it.

Cold start for the talking-head profile:

- **Empty disk:** download about 93.7 GB (declared, required models), then load. Manifest estimates: InfiniteTalk 180 s, each Chatterbox 30 s.
- **Disk or volume already holds the cache:** only the load.

Download time depends on the host's bandwidth. It was **not measured** here.

---

## F. Job lifecycle (what happens on Generate)

1. The web calls the API. The API starts the job workflow (Temporal).
2. The planner splits it into nodes. Each GPU node is enqueued as a `gpu_task` with:
   - `capability`
   - `adapter_id`
   - `model_key`
   - `vram_gb`
   - an estimated duration
   - a seed
3. **Budget holds** run on every fleet tick:
   - `budget_daily`: low-priority tasks while the projected daily spend is above `BUDGET_DAILY_USD`;
   - `budget_video` / `budget_project`: every task of a video or project whose spend reached its `budget_usd`.

   Held tasks show **"Held by a budget"** and the reason.
4. **Scale up.** No worker of the family is up, so the stage shows **"Waiting for a GPU"**. The autoscaler or the operator then provisions one ("Renting a GPU", then "GPU booting").
5. **Lease.** A worker of the family leases the task. Placement prefers workers that already hold the model resident or cached (`resident_models`, `cached_models`, a stickiness window of 120 s).
6. **Run.** The worker reports a phase in each heartbeat, and the stage follows it:

   | Worker phase | Stage |
   |---|---|
   | `fetching_model` | `downloading_model` |
   | `verifying_model` | `verifying` |
   | `loading_model` | `loading_model` |
   | `generating` | `generating` |
   | `uploading` | `uploading` |

   Inputs come from presigned GETs. Outputs go to presigned PUT slots (6, plus more on request).
7. **Complete.** The scheduler verifies the outputs and records the assets and the GPU seconds and cost. The workflow continues: post-processing and the final MP4, which goes to object storage. The web plays the MP4 from a presigned URL.
8. **Failure.** The error class decides what happens next:
   - `retryable`, `timeout`, `oom` (escalating the VRAM class): requeued under a new attempt with the same seed, up to 3.
   - `fatal`: the node fails and the job shows the error. **A real model that is unavailable never becomes a placeholder output.**

The stage resolver (`ce_api/job_stages.py`) reads these records only. A stage it cannot determine is not shown.

---

## G. GPU lifecycle

Worker states:

| State | Meaning |
|---|---|
| `provisioning` | Rented, not yet registered |
| `idle` | Registered, no task |
| `busy` | Running a task |
| `draining` | Finishing its task before release |
| `stopped` | The instance is stopped: its disk is kept and storage is still billed |
| `offline` | No heartbeat |
| `failed` | Did not register in time, lost by its provider, or stale |
| `terminated` | The instance is destroyed |

Rows in `stopped`, `failed` and `terminated` stay visible on the GPU page, with the `all` scope, until you hide them.

| Transition | Trigger | Notes |
|---|---|---|
| → provisioning | `workers:provision`, `profiles/{id}:provision`, or autoscale | Paid providers need `allow_paid` (owner-approved, noted), a price within `max_price_per_hour_usd`, and room in the daily budget. |
| provisioning → failed (+ terminate) | Not registered within 900 s, or the provider reports failed or gone | Its provisioned time goes to `fleet_costs`. |
| idle/busy → failed (+ terminate) | No heartbeat for `worker_stale_s` (120 s) | The reaper marks it, and the fleet terminates the instance so it stops billing. For a colocated profile the shared instance is terminated only when no sibling still works. |
| idle → stopped | `:stop` with action `stop`, or scale-down with `idle_action: stop` | The disk and model cache are kept. Storage is still billed. |
| stopped → provisioning → idle | `:start` (or autoscale restarts a stopped worker before renting a new one) | The enrollment token is re-armed. Vast may wait in "scheduling" until the machine's GPU is free. |
| idle/busy → provisioning → idle | `:restart` (Vast reboot) | The token is re-armed. The disk survives. |
| any → terminated | `:stop` with action `terminate` (the web asks you to type the worker id), scale-down, or the reaper | Final. The container disk is gone, but a linked volume survives. |
| (none) → adopted | Reconciliation finds a `ce-worker-<id>` instance whose rental outcome was unknown | It is bound to its row, so the scheduler never rents a second one. |
| (none) → orphan | A labeled instance that has no live row | Listed under Orphans. Terminate it from the web, with confirmation. |

Reconciliation runs every 120 s, and on demand with `:refresh`. It compares each row with the provider (`provider_status`, `provider_checked_at`):

- Gone outside the platform → `terminated`.
- Stopped outside the platform → `stopped`.

---

## H. Persistent storage (cutover §18)

| Data | Where | Lifetime | Safe to discard? |
|---|---|---|---|
| User uploads, references, voices | Object storage | Permanent | **No** |
| Generated outputs (clips, audio, final MP4) | Object storage, written through presigned PUTs and verified by the scheduler | Permanent | **No** |
| Model cache (`/models`) | Instance container disk, or a linked Vast volume | Disk: until terminate. Volume: until the volume is deleted. | Yes. It can be rebuilt from pinned sources. |
| Staging (`*.partial-*`) | Inside the cache | One fetch | Yes. Removed on failure and at boot. |
| Task scratch | Instance disk | One task | Yes. Removed after each task. |
| Worker telemetry and model states | Postgres (`gpu_workers.telemetry`, `model_states`) | The latest values | Yes |

**Nothing the user cannot recreate depends on the GPU instance.**

**What survives what:**

- **Stop/start:** the container disk is kept, so the cache survives. Vast still bills storage while the instance is stopped, and starting again depends on the machine's GPU being free.
- **Restart (reboot):** the disk survives.
- **Terminate:** the container disk is lost.
  - Without a volume, the next instance downloads the cache again: about 94 GB for the talking-head profile.
  - With a volume, it keeps the cache.

**The chosen production configuration:**

1. **Default: container disk, sized by the profile (190 GB).** For short-lived or experimental capacity, use **stop** rather than terminate when the same machine will be used again soon.
2. **Recommended for steady production** (`persistent_cache: recommended`):
   - Create a Vast **local volume** of at least 110 GB on the chosen machine. That covers 96.65 GB of models, 14.5 GB of staging and the 5 GB reserve, so 120 GB gives headroom.
   - Set the provider's `storage.volume: {volume_id, machine_id, mount_path: /models}`.
   - The provider then links it (`volume_info`, `create_new: false`), and the search is limited to that machine, because a local volume lives on one machine.
   - Trade-off: you depend on that machine's availability.
   - Vast network volumes exist but are **not** used here.
3. **Restore.** The cache is restored by itself. A worker on a disk or volume that holds `index.json` finds the entries in `check_cache()` and does not download them again. No copy step is needed.

Creating the volume is a one-time step on Vast's console or CLI (section M). The platform does not create or delete volumes.

---

## I. Web operations

Everything here is on the **GPU** page, platform admin only, except where noted.

- **Providers.**
  - Create a provider: kind, name, credential reference, config overrides, daily budget.
  - Edit, **Test** (health and a read-only offer search; nothing is rented), **Approve spending** (requires a note, audited), and **Delete** (you type the provider name).
  - Secret values are never shown. Only `env:` and `file:` references are.
- **Offers.** A read-only marketplace search per class and region, with prices.
- **Model profiles.** Each profile card shows:
  - the computed disk and VRAM, with the breakdown;
  - each model's declared size, source at revision, and license;
  - whether a model is prepared at boot or optional.

  **Provision this profile** requires choosing a provider. A paid provider also requires ticking the acknowledgement: "billing starts now and continues until stopped or terminated".
- **Provision.** Choose a family, variant, class, region and provider. A mock provider is offered only in non-production environments, and never by default.
- **Workers.** Each worker shows:
  - provider and worker state;
  - class and price;
  - **provider telemetry, worker telemetry and scheduler state, labeled separately**: GPU utilization, VRAM used and free, disk used and free, cache size;
  - model states with bytes, speed and ETA;
  - the current task and the last error.

  A value the worker did not report shows as unavailable, never as `0`.

  Actions: **Refresh**, **Start**, **Stop**, **Restart**, **Terminate** (you type the worker id), **Prepare models** (all or selected, optionally warm), and **Cancel prepare**.
- **Orphans.** Labeled instances with no live row: their state and price, with **Terminate** (confirmation required).
- **Enrollment.** Issues a one-time token for a self-managed host.
- **Queue.** The live GPU queue, including held tasks and their reasons.
- **Jobs page** (all members): each GPU node shows its stage, its label, and download progress where there is one.
- **Dashboard:** spend today and the budgets, linking to the GPU page.

Every paid action is recorded in the audit log: approve spending, provision, start, restart, stop/terminate, and orphan terminate.

---

## J. Troubleshooting and recovery (cutover §17)

### The 14 recovery cases

| # | Case | What happens | Re-rent? | Re-download? |
|---|---|---|---|---|
| 1 | The worker process restarts but the cache remains | The first registration spends the one-time enrollment token. The worker then stores its worker token at `<MODEL_CACHE_DIR>/.ce-worker/<WORKER_ID>.json` (mode 0600, keyed by worker id; override with `WORKER_CREDENTIAL_FILE`). A process restarted inside the instance resumes with that token while the row is still live, i.e. within `worker_stale_s`, 120 s. `check_cache` then finds the models installed. If the scheduler refuses the token (the row was failed or terminated), the file is deleted. **From the web**, `:restart` re-arms the enrollment token and the worker registers again. A process that stays down longer than 120 s is failed and its instance terminated, and the task is retried elsewhere. | No | No (a replacement after 120 s without a volume: yes) |
| 2 | The Vast instance is stopped and started | `:start` re-arms the token. Vast may sit in "scheduling" (shown as provisioning) until the GPU is free. The disk and cache are intact. | No | No |
| 3 | The Vast instance restarts and the worker reconnects | `:restart` → Vast reboot. Same as case 2. | No | No |
| 4 | The scheduler restarts while a worker exists | Worker tokens are checked against the database. Leases continue. A lease that ran out during the outage is requeued with the same seed. The fleet loop resumes under leader election. | No | No |
| 5 | The API restarts while a job exists | The API is stateless. The workflow and queue rows persist. | No | No |
| 6 | A model download is interrupted | Staging is removed. Completed entries are kept. The interrupted entry starts again from zero (no byte-range resume). A retry, or **Prepare** from the web, continues. | No | Only the interrupted entry |
| 7 | A model checksum fails | Manifest pin mismatch: `fatal`, the model state is `failed` with the file named, and nothing is committed. LFS mismatch: `retryable`. Fix the manifest pin or the source, then **Prepare**. | No | That entry |
| 8 | The disk becomes too small | `NotEnoughDiskError` names the bytes needed and free. Eviction frees unpinned LRU entries first. Fix it by provisioning the profile (its disk is computed) or by raising `storage.disk_gb`. | Bigger disk needed | — |
| 9 | The GPU has insufficient VRAM | An `oom` failure is retried on the next bigger VRAM class. Profiles refuse to provision on a class smaller than `vram_min_gb` (`fits: false`). | Maybe, on a bigger class | No |
| 10 | The Vast offer disappears during provisioning | A 4xx saying the offer is unavailable is treated as "taken". The next offer is tried, up to `rent_attempts`, then the next class or provider. `NoCapacityError` is shown. | No double rental | — |
| 11 | A Vast API request times out after the rental may have succeeded | `ProvisionOutcomeUnknown` keeps the row (it is not discarded). Reconciliation finds the instance by its `ce-worker-<id>` label and adopts it, or confirms there is none. The autoscaler does not rent another while the row is pending. | **No duplicate** | — |
| 12 | The worker heartbeat stops | The lease is reaped after 30 s and the task requeued. The worker is marked failed after 120 s and its instance terminated. The autoscaler provisions a replacement if backlog remains. | Replacement | Without a volume, yes |
| 13 | Generation crashes after the model loaded | The error is classified. `fatal` fails the node with the message shown. Infrastructure classes are retried with the same seed. The models stay loaded or cached on that worker. | No | No |
| 14 | Upload fails after generation | `ArtifactIOError` is `retryable`: the task is retried. Outputs are not accepted until the scheduler has verified every object. | No | No |

Workers started outside the fleet (compose, self-managed hosts without `WORKER_ID`) store no token, so replicas that share a volume never share a worker identity. They register again with their shared token or a new enrollment token.

Mechanisms covered by tests:

- `packages/py/ce_worker/tests/test_model_ops.py::test_a_restarted_process_resumes_with_its_stored_worker_token` (case 1).
- `apps/scheduler/tests/test_fleet.py`: provisioning watch, reconcile, adopt, orphans, outcome unknown, start/restart re-arm, siblings.
- `packages/py/ce_worker/tests/test_model_ops.py`: cache reuse, space, staging cleanup, checksum failures, progress, prepare and cancel.
- `apps/api/tests/test_job_stages.py`
- `plugins/providers/gpu/vast/tests/test_vast.py`: labels, list, reboot, timeouts → `ProvisionOutcomeUnknown`.
- The queue reaper tests.

### Symptoms

| Symptom | Look at | Likely cause and fix |
|---|---|---|
| **Worker offline** | Workers: last heartbeat, provider state | Host partition or a crashed process. The fleet terminates it after 120 s. **Restart** if the provider shows it running. |
| **Stuck provisioning** | Provider state (`scheduling`, `loading`), elapsed time | Image pull (large CUDA layers), Vast waiting for a GPU, a wrong `SCHEDULER_URL`. It is failed and terminated after 900 s. Check that `scheduler.public_url` is reachable from the internet. |
| **Cache missing after re-provision** | Model states `not_installed` | Expected after terminate without a volume. Use a volume (section H) or stop instead of terminate. |
| **Slow download** | Model state speed (MB/s) and ETA | Host bandwidth, or Hugging Face throttling (set `HF_TOKEN`). Raise `MODEL_FETCH_CONCURRENCY` with care (default 4). Prefer hosts with more download bandwidth (Vast reports `inet_down` per offer; the search does not filter on it yet). |
| **Interrupted download** | Model state `failed` or `not_installed` with a note | Press **Prepare**. Completed entries are kept. |
| **Checksum failure** | Model state `failed`, with the file named | Pin mismatch: correct the manifest (the revision or file changed). LFS mismatch: retry. |
| **Insufficient disk** | `NotEnoughDiskError` text, disk telemetry | Provision via the profile (computed disk) or raise `storage.disk_gb`. Remove unused models. |
| **Insufficient VRAM** | `oom` failures, profile `fits` | Use a bigger class, or a profile whose models fit together. |
| **Vast capacity** | `NoCapacityError`, Offers | Widen regions or classes, raise the price ceiling (within budget), or retry later. |
| **Scheduler unreachable** | Worker logs "scheduler unreachable" | It backs off up to 30 s and recovers. Check DNS, TLS and firewall for `scheduler.public_url`. |
| **Object storage unreachable** | `ArtifactIOError` (retried) | `S3_WORKER_ENDPOINT_URL` must be public HTTPS and its TLS valid. Presigned URLs expire (`presign_ttl_s`). |
| **Generation failure** | Job page: node error, error class | `fatal`: an adapter or input problem, with the message shown. Never replaced by a placeholder. |

---

## K. Cost controls

- **Hourly price ceiling:** `max_price_per_hour_usd` per provider (default 1.0 for a new provider). Offers above it are not rented.
- **Daily budget:**
  - Global `BUDGET_DAILY_USD`, plus a per-provider `budget_daily_usd`.
  - Projected spend = spent today + the running fleet's burn rate × `spend_horizon_h` (1 h).
  - Above the budget, low-priority tasks are held and scale-up stops.
  - A `budget_alert` notification is sent once per hour per scope.
- **Per-project and per-video budgets:** `budget_usd`. Once it is reached, every queued task of that project or video is held.
- **Paid spending is off until approved.** `allow_paid` requires an owner-approved change with a note. **A paid provider is never replaced by a mock in production.**
- **When billing continues:**
  - while an instance is running, idle or busy;
  - **while it is stopped**: Vast bills storage for a stopped instance's disk, and a volume is billed until it is deleted.
  - Billing ends only at **terminate**, plus volume deletion.
- **Stop vs terminate:**
  - **Stop** keeps the cache and saves a ~94 GB download on the next start, at the cost of storage billing and a possible wait for the machine's GPU.
  - **Terminate** ends billing and loses the container disk.
  - Idle scale-down follows each pool's `idle_timeout_s` and `idle_action`.

Costs are recorded per worker in `fleet_costs` (provisioned time × price) and per task (GPU seconds).

---

## L. Upgrade policy

- **Model revision.** Change the manifest's pinned `revision`, `size_gb` and checksums in one reviewed change. The new revision gets a new cache path. The old entry stays until evicted.
  - The adapter returns to `sandbox` until it passes the gates again.
  - **Never** use a floating `main`.
- **Upstream code.** Bump the pinned commit in `families.yaml`, regenerate the Dockerfiles, run `worker-images`, then point the provider at the new **immutable** tag.
- **Promotion.** A model serves production only after:
  - a GPU smoke run;
  - a golden comparison;
  - an admin promotion through the documented gates.

  Never edit `validation:` or `status:` by hand. Chatterbox and InfiniteTalk are still `sandbox`.
- **Real people.** A likeness or voice of a real, identifiable person needs the consent and digital-twin path. It is **disabled in V1** (`digital_twins_enabled: false`). Uploads require one of these attestations:
  - `not_a_real_person`
  - `no_identifiable_people_rights_held`
  - `synthetic_voice_not_a_person`

---

## M. Verification and emergency CLI

**Normal operation needs no terminal and no SSH.** Everything in sections I–K happens in the web UI. The commands below are for **bootstrap, verification and emergencies only**.

```bash
# Bootstrap (once per deployment)
make migrate
uv run ce admin bootstrap --org-name "<Org name>" --email owner@example.com --name "<Owner>"   # first real org + owner (platform admin)
uv run ce data purge-demo            # dry run: lists demo-org rows (seeded data), deletes nothing
uv run ce data purge-demo --apply    # deletes them (demo orgs are refused at login in production anyway)

# One-time persistent volume (optional, section H) — Vast's own CLI, outside the platform
# illustrative: check `vastai create volume --help` for the current syntax before running
vastai search volumes '<filters>'   # pick a volume offer on the machine you will rent
vastai create volume <volume_offer_id> -s 120 -n ce-models

# Image publishing (once per upstream bump): GitHub Actions → worker-images → targets=profile:talking_head
```

### Before the first live run (not yet done)

1. Set `VAST_API_KEY` in the scheduler's environment and create the provider with `credentials_ref: env:VAST_API_KEY`. Press **Test**: a health check and a read-only offer search.
2. Publish `profile:talking_head` and set the provider image to the immutable tag. Record the measured image size into `image_gb`.
3. Set `S3_WORKER_ENDPOINT_URL` and `scheduler.public_url` to public HTTPS endpoints. The startup check must show no warnings.
4. Approve spending with a low daily budget and a low `max_price_per_hour_usd`.
5. **Provision this profile.** Watch it go provisioning → booting → registered → downloading → ready. Record the real download time and the bytes per model.
6. Generate one talking-head clip. Then **Terminate**, and confirm on Vast's console that no instance is left.
7. Promote the adapters through the gates, with those measurements recorded.

In an emergency, Vast's own console or `vastai destroy instance <id>` stops billing for an instance the platform cannot reach. Reconciliation then marks its row `terminated`.

---

## N. Sources (checked 2026-10-08)

| Source | What it was used for |
|---|---|
| `vastai` 1.8.3 (PyPI wheel sha256 `8c50b291dd92feecc4e0a7eb57b365afe6f9ab719c4d6b4f681d0af367757010`; github.com/vast-ai/vast-cli) | Instance create body (`build_create_instance_payload`), `runtype`, `cancel_unavail`, `image_login`, `volume_info`, stop/start semantics (stopped data preserved, start subject to availability), reboot `PUT /api/v0/instances/reboot/{id}/`, the paged instance list, local and network volumes. `docs.vast.ai` was blocked by the egress proxy, so the official client source is the reference. |
| `huggingface_hub` 2.2.0 (wheel sha256 `1667f145dc56dc210d60966069397df9ecfca9607a5d43db88b308c89dae56b3`) | `?blobs=true` listing, with sizes and `lfs.sha256` per file (`model_info(files_metadata=True)`). `huggingface.co` itself was blocked here. |
| github.com/MeiGen-AI/InfiniteTalk README (main, 2026-10-08) | torch 2.4.1 / cu121, xformers 0.0.28, flash_attn 2.7.4.post1, the weights list, the wav2vec2 `refs/pr/1` safetensors, LoRAs, the low-VRAM flag |
| github.com/resemble-ai/chatterbox README + pyproject (master, 2026-10-08) | chatterbox-tts 0.1.7, Python ≥ 3.10 (tested on 3.11), torch 2.6.0, transformers 5.2.0, Perth watermark; models Turbo, Multilingual V3, English, Nano |
| Repository manifests (`plugins/**/plugin.yaml`), `config/gpu/{profiles,families,pools,variants}.yaml` at this commit | Model registry, declared sizes and VRAM, pinned revisions, the profile |
