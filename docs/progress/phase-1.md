# Phase 1 — Domain core, vocabularies, persistence, API skeleton: progress report

**Date:** 2026-10-03 · **Status:** DoD passed · **Path taken:** Docker (Compose `core`); the local-filesystem storage provider exists for the native fallback but the fallback stack itself is still not built.

## Definition of Done

> Unit tests pass, plus schema tests (VideoSpec, CBS, World DNA and intent round trips; vocabulary validation and lint), migration up and down, API tests, tenancy tests over every tenant table, and immutability tests; World DNA validation and world-versioning tests (drafts editable, approved immutable) pass, and so do the I2 and I12 invariant tests; an upload round-trips through SeaweedFS, or through local storage in the native fallback.

Verified on a **fresh clone** at commit `50b0c1a`: `make bootstrap`, `make dev` (rebuilt the api image, migrated, created the buckets, seeded, every container healthy), `make lint`, `make typecheck`, and `CE_REQUIRE_INFRA=1 make test` (infrastructure tests may not skip) all exited 0 — **789 tests passed, none skipped**.

| DoD item | Where | Result |
| --- | --- | --- |
| Unit tests | `packages/py/*/tests` (343) | pass |
| Schema round trips and snapshots (VideoSpec, CBS, World DNA, intent) | `packages/py/ce_core/tests`, `tests/phase1` (JSON Schema + TS freshness) | pass |
| Vocabulary validation and lint | `ce_core/tests/test_vocab.py`, `ce config validate` in `make test` | pass; 112 config files, 0 errors |
| Migration up and down | `ce_db/tests/test_migrations.py`: upgrade → downgrade → upgrade, then `alembic check` finds no drift | pass |
| API tests | `apps/api/tests` (69): auth, members, projects, identity, worlds, memory, assets, events, versions, OpenAPI | pass |
| Tenancy over every tenant table | `tests/invariants/test_i12_tenancy.py` (all 64 tenant tables; 11 global tables admin-only) and `test_i12_tenancy_api.py` (every API route with an id) | pass |
| Immutability | `tests/invariants/test_i03_immutability.py` (repository + trigger, 6 identity tables, video-version identity columns) and `test_i03_immutability_api.py` | pass |
| World DNA validation and world versioning | `apps/api/tests/test_worlds_api.py`, `ce_core/tests/test_identity_models.py` | pass |
| I2 | `tests/invariants/test_i02_projection_rebuild.py` | pass |
| Upload round trip through SeaweedFS | `apps/api/tests/test_assets_api.py::test_upload_round_trips_through_seaweedfs` (two presigned parts, validation job, download, sha256 equal) and the S3 contract suite (30 cases) | pass |

Also checked by hand against the running api container: login, `/v1/me`, a presigned upload from the host to SeaweedFS through `S3_PUBLIC_ENDPOINT_URL`, validation with the container's ffprobe, download, delete, `/openapi.json`, and JSON log lines.

## What was built

| Area | Result | State |
| --- | --- | --- |
| `ce_core` | ids, keys, canonical digests, VideoSpec, SpecPath, anchors, tokenizer, CBS / compiled / observed / coverage / plan report, Creator/Appearance/Voice/Wardrobe/World DNA, memory, validator, BuildManifest model | implemented and tested |
| Vocabularies | 10 files at `2026.10.1`, loader, lint (`docs/VOCABULARIES.md`) | implemented and tested |
| `ce_config` | schemas for every config file, layered settings, digests, production startup refusals, `ce config validate` | implemented and tested |
| `ce_db` | 75 tables, migration 0001 with database rules (I3 triggers, append-only, cache guards), org-scoped repositories, projections | implemented and tested |
| `ce_storage` | S3 (SeaweedFS/R2) and local-filesystem providers behind one interface, shared contract suite, registry (ADR 0030) | implemented and tested (S3 against SeaweedFS only, not R2) |
| `ce_obs` | JSON logs with bound context, trace ids and secret redaction; OpenTelemetry tracing, OTLP/HTTP export, W3C propagation | implemented and tested; OTLP export tested against a local HTTP stub, not a real collector |
| Seed | `ce seed dev`, `ce storage init`, `make seed` (idempotent, placeholder media uploaded) | implemented and tested |
| API | auth (password provider, sessions, CSRF, API keys, rate limit), members, invitations, projects, videos/versions (read), creators, appearances, wardrobes, worlds, memory, voices (read), uploads, problem+json, pagination, idempotency, OpenAPI, SSE (`docs/API.md`) | implemented and tested |
| Jobs | `asset_validation` and memory `deletion` as `generation_jobs` with `job.updated` events, run in-process (ADR 0031) | implemented and tested; no retry or recovery yet |
| Images and Compose | `infra/docker/api.Dockerfile` (pinned bases, non-root, optional `extra_ca` build secret), `api-migrate` + `api` in `core`, `make build-images`, `make dev` | implemented and tested |
| TypeScript | `make gen-schema` (models.ts) and `make gen-client` (api.ts) with freshness tests | implemented and tested |
| Docs | VIDEOSPEC, WORLDS, CREATORS, MEMORY, API, INVARIANTS status, ADRs 0030–0031, DECISIONS D13–D27 | written |

## How it was tested

- One storage contract suite runs against both providers (S3 on SeaweedFS, local). The API cross-tenant invariant enumerates every route with an id from the OpenAPI document, so a new route without a cross-tenant case fails.
- Mutation checks: the suites were run against deliberately broken code (signature, expiry and content-type checks removed from the local provider; redaction removed from the log chain; the org filter removed from a lookup; approval checks disabled). Each break failed the expected tests.
- SSE runs against a real uvicorn server (in-process ASGI clients buffer streams): live delivery, `Last-Event-ID` replay, project filter, cross-org isolation.
- Phase 0 checks still pass (layout, docs, env, Compose pinning — updated for the built api image —, secret scan).

## Not tested or not built

- **Native fallback stack** (no Docker): the local-filesystem storage provider exists and passes the contract suite, but `make infra-up-native` (local Postgres, `temporal server start-dev`) is not built. Docker works here.
- **CI on GitHub**: never run (no remote yet).
- **Approvals check recorded results only.** Nothing produces identity packs, age estimates, wardrobe references or plate fingerprints yet; the tests write those results the way the Phase 2 mock workflows will. The seeded fixtures are pre-approved placeholders.
- **Video creation** answers `501` until planning (Phase 4). Generation, editing and rendering endpoints do not exist yet.
- **Voices** are read-only (voice design is a Phase 2+ workflow).
- **Prometheus metrics**: planned for Phase 2.
- OIDC is V1.
- No GPU was used, no money was spent.

## Known issues and decisions worth a look

- **In-process jobs** (ADR 0031): a job running when the API process stops stays `running`. Phase 2 moves both kinds into Temporal workflows.
- **SeaweedFS ran out of volumes** with its defaults once a third bucket existed (PUT → `InternalError`). The dev server now allows more, smaller volumes (DECISIONS D23).
- **ffprobe exits 0 on many corrupt files**; validation treats any error output as a failure and requires a picture with non-zero size for images and video. A truncated PNG would otherwise have been accepted — the tests caught it.
- **openapi-typescript caught an unresolvable `$ref`** in the problem responses; the Problem schema is now a registered component.
- **Presigned URLs need a host the browser can reach**; `S3_PUBLIC_ENDPOINT_URL` signs them for the public endpoint (ADR 0030). Three storage variables are additions to §35.
- **The api image is 1.04 GB**, mostly Debian's FFmpeg (a GPL build, needed for ffprobe; ADR 0017 review before distributing it, DECISIONS D27).
- **SSE is hand-encoded** over a streaming response so the stream never holds a database session (DECISIONS D26).
- **Builds behind TLS-intercepting proxies** need the proxy's CA: `EXTRA_CA_BUNDLE=… make dev` passes it as a build secret (never stored in the image).
- **The build VM is ephemeral.** The repository exists only inside this session until it is pushed or copied to the owner's machine (still awaiting the owner's choice).

## Next steps

Phase 2 — execution backbone in mock mode: `ce_contracts` and the plugin loader (then the storage providers become plugins), mock plugins producing real media, `ce_worker`, the scheduler with leasing and the mock GPU provider, the build graph with cache keys and pinning, policy and router, Temporal workflows (including `AssetValidationWorkflow` and `DeletionWorkflow` replacing the in-process jobs), FFmpeg rendering with mock provenance, and `make e2e-mock`. Invariants I5, I11, I14.
