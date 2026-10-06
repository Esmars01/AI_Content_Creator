# Phase 0 — Environment and bootstrap: progress report

**Date:** 2026-10-03 · **Status:** DoD passed · **Path taken:** Docker (the native fallback was not needed)

## Definition of Done

> `make bootstrap && make infra-up && make test` pass (or the native-fallback equivalents), and the services are healthy.

Verified on a **fresh clone** of the repository: `make bootstrap && make infra-up && make lint && make typecheck && make test` exited 0 — 95 tests passed, spec check 0 errors / 0 warnings, all five services healthy. Each intermediate commit was also checked in a clean worktree (ruff, mypy, pytest green).

## What was built

| Area | Result | State |
| --- | --- | --- |
| Spec verification | `scripts/verify_spec.py` checks every cross-reference mechanically (43 sections, 15 phases, 14 invariants, 27 ADRs, 75 tables, 26 workflows, 35 node kinds, 51 capabilities, 48 env vars, 24 make targets, 148 API paths). One semantic erratum found by reading and fixed (E1, `docs/SPEC_ERRATA.md`) | implemented and tested |
| Environment inspection | `docs/ENVIRONMENT.md`, `scripts/inspect_env.sh` | implemented and tested |
| FFmpeg/libass | System FFmpeg 6.1.1 renders an Arabic caption with libass reporting `Shaper: FriBidi … HarfBuzz-ng … (COMPLEX)`; glyphs visibly joined and right-to-left | implemented and tested |
| Repository layout (§8) | 24 `ce_*` packages and 5 Python apps (importable, typed, each declares its phase); `apps/web` and `packages/ts/api-client` placeholders; 20 plugin categories; config tree | stubbed (skeletons by design) — layout tested |
| Workspaces and tooling | uv (Python 3.12; `ce_contracts`/`ce_worker` parse as Python 3.10), pnpm (Node 24), ruff, mypy, ESLint 10, Prettier, pre-commit (all hooks pass on all files) | implemented and tested |
| Makefile (§36) | Every target exists; Phase 0 targets work; later-phase targets exit 2 naming their phase | implemented and tested |
| `.env.example` (§35) | Every variable, dev-safe; `PROVENANCE_MODE`/`COOKIE_SECURE` empty and resolved per environment from `config/env/*.yaml` | implemented and tested |
| Compose `core` | Postgres 18 + pgvector, Valkey 8.1, Temporal dev server + UI, SeaweedFS S3; digests pinned; healthchecks; cold start to healthy in ~12 s; data survives `infra-down`/`infra-up` | implemented and tested |
| `make fetch-cpu-assets` | Manifest-driven, SHA-256 verified downloader; manifest empty until Phase 7 | implemented; download path **untested** (no assets yet) |
| CI | `.github/workflows/ci.yml` (lint, typecheck, unit, invariants, behavior; infra job with Compose) | implemented but **untested** — no GitHub remote yet |
| PR template | Rule-20 checklist and invariant checkbox | implemented and tested (content check) |
| Docs | ADRs 0001–0029, `DECISIONS.md` (12 deviations), `INVARIANTS.md`, `ENVIRONMENT_VARIABLES.md`, `SPEC_ERRATA.md`, `TODO.md`, `ROADMAP.md`, `README.md`, `CHANGELOG.md` | written; structure tested |

## How it was tested

- `tests/phase0/` (90 tests): layout derived from the spec's own §8 tree; every package and app imports; Python 3.10 syntax compatibility of `ce_contracts`/`ce_worker`; ADR files match the §6 table and have Context/Decision/Alternatives/Consequences; `DECISIONS.md` links every ADR; `INVARIANTS.md` gives each invariant a phase; env vars and make targets match §35/§36; layered config resolution; Compose healthchecks and digest pinning; the RTL render; a secret scan of every committable file.
- `tests/infra/` (5 tests) against the live stack: pgvector distance query on Postgres ≥ 18; Streams replay after an event ID (the SSE `Last-Event-ID` pattern); a Temporal workflow + activity round trip with `pydantic_data_converter`; S3 bucket/put/get and a presigned GET through SeaweedFS; Temporal UI over HTTP.
- Skip behavior: with the stack down, `make test` skips the 5 infra tests with the reason "core infrastructure not reachable (…); run `make infra-up`", and `make test-infra` fails (exit 2).
- mypy strictness was probed: an untyped function and a bare `list` are rejected in `ce_core`/`ce_contracts` and accepted in `ce_db`.

## What is not tested or not built

- **Native fallback** (no Docker): not built — Docker worked here. Tracked in `TODO.md`.
- **CI on GitHub**: never run (no remote).
- **GPU**: none on this machine; nothing GPU-related exists yet. No money was spent and no paid GPU was provisioned.
- **Invariant tests**: none yet — nothing they protect exists. `make test-invariants` says "no tests collected yet" rather than passing silently.
- The `cpu_assets` download path has no assets to exercise until Phase 7.

## Known issues and decisions worth a look

- **Valkey instead of Redis** (ADR 0028): Redis 7.4+ is source-available and Redis 8 adds AGPLv3; Valkey is BSD-3 and protocol-compatible. Same service name and `REDIS_URL`.
- **Temporal dev server + single-node SeaweedFS** for local infrastructure (ADR 0029).
- **mypy `strict = true` in a per-module override applies globally** in mypy 2.4 — found by probing; the override now lists the individual strict flags.
- **ruff 0.16 formats Python code blocks inside Markdown**; it had reformatted the spec's §23 code sample. Markdown is now excluded from ruff, and the spec copy was restored with only E1 applied.
- **TypeScript 5.9.3** is pinned although 7.0.2 is npm `latest`: typescript-eslint 8.71 supports `<6.1.0` (DECISIONS D9). Revisit before Phase 5.
- The tool shell here resolves `node` to v22; commands use `/opt/node24/bin` first. A developer machine just needs Node 24 on `PATH`; `make bootstrap` checks it.
- **The build VM is ephemeral.** The repository (6 commits) exists only inside this session until it is pushed or copied to the owner's machine.

## Next steps

Phase 1 — domain core, vocabularies, persistence, API skeleton (`TODO.md` has the breakdown): `ce_core` models and SpecPath, vocabularies at `vocab_version 2026.10.1`, `ce_config`, `ce_db` with every §29 table and the immutability triggers (including the full §12.8 identity-column set from E1), `ce_storage` contract tests on SeaweedFS, `ce seed dev`, and the API skeleton with auth — plus invariant tests I2, I12 and I3 (identity rows).

## Post-audit corrections (2026-10, after Phase 14)

- **B1** — the credential scan and the `.env`-ignored check skipped outside a plain checkout (they tested `.git` beside the sources); they now run in any git work tree.
- **D12** — the Compose ports (Postgres with its dev password, Valkey without auth, Temporal, SeaweedFS) were published on all interfaces; now on `CE_BIND_ADDRESS`, default `127.0.0.1`.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-0`).
