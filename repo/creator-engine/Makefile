# AI Creator Engine — developer entry points (MASTER_BUILD_PROMPT §36).
# A target that belongs to a later phase fails loudly with the phase that builds it (`not_yet`), so
# nothing pretends to work before it does (rule 5); since Phase 8 every §36 target is implemented.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env
UV := uv
PNPM := pnpm
PYTEST := $(UV) run pytest

define not_yet
	@echo "make $@: not available yet — it is built in Phase $(1) (see ROADMAP.md)." >&2; exit 2
endef

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup
.env:
	@cp .env.example .env && echo "created .env from .env.example (dev-safe defaults)"

.PHONY: bootstrap
bootstrap: .env ## Check prerequisites, install Python and JS workspaces, install git hooks
	@$(UV) run --no-project python scripts/check_prereqs.py
	$(UV) sync
	$(PNPM) install --frozen-lockfile
	@if [ -d .git ]; then $(UV) run pre-commit install --install-hooks >/dev/null && echo "pre-commit hook installed"; fi

.PHONY: inspect-env
inspect-env: ## Print the environment facts recorded in docs/ENVIRONMENT.md
	@bash scripts/inspect_env.sh

# ---------------------------------------------------------------- infrastructure
.PHONY: infra-up
INFRA_SERVICES := postgres redis temporal temporal-ui seaweedfs

infra-up: .env ## Start the infrastructure only (Postgres, Valkey, Temporal, SeaweedFS) and wait until healthy; no service images are built
	@docker info >/dev/null 2>&1 || { echo "Docker daemon is not reachable. Start Docker, or use the native fallback (TODO.md)." >&2; exit 1; }
	$(COMPOSE) --profile core up -d --wait --wait-timeout 180 $(INFRA_SERVICES)
	@$(COMPOSE) --profile core ps --format 'table {{.Service}}\t{{.Status}}'

.PHONY: infra-down
infra-down: .env ## Stop the infrastructure (keeps volumes)
	$(COMPOSE) --profile core down

.PHONY: infra-reset
infra-reset: .env ## Stop the infrastructure and delete its volumes (destroys local data)
	$(COMPOSE) --profile core down -v

.PHONY: infra-status
infra-status: .env ## Show infrastructure container health
	@$(COMPOSE) --profile core ps --format 'table {{.Service}}\t{{.Status}}'

.PHONY: dev
dev: infra-up seed ## Bring up the system in mock mode: infrastructure, API, orchestrator, scheduler, workers, dev seed
	@# --build: the service images carry the code (non-editable install); reusing an existing image would run
	@# stale code against the current API and database. The layer cache keeps an unchanged rebuild quick.
	EXTRA_CA_BUNDLE=$(EXTRA_CA_BUNDLE) $(COMPOSE) --profile core --profile mock-gpu up -d --build --wait --wait-timeout 300
	@$(COMPOSE) --profile core --profile mock-gpu ps --format 'table {{.Service}}\t{{.Status}}'
	@echo ""
	@echo "Running in mock mode: infrastructure, web app, API, orchestrator, scheduler, render worker and worker-cpu"
	@echo "(mock adapters on CPU, plus the real CPU engines whose assets are in .cache/models: CPU_REAL_ENGINES=auto);"
	@echo "migrated, buckets created, dev data seeded."
	@echo "  Web app    http://localhost:$${WEB_PORT:-3000}"
	@echo "  API        http://localhost:$${API_PORT:-8000}  (OpenAPI docs: /docs)"
	@echo "  Scheduler  http://localhost:$${SCHEDULER_PORT:-8100}/healthz"
	@echo "  Postgres   localhost:$${POSTGRES_PORT:-5432}   Temporal  localhost:$${TEMPORAL_PORT:-7233}"
	@echo "  Valkey     localhost:$${REDIS_PORT:-6379}   Temporal UI http://localhost:$${TEMPORAL_UI_PORT:-8080}"
	@echo "  S3 (SeaweedFS) http://localhost:$${S3_PORT:-8333}"
	@echo "Build the fixture video: make e2e-mock  (or: uv run ce video generate-fixture --out .data/final.mp4)"

.PHONY: dev-down
dev-down: .env ## Stop everything `make dev` started (keeps volumes)
	$(COMPOSE) --profile core --profile mock-gpu down

# ---------------------------------------------------------------- native mode and observability (Phase 14)
.PHONY: dev-native
APP_SERVICES := api web orchestrator render-worker scheduler worker-cpu

dev-native: infra-up seed ## Run the API, scheduler, orchestrator, render worker, CPU worker and web on the host (no service images)
	@# containers of an earlier `make dev` would poll the same Temporal queues and hold the same ports
	@$(COMPOSE) --profile core --profile mock-gpu stop $(APP_SERVICES) >/dev/null 2>&1 || true
	@# always rebuild: a stale .next build would serve an old web app against the current API
	$(PNPM) --filter @ce/web build
	$(UV) run python scripts/dev_native.py up

.PHONY: dev-native-down
dev-native-down: ## Stop the services `make dev-native` started (infrastructure keeps running)
	$(UV) run python scripts/dev_native.py down

.PHONY: obs-up
obs-up: .env ## Start the observability profile (Prometheus, Alertmanager, Grafana, Loki, Tempo, OTel collector) next to `make dev`
	$(COMPOSE) --profile observability up -d --wait --wait-timeout 180
	@echo "Grafana http://localhost:$${GRAFANA_PORT:-3001} (admin / GRAFANA_ADMIN_PASSWORD)  Prometheus http://localhost:$${PROMETHEUS_PORT:-9090}"

.PHONY: obs-native
obs-native: .env ## The observability profile scraping services run by `make dev-native`
	$(COMPOSE) -f infra/compose/docker-compose.native-obs.yml --profile observability up -d --wait --wait-timeout 180

.PHONY: obs-down
obs-down: .env ## Stop the observability profile (keeps its volumes)
	$(COMPOSE) --profile observability down

.PHONY: gen-dashboards
gen-dashboards: ## Regenerate the Grafana dashboards from scripts/gen_dashboards.py
	$(UV) run python scripts/gen_dashboards.py

.PHONY: demo
demo: .env ## The mock-mode demo through the HTTP API of a running stack (`make dev` or `make dev-native`)
	$(UV) run python scripts/demo.py $(ARGS)

.PHONY: backup
backup: .env ## Back up Postgres and both buckets to .data/backups/<UTC timestamp> (scripts/backup.py)
	$(UV) run python scripts/backup.py backup --out .data/backups/$$(date -u +%Y%m%dT%H%M%S)

.PHONY: load-test
load-test: .env ## Scheduler load test on the real queue and object store (ARGS="--tasks 3000 --workers 40")
	$(UV) run python scripts/load_test_scheduler.py $(ARGS)

# ---------------------------------------------------------------- quality
.PHONY: lint
lint: ## Ruff (lint + format check), ESLint, Prettier
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(PNPM) exec eslint .
	$(PNPM) exec prettier --check .

.PHONY: format
format: ## Apply ruff and prettier formatting
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
	$(PNPM) exec prettier --write .

.PHONY: typecheck
typecheck: ## mypy (strict on ce_core, ce_contracts, ce_behavior) and tsc on the TS workspace (api client, web app)
	$(UV) run mypy
	$(PNPM) --filter @ce/api-client run typecheck
	$(PNPM) --filter @ce/web run typecheck

.PHONY: verify-spec
verify-spec: ## Mechanical consistency check of docs/MASTER_BUILD_PROMPT.md
	$(UV) run python scripts/verify_spec.py

.PHONY: verify-config
verify-config: ## Validate every config file, vocabulary and cross-reference (ce config validate)
	$(UV) run ce config validate --env dev
	$(UV) run ce config validate --env prod

# ---------------------------------------------------------------- tests
.PHONY: test
test: verify-spec verify-config ## Full test suite (infra tests skip with a reason when infra is down) and web unit tests
	$(PYTEST)
	$(PNPM) --filter @ce/web run test

.PHONY: test-web
test-web: ## Web unit and contract tests (Vitest)
	$(PNPM) --filter @ce/web run test

.PHONY: test-unit
test-unit: ## Tests that need no infrastructure
	$(PYTEST) -m "not infra"

.PHONY: test-infra
test-infra: ## Infrastructure smoke tests (needs `make infra-up`)
	CE_REQUIRE_INFRA=1 $(PYTEST) -m infra

.PHONY: test-invariants
test-invariants: ## Architecture invariant tests (section 4)
	@bash scripts/pytest_allow_empty.sh tests/invariants

.PHONY: test-behavior
test-behavior: ## Behavior, world and memory suites (section 37)
	@bash scripts/pytest_allow_empty.sh -m behavior

.PHONY: test-api
test-api: ## API tests
	@bash scripts/pytest_allow_empty.sh apps/api

.PHONY: test-workflows
test-workflows: ## Temporal workflow tests
	@bash scripts/pytest_allow_empty.sh apps/orchestrator

.PHONY: test-render
test-render: ## Render golden tests
	@bash scripts/pytest_allow_empty.sh apps/render-worker packages/py/ce_render

.PHONY: test-e2e
test-e2e: ## Playwright e2e in mock mode against `make dev` (create → previz → approve → play); CE_E2E_CHROME=path to Chrome
	$(PNPM) --filter @ce/web exec playwright test

.PHONY: e2e-mock
e2e-mock: dev ## Fixture spec → MP4 on CPU (< 3 min), cached rerun, text → previz → approve → video, worker kill
	@set -a; . ./.env; set +a; $(UV) run python scripts/e2e_mock.py

# ---------------------------------------------------------------- data and codegen
.PHONY: migrate
migrate: .env ## Apply database migrations (alembic upgrade head on DATABASE_URL)
	@set -a; . ./.env; set +a; $(UV) run alembic -c packages/py/ce_db/alembic.ini upgrade head && echo "database migrated to head"

.PHONY: seed
seed: migrate ## Seed the demo dev org, admin, creator "Alex", wardrobe, world and memory (idempotent; dev/test only; SEED=0 skips)
	@set -a; . ./.env; set +a; $(UV) run ce storage init && \
	  if [ "$(SEED)" = "0" ]; then echo "SEED=0: demo seed skipped (empty database; ce admin bootstrap creates the first org)"; \
	  else $(UV) run ce seed dev; fi

.PHONY: purge-demo
purge-demo: .env ## Report the demo (seed) data `ce data purge-demo` would delete; APPLY=1 deletes it
	@set -a; . ./.env; set +a; $(UV) run ce data purge-demo $(if $(filter 1,$(APPLY)),--apply,)

.PHONY: gen-schema
gen-schema: ## Export JSON Schemas from the Pydantic models and generate the TypeScript model types
	$(UV) run python scripts/gen_schema.py
	$(PNPM) --filter @ce/api-client run gen:models

.PHONY: gen-client
gen-client: ## Generate the TypeScript API types from the API's OpenAPI document
	$(UV) run python scripts/gen_openapi.py
	$(PNPM) --filter @ce/api-client run gen:api

.PHONY: calibrate-analyzers
calibrate-analyzers: .env ## Calibrate observation proxies of the real CPU analyzers (fixture clips, synthetic speech) and store them
	$(UV) run python scripts/calibrate_analyzers.py

.PHONY: verify-licenses
verify-licenses: ## Re-check every plugin license block (offline; ARGS=--online re-fetches the evidence at the pins)
	$(UV) run python scripts/verify_licenses.py $(ARGS)

.PHONY: fetch-cpu-assets
fetch-cpu-assets: .env ## Download CPU model assets listed in scripts/cpu_assets.yaml (checksum-verified)
	$(UV) run python scripts/fetch_cpu_assets.py

# ---------------------------------------------------------------- images and GPU
.PHONY: build-images
build-images: ## Build the service images (api, services, web). EXTRA_CA_BUNDLE=path for TLS-intercepting proxies
	EXTRA_CA_BUNDLE=$(EXTRA_CA_BUNDLE) $(COMPOSE) --profile core build api orchestrator web

# Smoke, bench and calibration runs of one adapter on eval/smoke (docs/GPU_VALIDATION.md). On a GPU host
# with the family's stack installed: `make smoke-gpu PLUGIN=infinitetalk ARGS="--fetch --record"`.
# ARGS="--backend test" is a dry run on the CPU stand-in: it never produces evidence (rule 5).
define need_plugin
	@if [ -z "$(PLUGIN)" ]; then echo "usage: make $@ PLUGIN=<adapter id> [ARGS=…]" >&2; exit 2; fi
endef

.PHONY: smoke-gpu
smoke-gpu: ## Smoke run of one adapter on eval/smoke (PLUGIN=…, ARGS="--fetch --record" on a GPU host)
	$(call need_plugin)
	@set -a; [ -f .env ] && . ./.env; set +a; $(UV) run python scripts/smoke/run.py $(PLUGIN) $(ARGS)

.PHONY: bench
bench: ## Bench run (timings, VRAM, measured behavior) of one adapter on eval/smoke (PLUGIN=…)
	$(call need_plugin)
	@set -a; [ -f .env ] && . ./.env; set +a; $(UV) run python scripts/bench/run.py $(PLUGIN) $(ARGS)

.PHONY: calibrate
calibrate: ## Knob calibration of one avatar adapter (PLUGIN=…)
	$(call need_plugin)
	@set -a; [ -f .env ] && . ./.env; set +a; $(UV) run python scripts/calibrate/run.py $(PLUGIN) $(ARGS)
