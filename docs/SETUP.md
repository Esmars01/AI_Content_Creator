# Setup (development, mock mode)

How to get the whole system running on one machine without a GPU: infrastructure, web app, API,
orchestrator, scheduler, render worker and `worker-cpu`, with mock adapters and — optionally — the
real CPU engines. Everything here comes from `Makefile`, `.env.example`, `scripts/` and
`infra/compose/docker-compose.yml`; `make help` lists every target. How the services fit together:
`docs/ARCHITECTURE.md`. The machine this was built on: `docs/ENVIRONMENT.md`. GPU hosts:
`docs/GPU_SETUP.md`.

Mock mode is honest about what it is: generation engines, the VLM and the invisible watermarks are
mocks, every GPU adapter is `untested_on_gpu`, no real `embed.text` model is registered (ADR 0057),
and every render carries mock provenance, so none can be exported (ADR 0047, ADR 0059).

## Prerequisites

`make bootstrap` runs `scripts/check_prereqs.py`, which fails on the first four rows and only warns
on the others.

| Tool | Required | Checked as |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | yes | `uv` on `PATH` |
| Python 3.12 | yes | `uv python find 3.12` (install with `uv python install 3.12`); pinned in `.python-version` |
| Node 24 LTS or newer | yes | `node --version` ≥ 24 (`engines` in `package.json`) |
| pnpm 10 | yes | `pnpm` on `PATH` (`packageManager: pnpm@10.28.0`; `corepack enable` provides it) |
| Docker with Compose v2, daemon running | for `make infra-up` / `make dev` | warning when `docker info` fails |
| FFmpeg with libass, HarfBuzz and FriBidi | for host-side tests and `make e2e-mock` (the containers bring their own FFmpeg, ADR 0035) | warning when `ffmpeg -version` lacks `--enable-libass`, `--enable-libharfbuzz` or `--enable-libfribidi` |
| Google Chrome | only for `make test-e2e` | Playwright uses `channel: "chrome"` or the binary in `CE_E2E_CHROME`; its bundled Chromium cannot play the H.264 renders |

The reference machine had 2 vCPUs, 7.8 GiB RAM and ~30 GB free disk, and ran the Compose stack and
the CPU engines (`docs/ENVIRONMENT.md`). No GPU is needed or used.

There is no native (Docker-free) stack: the fallback with a local Postgres and Temporal binary is
deferred (`TODO.md`, Phase 0). `make infra-up` does not start the Docker daemon for you.

## 1. Bootstrap

```bash
make bootstrap
```

- creates `.env` from `.env.example` when it does not exist (dev-safe values; every variable is in
  `docs/ENVIRONMENT_VARIABLES.md`);
- checks the prerequisites above;
- `uv sync` (the Python workspace) and `pnpm install --frozen-lockfile` (the JS workspace);
- installs the pre-commit hooks when the directory is a git repository.

Run it before anything else: `make dev`, `make migrate` and `make seed` run Python tools on the host
through `uv run`.

## 2. Optional: real CPU engines

```bash
make fetch-cpu-assets
```

Downloads the 18 files listed in `scripts/cpu_assets.yaml` (about 600 MB by the manifest's byte
counts: Kokoro TTS, faster-whisper, MediaPipe face and body, AuraFace, DINOv2, DNSMOS, PP-OCR) into `MODEL_CACHE_DIR`
(`./.cache/models` in `.env.example`). Each file has a pinned URL, a SHA-256 and a license; files
already present with the right checksum are skipped. `uv run python scripts/fetch_cpu_assets.py
--check` only verifies; `--plugin ID` limits the run to one plugin.

With `CPU_REAL_ENGINES=auto` (the default) a real engine is used when all its files are present,
and the mocks are used otherwise. The Compose services mount `./.cache/models` read-only at
`/models`, so keep the default `MODEL_CACHE_DIR` if the containers should see the assets. Kokoro is
development-only (GPL phonemizer stack, ADR 0043). Without this step everything runs on mocks.

## 3. Start the stack

```bash
make dev
```

What it does, in order:

1. `infra-up`: checks that the Docker daemon answers, then starts only the infrastructure of profile
   `core` (`docker compose --profile core up -d --wait postgres redis temporal temporal-ui
   seaweedfs`): Postgres 18 + pgvector, Valkey (service `redis`), the Temporal dev server and UI,
   SeaweedFS. No service image is built.
2. `seed` (after `migrate`): `alembic upgrade head` on the host's `DATABASE_URL`, then
   `ce storage init` and `ce seed dev` — the dev organization, admin, creator "Alex", wardrobe,
   world and memory. The seed is idempotent and refuses `APP_ENV=prod`.
3. The `core` and `mock-gpu` profiles together: the one-shot `api-migrate` (migrations and buckets),
   `api`, `web`, `orchestrator`, `scheduler` and `render-worker` (the images
   `creator-engine/api`, `creator-engine/services` and `creator-engine/web` are rebuilt from
   `infra/docker/` on every `make dev` — the images carry the code, so an image that is not rebuilt
   runs old code; the layer cache keeps an unchanged rebuild quick), and `worker-cpu`
   (`WORKER_RUNTIME_FAMILY=cpu_model`, two tasks at once: `WORKER_CPU_CONCURRENCY`), which registers
   with the scheduler and serves the mock adapters plus the real CPU engines whose assets are present.
   Ports are published on `CE_BIND_ADDRESS` (default `127.0.0.1`, this machine only).

`make dev` prints the dev login `admin@creator-engine.local` and, the first time, a generated
password, shown once. Set `CE_SEED_ADMIN_PASSWORD` (in the environment or in `.env`, which the seed
target sources) to choose it; an existing password is kept unless `ce seed dev --reset-password`.

`make infra-up` alone starts the infrastructure only; `make dev-native` then runs the services on the host without images (`docs/DEPLOYMENT.md`).

| Endpoint | Default address |
| --- | --- |
| Web app | <http://localhost:3000> (`docs/FRONTEND.md`) |
| API | <http://localhost:8000>, OpenAPI docs at `/docs`, document at `/openapi.json` (`docs/API.md`) |
| Scheduler | <http://localhost:8100/healthz> |
| Temporal UI | <http://localhost:8080> (gRPC on `localhost:7233`) |
| S3 (SeaweedFS) | `http://localhost:8333`, keys `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` from `.env` |
| Postgres | `localhost:5432` (`ce` / `ce_dev_password`, database `creator_engine`) |
| Valkey | `localhost:6379` |

Host ports come from `.env` (`WEB_PORT`, `API_PORT`, `POSTGRES_PORT`, `REDIS_PORT`, `TEMPORAL_PORT`,
`TEMPORAL_UI_PORT`, `S3_PORT`, `SEAWEED_MASTER_PORT`; the scheduler uses `SCHEDULER_PORT`, default
8100). The host-side URLs in `.env` (`DATABASE_URL`, `REDIS_URL`, `TEMPORAL_ADDRESS`,
`S3_ENDPOINT_URL`, …) name the same ports, so change both together; containers get internal service
names from the Compose file.

`make infra-status` shows the health of the `core` containers.

### What makes it mock mode

| Variable (`.env.example`) | Value | Effect |
| --- | --- | --- |
| `APP_ENV` | `dev` | dev configuration (`config/env/dev.yaml`); production startup checks do not apply |
| `MOCK_GPU` | `true` | registers the mock GPU provider and mock adapters, served by `worker-cpu` |
| `CPU_REAL_ENGINES` | `auto` | real CPU engines when their assets are present (`on` = refuse to start without them, `off` = mocks only) |
| `LLM_PROVIDER` | `fixture` | the Director replays recorded LLM fixtures; other input is planned by the labeled template Director (ADR 0016) |
| `PROVENANCE_MODE` | empty → `mock_dev` | real C2PA signed by a throwaway dev CA, mocked watermarks, a burned "MOCK PROVENANCE — NOT FOR DISTRIBUTION" label; exports refused |
| `MOCK_QC_FAIL_RATE`, `MOCK_TTS_MISREAD_RATE`, `MOCK_IMAGE_FACELESS_RATE` | `0.0` | inject failures into the mocks to exercise QC retries, the exact-script loop and analyzer honesty |

## 4. Make a video

- **Web app:** sign in, open **Create**, describe the video, review the previz, approve, watch the
  job and play the MP4 (`docs/FRONTEND.md`).
- **API:** create a project, `POST /v1/projects/{project_id}/videos` `{"input": "…"}` with an
  `Idempotency-Key` header, follow the job until `GET /v1/versions/{version_id}/previz` reports
  `previz_ready`, then `POST /v1/versions/{version_id}:approve` (`docs/API.md`, `docs/DIRECTOR.md`).
- **Fixture spec by hand:** `uv run ce video generate-fixture --out .data/final.mp4`; follow it in
  the Temporal UI or through `GET /v1/jobs/{id}`.

## 5. Tests

| Target | What it runs | Needs |
| --- | --- | --- |
| `make test` | `verify-spec` (`scripts/verify_spec.py`), `verify-config` (`ce config validate --env dev` and `--env prod`), the whole pytest suite, then the web unit tests (Vitest) | bootstrap; tests marked `infra` skip with a reason when the infrastructure is down |
| `make test-unit` | pytest `-m "not infra"` | bootstrap |
| `make test-infra` | pytest `-m infra` with `CE_REQUIRE_INFRA=1` (fails instead of skipping) | `make infra-up` |
| `make test-web` | Vitest for `apps/web` | bootstrap |
| `make test-invariants`, `test-behavior`, `test-api`, `test-workflows`, `test-render` | subsets: `tests/invariants`, `-m behavior`, `apps/api`, `apps/orchestrator`, `apps/render-worker` + `packages/py/ce_render` | bootstrap |
| `make e2e-mock` | depends on `make dev`, then `scripts/e2e_mock.py` against the running stack | Docker; host FFmpeg |
| `make test-e2e` | Playwright (`apps/web/e2e`) against the running stack | a running `make dev`; Google Chrome |

`make e2e-mock` checks, in order: the fixture spec becomes a playable MP4 with captions and music in
under 3 minutes on CPU, with a coverage report; a second run is entirely cache hits with an
identical render; text → Director (fixture LLM) → previz → approve → a ready version over HTTP; an
incremental edit ("make him more skeptical") that re-runs only the edited scene's behavior and
performance, and a restore built entirely from cache hits; and killing `worker-cpu` mid-task,
after which the lease expires and the restarted worker finishes. It writes
`.data/e2e/final.mp4`, `.data/e2e/coverage.json` and `.data/e2e/report.json`.

`make test-e2e` drives create → previz → approve → progress → play, among other specs. Its global
setup creates a throwaway editor in the dev organization with `scripts/e2e_user.py`; the edit spec
builds a two-scene video with `scripts/e2e_video.py`. Set `WEB_BASE_URL` when the web app is not on
`http://localhost:3000`.

## Stop and reset

| Target | Effect |
| --- | --- |
| `make dev-down` | stops everything `make dev` started (volumes kept) |
| `make infra-down` | stops the `core` profile (volumes kept) |
| `make infra-reset` | stops the `core` profile and **deletes its volumes** (database, Temporal history, object storage) |

## Other targets

| Target | Purpose |
| --- | --- |
| `make lint`, `make format`, `make typecheck` | Ruff, ESLint, Prettier; mypy and `tsc` |
| `make migrate`, `make seed` | migrations to head; the idempotent dev seed (after `migrate`) |
| `make gen-schema`, `make gen-client` | JSON Schemas and TS model types from Pydantic; TS API types from the OpenAPI document |
| `make build-images` | builds the `api`, `services` and `web` images |
| `make calibrate-analyzers` | calibrates the observation proxies of the real CPU analyzers |
| `make verify-licenses` | re-checks every plugin license block offline (`ARGS=--online` re-fetches the evidence) |
| `make inspect-env` | prints the facts recorded in `docs/ENVIRONMENT.md` (read-only) |
| `make smoke-gpu`, `make bench`, `make calibrate` | one adapter on `eval/smoke` (`PLUGIN=…`). They need a GPU host with the family's stack to produce evidence; `ARGS="--backend test"` is a CPU dry run that never produces evidence (`docs/GPU_VALIDATION.md`) |

## Troubleshooting

- **"Docker daemon is not reachable"** from `make infra-up`: start Docker; the target does not.
- **TLS-intercepting proxy:** image builds need its CA bundle:
  `make dev EXTRA_CA_BUNDLE=/path/to/ca.pem` (also accepted by `make build-images`).
- **Docker Hub rate limits (HTTP 429):** the Phase 8 session configured a registry mirror in
  `/etc/docker/daemon.json`; the pinned digests pull through it (`docs/ENVIRONMENT.md`).
- **Wrong Node version:** `make bootstrap` refuses Node < 24; put a Node 24 bin directory first on
  `PATH`.
- **Video does not play in Playwright:** use Google Chrome (`CE_E2E_CHROME=/path/to/chrome`); the
  bundled Chromium has no H.264.
- **Real CPU engines not used:** check the assets with
  `uv run python scripts/fetch_cpu_assets.py --check` and that they are under `./.cache/models`.
- **Clean slate:** `make infra-reset` (destroys local data), then `make dev`.
