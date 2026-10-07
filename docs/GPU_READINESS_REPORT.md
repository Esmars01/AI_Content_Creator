# GPU Readiness Report — AI Creator Engine

**Date:** 2026-10-06 · **Scope:** the corrected workspace (`repo/creator-engine/`, tag `phase-14-audit` in
`bundles/creator-engine-phase14.bundle`) · **Companion:** [`FINAL_AUDIT_AND_FIX_REPORT.md`](FINAL_AUDIT_AND_FIX_REPORT.md)

> **No GPU was available in this audit, and no GPU code path has ever executed on a GPU in this project.**
> Nothing below claims GPU validation. Every statement is labelled with what actually proved it.

## 1. Verdict

> **Update 2026-10-07, after the product-level audit
> ([`PRODUCT_LOGIC_AUDIT_REPORT.md`](PRODUCT_LOGIC_AUDIT_REPORT.md)): YELLOW, go for GPU testing with a known
> list.** The audit ran every user journey a GPU run depends on end to end in mock mode, and verified it on the
> produced files:
> - planning, approval and generation, with cancel, resume and recovery from a worker kill;
> - scoped edits that change only their scope;
> - new creators whose voice and look reach the video.
>
> Two of its findings matter for GPU sessions:
> - `pitch_semitones` (a cast voice offset) is honoured only by the mock voice (PITCH-REAL). Check it first with
>   a real TTS.
> - `post.realism` encodes are not byte-deterministic (REALISM-ABR). Compare GPU re-runs with PSNR/SSIM, not SHA.
>
> Identity, acting, take variety and voice quality cannot be judged on mock output (report section 25).

**Ready to *start* real GPU testing; not GPU-validated.** The control plane that GPU workers depend on —
the scheduler, leases, completion back to Temporal, worker lifecycle, presigned I/O, the fleet manager — was
audited, and the defects that would have broken or silently stalled the first GPU runs were fixed and
regression-tested on CPU stand-ins (section 4). The adapters, their CUDA images and every model are
unvalidated (section 3). The first GPU session should follow section 6 in order.

## 2. Validation levels used in this report

| Label | Meaning |
| --- | --- |
| **MOCK VALIDATED** | Exercised end to end with the mock adapters (real media produced on CPU by FFmpeg/numpy), in tests and in measured runs on this host |
| **CPU/STAND-IN VALIDATED** | The real code path ran, with a CPU stand-in for the GPU part (fake adapter, `--backend test`, mock provider), in automated tests |
| **REAL GPU VALIDATED** | Ran on a GPU with the real model. **Nothing in this project is at this level.** |
| **EXTERNAL PROVIDER VALIDATED** | Called a real paid/hosted provider. **Nothing is at this level** (no paid GPU provider, no hosted LLM was ever called) |
| **NOT VALIDATED** | Code exists; nothing has exercised it beyond static checks |

## 3. What exists for GPUs, and its level

| Part | Level | Evidence / notes |
| --- | --- | --- |
| 29 GPU adapters in 9 runtime families (`wan`, `tts`, `asr`, `audio`, `image`, `lipsync`, `post`, `vision`, `vllm`), every manifest `validation: untested_on_gpu` | CPU/STAND-IN (contract tests only) | Adapter contract suites run on CPU stand-ins (Phase 8). The router excludes `untested_on_gpu` routes from production profiles, so no user job can land on one by accident. Minimum VRAM declared 1–80 GB (`wan22_a14b_*` 48 GB, `vlm_qwen38_27b` 80 GB, `qwen_image_edit` 48 GB, `infinitetalk` 24 GB). |
| GPU family images (`infra/docker/worker-<family>.Dockerfile`, generated from `families.yaml`) | NOT VALIDATED | Lint-checked only. No image was ever built with its CUDA stack; several torch pins are marked `[RV]` (chosen from a range). This audit could not build any image either (`ghcr.io` blob host 403 in this sandbox). |
| Worker runtime (`ce_worker`): register, lease, heartbeat, presigned download/upload, model cache, error classes | CPU/STAND-IN + MOCK | Unit tests; every mock build runs through it (worker-cpu). |
| Scheduler: queue, leases, reaper, fair share, OOM VRAM-class escalation, completion to Temporal | CPU/STAND-IN + MOCK | `apps/scheduler/tests`, load test of Phase 14 (simulated workers), every mock build. |
| Fleet manager + providers (mock, local, local-docker, RunPod, example-cloud) | CPU/STAND-IN | Mock providers in tests; `local_docker` lifecycle against the real Docker daemon (no GPU); **RunPod never called**. |
| Model cache (HF / URL fetchers, pins, sha256 verification, LRU eviction) | CPU/STAND-IN | Unit tests with fake fetchers; Hugging Face is unreachable from this sandbox. |
| Smoke / bench / calibrate tooling (`make smoke-gpu`, `make bench`, `make calibrate`) | CPU/STAND-IN (dry run `--backend test`) | Never produced evidence; by design a dry run cannot record any. |
| Real watermarks (VideoSeal, AudioSeal) | NOT VALIDATED | Production startup refuses without production-validated watermark adapters (invariant I11); dev uses `mock_dev`. |
| CPU post on the GPU path (camera, realism, acoustics, captions, mix, render, provenance) | MOCK VALIDATED | Identical code on CPU in every mode; measured below. |

## 4. GPU-path defects found and fixed in this audit (all CPU/STAND-IN validated)

These would have surfaced only once real, long-running GPU work arrived. IDs refer to the main report.

| ID | What would have happened on real GPUs | Fix | Regression test |
| --- | --- | --- | --- |
| W1 | A Temporal blip while a worker reported its result: the task row was already `succeeded`, the result was dropped (every RPC error counted as "activity gone"), and the node hung until its 2-hour dispatch timeout while the org was billed | Only NOT_FOUND / INVALID_ARGUMENT / FAILED_PRECONDITION / ALREADY_EXISTS mean gone; anything else keeps the result on the task (`payload.completion_pending`) and the leader redelivers it (`completions` loop, metric `ce_gpu_completions_deferred_total`) | `test_a_temporal_outage_delays_results_instead_of_losing_them`, `test_only_permanent_rpc_errors_mean_the_activity_is_gone` |
| W2 | A Temporal restart of more than ~10 s cancelled **every** queued and running GPU task | An unavailable Temporal makes the cancellation probe skip its cycle | `test_an_unavailable_temporal_cancels_no_task` |
| W3 | A deploy, scale-down or spot preemption (SIGTERM) permanently failed the user's node (`cancelled` is final) | Shutdown is reported `retryable`: the same seed reruns on another worker | `test_a_worker_shutdown_is_an_infrastructure_retry` |
| W4 | Inputs and upload slots are presigned once at lease time with the browser TTL (15 min): a model download + load + long render past 15 min failed every upload, and every retry repeated it | Worker URLs are signed for at least `model_node_timeout_s` + 10 min | config-level (startup) |
| W5 | Workers provisioned by the fleet got no `WORKER_VRAM_GB`, advertised 0 GB and could never lease a GPU task (the instance idled, billing, until the 2-hour timeout) | The scheduler uses the VRAM recorded for the instance when a worker reports 0; the worker detects device memory with `nvidia-smi` when the variable is unset | (logic in `Scheduler.lease`, `ce_worker.__main__.detect_vram_gb`) |
| W6 | After a Postgres restart or idle-connection kill, two scheduler replicas both ran the leader loops — two fleet managers, double provisioning of paid hosts | The leader checks its lock session every cycle and steps down when it is gone | `test_a_leader_that_lost_its_lock_connection_steps_down` |
| W7 | `/complete` waits for the scheduler to re-download and re-hash every output; at 90 s read timeout, large video outputs were discarded and retried forever; a 5xx was classed `fatal` | `/complete` read timeout 900 s; 5xx is `retryable` | `test_error_classes` |
| W8 | First fetch of tens of GB of weights hashed on the event loop that also heartbeats: the lease (30 s) expired mid-hash | Hash, remove and size in threads | existing model-cache tests |
| W9 | A crashed or partitioned fleet worker was marked `failed`, but its paid instance was never terminated | The fleet tick terminates instances of failed fleet workers once | `test_a_worker_the_reaper_failed_has_its_instance_terminated` |
| W12 | A cancelled task's ffmpeg kept running and competed with the next task | Subprocesses are killed on cancellation | (code review) |
| C1/C2 | Any failing bookkeeping activity (`fail_node`, the QC gate, `accept_outputs`, a render plan) failed the whole workflow without `complete_build`: job `running` and version `generating` forever, nothing reconciles them | The failing node or gate fails alone; every build workflow reaches `complete_build` | `test_a_failing_bookkeeping_activity_still_closes_the_build`, `test_a_render_whose_plan_fails_still_closes_its_job` |
| C5 | Local (CPU/render) activities never heartbeated: a cancelled build's ffmpeg kept running and wrote results after the cancel; a dead render worker was noticed only after 30 min | 10 s heartbeats, 60 s heartbeat timeout | existing cancellation workflow tests |
| D6 | The Compose services image did not contain any GPU adapter plugin: a GPU worker registering with the Compose scheduler had **all** its adapters dropped and never leased work (native mode installs everything, so it only showed in Compose) | The image installs every workspace plugin (manifest + adapter code; engines stay in the family images) | `tests/phase0/test_service_image_plugins.py` |

Also relevant: workers can now run several tasks at once (`WORKER_CONCURRENCY`, default 1). **Keep 1 on GPU
workers**: the scheduler places by the VRAM a worker reports, and VRAM is not yet accounted per running task
(R3 below).

## 5. Remaining GPU risks (not fixed; none blocks the first smoke run)

| # | Risk | Likely symptom on a GPU host | What resolves it |
| --- | --- | --- | --- |
| R1 | **No adapter has run on a GPU.** Contract tests prove request/response shapes only | Import errors, wrong tensor shapes, missing weights, CUDA/torch mismatches on first load | The smoke runs of section 6 |
| R2 | **Family images never built with CUDA**; some torch pins `[RV]` | Build failures (flash-attn / apex compile), wheel resolution errors, driver too old for CUDA 12.8/12.9 | Build each variant on the GPU host first; pin what works |
| R3 | **VRAM is not accounted**: the worker reports its static total (`free_vram_gb = vram_gb`) and keeps every loaded adapter resident; nothing unloads on OOM | The second large model on one GPU OOMs; after an OOM the same worker OOMs again | Report real free VRAM (NVML / `torch.cuda.mem_get_info`), unload LRU adapters before loading and after an OOM |
| R4 | **OOM escalation vs fleet**: an OOM raises the task's VRAM class, but the fleet's backlog groups by adapter only and may provision the small class again | An escalated task waits up to the 2 h dispatch timeout | Include the max VRAM class per adapter in the backlog; fail fast when no class qualifies |
| R5 | **Cancellation does not stop GPU kernels**: real engines run in threads (`asyncio.to_thread`); cancelling the coroutine leaves the thread running | After a cancel or lost lease the next task starts while the previous kernel still holds VRAM | Run engines in a killable subprocess, or wait for the thread before leasing again |
| R6 | **Fleet workers use a one-time enrollment token**: a restarted fleet container cannot re-register (W9 now terminates the failed instance, so the cost leak is closed, but the worker is lost) | A transient restart costs a full re-provision | A renewable credential for provisioned workers |
| R7 | **Billing trusts the worker**: `busy_seconds` (and, for self-managed workers, the price) come from the worker | A compromised or misconfigured self-managed host can mis-bill a tenant | Compute busy time server-side (lease start → completion) |
| R8 | `dispatch_gpu` has no retry and a 2 h start-to-close that **includes queue time** | A long backlog times out tasks that never started | Separate enqueue retry; a heartbeat timeout fed by the scheduler's probe |
| R9 | Long model loads: the lease (30 s, extended by heartbeats every 10 s) is fine while the worker loop is free; any other blocking call in an adapter's `load()` would lose it | Lease lost during a first load | Keep `load()` async or in a thread (adapter review on the GPU host) |
| R10 | Remote workers need presigned URLs for an object-store endpoint they can reach; the scheduler signs with the **internal** endpoint (documentation said "public") | A split GPU worker cannot download inputs or upload outputs | Point the scheduler's S3 endpoint at an address the workers reach, or add a worker-specific endpoint setting (not done) |

## 6. First real GPU session — what to do, in order

Prerequisites: one NVIDIA GPU host with a driver for CUDA 12.8 (12.9 for vLLM), Docker with the NVIDIA
container toolkit, outbound access to Hugging Face and the upstream Git repositories, and the owner's explicit
approval of any paid provider spend (`docs/GPU_VALIDATION.md` in the phase bundles).

1. **Control plane first, mock workers:** `make dev` on the control-plane host (images now rebuilt on every
   start; set `CE_BIND_ADDRESS=0.0.0.0` so remote workers reach the scheduler and the object store). Confirm a
   mock build is `ready` (the measured reference on 4 vCPU: previz ≈ 7–8 s, generation ≈ 110–140 s).
2. **Object-store reachability (R10):** from the GPU host, `curl` a presigned URL issued by the scheduler.
3. **Build one family image** (start with a small one: `post` → `rife_425`, 4 GB; or `asr` → `qwen3_asr`, 8 GB).
4. **Register one GPU worker** with `WORKER_VRAM_GB` set (or rely on `nvidia-smi` detection) and
   `WORKER_CONCURRENCY=1`; check the scheduler lists all its adapters (D6).
5. **Smoke run** that adapter: `make smoke-gpu PLUGIN=<id> ARGS="--fetch --record"` — this is the first
   evidence ever recorded; then `make bench PLUGIN=<id>` for timings and peak VRAM.
6. **Failure drills on that worker** (each is now expected to recover): stop the worker mid-task (expect an
   infra retry with the same seed), restart Temporal while a task runs (expect delayed, not lost, completion),
   kill the scheduler leader's DB session (expect one leader).
7. Only then the large families (`wan` 24–48 GB, `vllm` 48–80 GB), one adapter at a time, watching R3–R5.
8. Promote an adapter only through the documented promotion gates (bench evidence + golden set), never by
   editing `validation:` by hand.

## 7. Likely bottlenecks once GPUs are real

- **Model initialization**: multi-GB downloads and loads on a fresh worker (manifests declare `load_seconds`
  up to 300 s for Wan 2.2 A14B); keep workers warm (sticky placement already prefers resident models).
- **The CPU post stays on CPU**: on 4 vCPU the render queue (camera, realism at 1080×1920, final render) is
  already the critical path of a mock build — about 175 s of FFmpeg work across 2 render slots that saturate
  the CPU (measured in this audit). With real GPU generation upstream, size the render workers' CPUs
  accordingly; the GPU does not speed this part up.
- **Output verification**: the scheduler re-downloads and re-hashes each output before acknowledging it (W7);
  for long 1080p takes this adds real seconds per task and network traffic.
