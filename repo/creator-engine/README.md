# AI Creator Engine

A self-hostable system that plans, generates and edits short videos featuring persistent synthetic creators — each with their own DNA, memory, voice, wardrobe and recurring worlds — through a model-independent behavior layer, an incremental build graph and honest requested/compiled/observed reporting.

The full specification is [`docs/MASTER_BUILD_PROMPT.md`](docs/MASTER_BUILD_PROMPT.md). Decisions are in [`docs/DECISIONS.md`](docs/DECISIONS.md), progress in [`ROADMAP.md`](ROADMAP.md) and [`docs/progress/`](docs/progress/).

## Status

| Phase | Status |
| --- | --- |
| 0–7 | complete — milestone **M1**: the full product in mock mode plus the real CPU engines |
| 8 | complete — real GPU adapters exist as code and pass their contract tests on CPU stand-ins; **none has run on a GPU** (`validation: untested_on_gpu`) |
| 9 | complete — fleet manager and provider drivers, tested against mock providers; **no paid provider was ever called** |
| 10 | complete — Creator Studio and World Studio, in mock mode |
| 11 | complete — QC gate, Performance QA, continuity, consistency, Creative Director critique, benchmarks, in mock mode |
| 12 | complete — memory loop, persistent research and the claim ledger, templates, brand kits, caption translation, packaging and exports, in mock mode |
| 13 | **intentionally skipped** by the product owner (ADR 0050): no Phase 13 exists |
| 14 | complete — observability, scheduler load test, security review, backup/restore, deployment and operations docs, native mode, demo |

What has actually been validated: everything above in **mock mode** (mock engines produce real media on CPU; the
Director runs on authored LLM fixtures), and the **real CPU engines** (Kokoro, faster-whisper, MediaPipe, the
prosody analyzer, DINOv2, AuraFace, DNSMOS, PP-OCR, C2PA) in Phase 7 on a host that had their assets. **Not
validated:** any GPU model (milestone M2 is not reached), any paid GPU provider, any hosted LLM, a real
text-embedding model (none is registered), production startup (it needs production-validated watermark
adapters, which do not exist yet). Phase reports with the exact commands and results: `docs/progress/`.

In mock mode you sign in to the web app at <http://localhost:3000>, describe a video in a sentence ("Create a 30-second TikTok explaining why most people misunderstand AI agents"), review the Director's plan and its previz (script, storyboard, intent, a measured performance timeline, predicted behavior coverage, memory, claims and findings), approve it, watch the job progress and play the MP4 — with coverage badges that say "delivered" only for behaviors the analyzers confirmed. Edit it in words ("make him more skeptical"), lock what must stay, compare versions; save templates and a brand kit; translate captions; package it for a platform. Exports of dev renders are refused by design: they carry mock provenance.

| Works and is tested | Not built yet |
| --- | --- |
| Web app: sign-in, dashboard, projects, the Create wizard with previz review, Video Studio (player, live job progress, Performance lane, coverage badges, NL edit panel with proposal cards, Advanced performance and intent editors, locks, takes, versions tree with restore/branch/compare/duplicate, scene reorder, cancel and resume), Creator and World Studios (create, identity links, memory authoring), jobs, developer viewers | Keyboard shortcuts; a DOM caption overlay |
| Domain models, closed vocabularies, configuration, the database with every table and its invariants, object storage | Real invisible watermarks (VideoSeal/AudioSeal, Phase 8; `mock_dev` in dev, ADR 0047) |
| AI Director: stages 1–11, intent policies, situational acting, exact scripts, memory retrieval into pinned snapshots, repetition and contradiction checks, blocklists and the testimonial guard, research through an SSRF-guarded fetcher, previz and approval | Scene-scoped replans; world proposals from edits (Phase 10) |
| Incremental editing: natural-language and structured edits as proposals (operations, patch, impact with `no_visible_effect`, predicted coverage delta, alternatives, estimate), apply as a new version that rebuilds only dirty nodes, locks with route pinning, regenerate / re-route / take selection, restore, branch, duplicate, resume, compare, estimates (`docs/EDITING.md`) | Variants and remixes (V1) |
| API: auth, projects, creators, worlds, memory, uploads, events (SSE), planning (create, previz, intent, storyboard, replan, approve), jobs, build manifests, renders, behavior and coverage | — (Phase 12 added research sources, the claim ledger, spec templates, brand kits, caption translation, packaging and exports) |
| Plugin contracts and loader; mock engines for every capability producing real media on CPU; real CPU engines behind `CPU_REAL_ENGINES` (Kokoro TTS, faster-whisper, MediaPipe face/body, prosody, DINOv2, AuraFace, DNSMOS, PP-OCR, C2PA) | Real GPU adapters (Phase 8), GPU providers (Phase 9) |
| Behavior engine: CBS resolver, engine-agnostic compiler, translators that never drop an item, observation, the requested/compiled/observed triad with the 12 outcomes, Performance QA executed by the QC gate, measured mock profiles; Creator and World Studio (Phase 10) | — |
| Build graph with content-only cache keys, route and seed pinning; license policy and router; Temporal workflows, the GPU scheduler with leases and a CPU worker; FFmpeg rendering with captions (RTL, ASS/SRT/VTT), camera/lens/realism post, subject-aware reframing, per-scene room acoustics, loudness and true-peak targets; the exact-script loop; screen-recording analysis and zoom planning; C2PA signing and `GET /v1/renders/{id}/verify` | A registered real `embed.text` model (none could be pinned or license-verified here, ADR 0057) |
| QC gate (Phase 11): thresholds keyed by adapter, the VLM judge, the decision ladder with budgets (seed, fallback route, cheaper fix proposed, review), world continuity, creator consistency reports, the Creative Director critique with findings as edit proposals, the human rating queue and calibration, the benchmark runner with blind pairwise rating and bench-based promotion (`docs/QC.md`) | An LLM critic; real-engine thresholds (no GPU here) |

No GPU is needed for anything above, and no paid GPU is ever provisioned without the owner's explicit approval. Real CPU engines are optional: without their assets everything runs on the mocks (`CPU_REAL_ENGINES=auto`).

## Quick start (mock mode)

Prerequisites: Docker with Compose v2 (daemon running), [uv](https://docs.astral.sh/uv/), Python 3.12, Node 24 LTS, pnpm 10, FFmpeg with libass (HarfBuzz + FriBidi). `make bootstrap` checks them.

```bash
make bootstrap     # creates .env from .env.example, installs Python + JS workspaces, git hooks
make fetch-cpu-assets  # optional: real CPU engines (0.57 GB into .cache/models; mocks without it)
make dev           # infrastructure, web app, API, orchestrator, scheduler, render worker and worker-cpu
                   # (service images rebuilt on every start: they carry the code)
                   # (images built, migrated, buckets created, dev data seeded) — mock mode, plus the
                   # real CPU engines whose assets are present;
                   # `make infra-up` starts only the infrastructure (no service images)
make e2e-mock      # fixture spec → MP4 in .data/e2e/final.mp4 (< 3 min on CPU) with a coverage report
                   # (.data/e2e/coverage.json), cached rerun, text → previz → approve → video through
                   # the API, worker-kill check
make test          # spec check + all tests (infra tests skip with a reason if infra is down) + web unit tests
make test-e2e      # Playwright in mock mode against `make dev`: create → previz → approve → progress → play
make demo          # the product path through the HTTP API: research → plan → previz → build → edit →
                   # template → German captions → packaging → export refused (mock provenance)
make obs-up        # optional: Prometheus, Alertmanager, Grafana (:3001), Loki, Tempo, OTel collector
```

No service images (for example, no access to a Debian package mirror)? Run the services on the host instead:
`make infra-up && make dev-native` (then `make demo`, `make obs-native`; `make dev-native-down` to stop) —
`docs/DEPLOYMENT.md`.

`make dev` prints the dev login (`admin@creator-engine.local`) and, the first time, a generated
password (shown once; set `CE_SEED_ADMIN_PASSWORD` to choose it). Then:

- Web app: <http://localhost:3000> — guide in `docs/FRONTEND.md`
- API: <http://localhost:8000> — OpenAPI docs at `/docs`, the document at `/openapi.json`; guide in `docs/API.md`
- Temporal UI: <http://localhost:8080>
- S3 (SeaweedFS): `http://localhost:8333` with the dev keys from `.env`
- Postgres: `localhost:5432`, Valkey: `localhost:6379`
- Scheduler: <http://localhost:8100/healthz> (workers dial in here)

To make a video from text, use **Create** in the web app, or the API: sign in, create a project, then `POST /v1/projects/{project_id}/videos`
`{"input": "…"}` with an `Idempotency-Key` header; follow the job until `GET /v1/versions/{version_id}/previz`
says `previz_ready`, review it, and `POST /v1/versions/{version_id}:approve` (`docs/API.md`, `docs/DIRECTOR.md`).
Inputs without an LLM fixture are planned by the labeled template Director in dev.

To build the fixture video by hand: `uv run ce video generate-fixture --out .data/final.mp4` (watch it in the
Temporal UI; follow `GET /v1/jobs/{id}` or the SSE stream). Dev renders carry a burned
"MOCK PROVENANCE — NOT FOR DISTRIBUTION" label and are not exportable: their C2PA manifest is real (signed by a
throwaway, untrusted dev CA; `GET /v1/renders/{id}/verify`), but the invisible watermarks stay mocked until Phase 8.

Other useful targets: `make lint`, `make typecheck`, `make test-infra` (fails instead of skipping when infra is down), `make migrate`, `make seed`, `make gen-schema`, `make gen-client`, `make build-images`, `make dev-down`, `make infra-down`, `make infra-reset` (deletes local data), `make help`. Behind a TLS-intercepting proxy, pass its CA bundle to image builds: `make dev EXTRA_CA_BUNDLE=/path/to/ca.pem`.

GPU validation targets need a GPU host and name the adapter: `make smoke-gpu PLUGIN=…`, `make bench PLUGIN=…`, `make calibrate PLUGIN=…` (`docs/GPU_VALIDATION.md`). Operations: `make backup`, `make load-test`, `make gen-dashboards` (`docs/OPERATIONS.md`).

## Repository layout

```
apps/        web (Next.js), api, orchestrator, scheduler, gpu-worker, render-worker
packages/    py/ce_* shared Python packages · ts/api-client
plugins/     capability adapters by category (mock, voice, avatar, video, …)
config/      layered config, closed vocabularies, routing, QC, platforms, policy
infra/       compose/, docker/, observability/, k8s/
scripts/     bootstrap checks, spec verifier, asset fetcher, environment inspection
tests/       cross-service tests, infra smoke tests, invariants/
docs/        spec, ADRs, decisions, invariants, environment, progress reports
```

See §8 of the spec for the complete tree.

## Documentation

Start here: [`docs/SETUP.md`](docs/SETUP.md) (install and run), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (services and data flow), [`docs/DEMO.md`](docs/DEMO.md) (the demo, step by step).

- Product and domain: [`VIDEOSPEC`](docs/VIDEOSPEC.md), [`DIRECTOR`](docs/DIRECTOR.md), [`INTENT`](docs/INTENT.md), [`BEHAVIOR`](docs/BEHAVIOR.md), [`CREATORS`](docs/CREATORS.md), [`WORLDS`](docs/WORLDS.md), [`MEMORY`](docs/MEMORY.md), [`CONSISTENCY`](docs/CONSISTENCY.md), [`QC`](docs/QC.md), [`EDITING`](docs/EDITING.md), [`VOCABULARIES`](docs/VOCABULARIES.md), [`FRONTEND`](docs/FRONTEND.md), [`API`](docs/API.md).
- Engines: [`PLUGINS`](docs/PLUGINS.md), [`ADAPTERS`](docs/ADAPTERS.md), [`MODELS`](docs/MODELS.md), [`MODEL_INSTALLATION`](docs/MODEL_INSTALLATION.md), [`GPU_SETUP`](docs/GPU_SETUP.md), [`GPU_VALIDATION`](docs/GPU_VALIDATION.md).
- Running it: [`DEPLOYMENT`](docs/DEPLOYMENT.md), [`OPERATIONS`](docs/OPERATIONS.md) (runbooks, backup and restore), [`OBSERVABILITY`](docs/OBSERVABILITY.md), [`SECURITY`](docs/SECURITY.md), [`POLICY_AND_COMPLIANCE`](docs/POLICY_AND_COMPLIANCE.md), [`LOAD_TEST`](docs/LOAD_TEST.md), [`TROUBLESHOOTING`](docs/TROUBLESHOOTING.md), [`ENVIRONMENT_VARIABLES`](docs/ENVIRONMENT_VARIABLES.md).
- Working on it: [`DEVELOPMENT`](docs/DEVELOPMENT.md), [`TESTING`](docs/TESTING.md), [`INVARIANTS`](docs/INVARIANTS.md) (the 14 architecture invariants), [`ENVIRONMENT`](docs/ENVIRONMENT.md) (the machines this was built on).
- Audit (2026-10): `FINAL_AUDIT_AND_FIX_REPORT.md` and `GPU_READINESS_REPORT.md` in the workspace's `docs/` (and in `docs/` of the Phase 14 bundle).
- Decisions: [`docs/adr/`](docs/adr/) (ADRs 0001–0060), [`DECISIONS`](docs/DECISIONS.md), [`SPEC_ERRATA`](docs/SPEC_ERRATA.md); plans: [`ROADMAP.md`](ROADMAP.md), [`TODO.md`](TODO.md), [`CHANGELOG.md`](CHANGELOG.md).

## Contributing

Every pull request follows the rule-20 checklist in `.github/pull_request_template.md`: a new concept is reflected in models, spec/CBS, migrations, API, UI, plugin manifests, build graph, router, tests, docs and roadmap — in the same change or a tracked TODO. No secrets in code, logs, fixtures or commits.
