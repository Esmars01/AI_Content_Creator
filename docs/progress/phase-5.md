# Phase 5 — Frontend MVP shell: progress report

**Date:** 2026-10-04 · **Status:** DoD passed · **Path taken:** Docker (Compose `core` + `mock-gpu`, now with the `web` service), 2 vCPUs, no GPU, fixture LLM. No money was spent, no paid GPU was provisioned and no hosted LLM was called.

## Definition of Done

> a Playwright e2e test in mock mode: create → previz → approve → progress → play; the I9 coverage-badge contract test passes. (§40)

Verified on a **fresh clone** at commit `1aaa244`: `make bootstrap`, `make build-images` (api, services and web rebuilt from the clone), `make e2e-mock` (which runs `make dev`, now including the web app), `make test-e2e`, `make lint`, `make typecheck` and `CE_REQUIRE_INFRA=1 make test` all exited 0 — **1251 pytest tests and 38 Vitest tests passed, none skipped**, and the Playwright flow passed in 2.1 min.

| DoD item | Where | Result |
| --- | --- | --- |
| Playwright e2e in mock mode: create → previz → approve → progress → play | `apps/web/e2e/create-to-play.spec.ts` (`make test-e2e` against `make dev`) | pass (fresh clone, 2.1 min, Google Chrome 154): sign in → Create (idea, Format step, Plan) → previz review with script, keyframe storyboard, measured Performance timeline and predicted coverage without "delivered" → Approve → job progress in the studio → the final MP4 plays (currentTime advances, duration > 3 s) → scene badges show delivered counts after observation |
| I9 coverage-badge contract | `apps/web/src/lib/coverage.contract.test.tsx` (19 tests) run by `tests/invariants/test_i09_honest_ui.py` | pass: every outcome of the domain model (read from the generated JSON Schema, checked equal to `ce_core.enums.Outcome`) maps to a badge; only `*_CONFIRMED` outcomes are delivered (label, tone, `data-delivered`); planned items never say delivered; summaries count delivered only after observation. Mutation checks (counting `*_PARTIAL` as delivered; relabelling an approximated confirmation) are killed |

## What was built

| Area | Result | State |
| --- | --- | --- |
| `apps/web` foundation | Next.js 16 App Router, React 19, TypeScript strict (`noUncheckedIndexedAccess`), Tailwind CSS 4 tokens (WCAG AA contrast), shadcn-style primitives (D64), TanStack Query, Zustand, `openapi-fetch` on `@ce/api-client` | implemented and tested |
| Same-origin gateway | `/api/*` route handler forwarding to `API_INTERNAL_URL`: allow-listed request headers, streamed bodies and SSE, every `Set-Cookie` passed through, a problem document when the API is down (ADR 0039, D60) | implemented and tested (unit + e2e; SSE verified streaming through it) |
| Auth and navigation | Login, session guard (redirect on 401 with `next`), sign out; §31 navigation with later sections labelled (D62); Simple/Advanced toggle remembered per user (D63) | implemented |
| Dashboard, Projects | Recent videos with states, live running jobs, memory conflicts, honest spend/fleet/QC placeholders; project list and creation, a project's videos | implemented |
| Create + previz review | Ten-step stepper from `GET /v1/create-options`; previz review with script chips, storyboard (keyframes and plates), intent summary, Performance timeline (estimated or measured, event markers), predicted coverage per scene, memory used, repetition / contradiction / fact-check findings, assumptions, measured vs target duration and cost; Approve (overrides + reason) and Regenerate plan (instruction, fresh memory); Edit marked Phase 6 | implemented and tested (e2e) |
| Video Studio v1 | Player (final render, else proxy), job progress over SSE, versions list, Performance lane (+ Advanced state table), scenes with world and Simple coverage badges, intent panel, coverage table (Advanced), creator and world | implemented and tested (e2e) |
| Creators, Worlds, Jobs, Developer | Read-only DNA (Simple summary / Advanced JSON), worlds' elements, positions and plates; jobs list and node detail; VideoSpec, CBS, compiled behavior, plan report and BuildManifest viewers | implemented |
| API additions (D59) | `GET /v1/create-options`, `GET /v1/versions/{id}/storyboard`, video state on `VideoOut`, `event_timings` in the plan report (estimated at planning, measured after previz) | implemented and tested (API, I12, Director acceptance, e2e) |
| Delivery (D65) | `infra/docker/web.Dockerfile`, Compose `web` (`WEB_PORT`), `make test-web`, `make test-e2e`, `make test` runs Vitest, `make typecheck` the web app; CI jobs for web unit and e2e tests | done |
| Docs | ADR 0039, DECISIONS D59–D65, `docs/FRONTEND.md`, API, INVARIANTS (I9), ENVIRONMENT_VARIABLES, README, TODO, ROADMAP, CHANGELOG | written |

## Tests run and results

| Command | Result |
| --- | --- |
| `make lint` (ruff, eslint with React hooks rules, prettier) | pass |
| `make typecheck` (mypy, tsc for the API client and the web app) | pass |
| `CE_REQUIRE_INFRA=1 make test` | 1251 pytest passed, 0 skipped (8 min 47 s) + 38 Vitest passed (fresh clone) |
| `make build-images` | api, services and web images rebuilt |
| `make e2e-mock` | pass (fresh clone: 23.1 s cold fixture build, coverage 25/25, 41/41 cached, text → previz 8.9 s → approved video 82.1 s, worker kill recovered in 40.4 s) |
| `make test-e2e` (Playwright, Google Chrome) | 1 passed (2.1 min, fresh clone) |
| `scripts/gen_openapi.py --check`, `scripts/gen_schema.py --check`, `make verify-spec verify-config` | fresh / 0 errors |

New tests: `apps/web` Vitest (7 files, 38 tests: badge contract 19, coverage grouping 2, Performance lane 5, SSE invalidations 3, client 4, create form 3, gateway route 2), `apps/web/e2e/create-to-play.spec.ts`, `tests/invariants/test_i09_honest_ui.py` (2), `apps/api/tests/test_planning_api.py::test_create_options_come_from_configuration` plus storyboard and video-state assertions, an I12 case for the storyboard, event-timing assertions in the Director acceptance and e2e tests.

## Files changed

Added: `apps/web/{next.config.ts,postcss.config.mjs,tsconfig.json,vitest.config.ts,playwright.config.ts}`, `apps/web/src/app/**` (layout, providers, login, the `(app)` pages, the `/api/[...path]` gateway), `apps/web/src/components/**` (app shell, coverage, Performance lane, plan panels, findings, JSON viewer, state badge, `ui/*`), `apps/web/src/lib/{api,coverage,create-form,events,format,performance,queries,store,utils}.ts` and their tests, `apps/web/e2e/{global-setup,create-to-play.spec}.ts`, `infra/docker/web.Dockerfile`, `scripts/e2e_user.py`, `tests/invariants/test_i09_honest_ui.py`, `docs/FRONTEND.md`, `docs/adr/0039-web-app-architecture.md`, `docs/progress/phase-5.md`.

Changed: `apps/web/package.json`, `apps/api` (planning router: create options and storyboard; videos router: version state), `ce_core` (`EventTiming`, `event_timings`), `ce_director` (estimated event timings), `ce_exec` (measured event timings), `infra/compose/docker-compose.yml` (`web`), `Makefile`, `.github/workflows/ci.yml`, `eslint.config.mjs`, `package.json` (react-hooks plugin), `pnpm-lock.yaml`, `.env.example` (`WEB_PORT`), `.gitignore`, `scripts/e2e_mock.py`, JSON schemas and the TS client, docs and tests listed above.

## Known issues and limitations

- **Playback in tests needs Google Chrome.** Playwright's open-source Chromium cannot decode H.264/AAC; the e2e test uses `channel: "chrome"` or `CE_E2E_CHROME` and fails otherwise (D61). In this environment `/opt/google/chrome` is a symlink to that Chromium, so the run used Google Chrome 154 extracted from Google's official package (`CE_E2E_CHROME`).
- **Read-mostly studio.** NL edits, proposal cards, locks, the takes gallery, the versions tree with compare, the timeline tracks beyond the Performance lane and the export dialog are Phase 6 and later; Edit is shown disabled on the previz review.
- **No DOM caption overlay** (captions endpoints arrive with Phase 7/12) and **no keyboard shortcuts** yet.
- **Preferences in the browser.** Simple/Advanced is stored per user in `localStorage` until a preferences API exists (D63).
- **Login rate limiting behind the gateway** sees the web server's address unless the API trusts `X-Forwarded-For` from it (`FORWARDED_ALLOW_IPS`); the per-email limit is unaffected (ADR 0039).
- **Repeated identical inputs** trip the repetition guard (the hook equals a recent one), so the strategy stage falls back to its template and the previz says so — the guard working as designed, visible in the e2e runs that reuse one fixture input.

## Next phase and actions

**Phase 6 — Incremental editing** (§40), started automatically after this report:

1. The typed `EditOperation` union and SpecPatch; `ProposeEditWorkflow` and `ApplyEditWorkflow` (async); reference resolution ("him", "the second scene"); anchor rebase for word-anchored elements.
2. Locks with scopes and route pinning (locked-path edits refused with explanations, I8); takes and selection; regenerate and reroute; `:resume`.
3. Versions: branch, restore (a new version), compare (spec, intent, CBS and coverage diffs); estimates; the `:variants` and `:remix` 501 stubs.
4. Impact with cascade, `no_visible_effect` and the coverage delta; exact dirty sets for behavior-only edits (voice locked and unlocked), every World DNA change of §19.5, accent, camera, wardrobe and pacing changes; a re-route auto-proposing removal of stale `compiler_approximation` elements.
5. The Studio UI: NL edit panel with proposal cards, Advanced performance and intent editors, takes gallery, versions tree and compare.
6. DoD: the dirty-set, environment-lock and restore tests, the edit acceptance fixtures for every §2 edit example, and an e2e covering "make him more skeptical" and a compare.

## Post-audit corrections (2026-10, after Phase 14)

- **S3–S6** — the player depended on SSE alone to learn renders were ready, a closed EventSource was never recreated, every node completion refetched every query of the version, and presigned playback URLs were never refreshed.
- The layout was desktop-only (fixed sidebar and grids); failed requests rendered as empty states; error alerts were not announced (`role=status`).

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-5`).

## Product-audit corrections (2026-10-07)

- ROLE-01 (viewers saw every write control enabled), NAV-D17 (broken links waited forever), D2/D9 (previz offered actions the API refuses), D4 (render panels while generating), D5 (Resume for never-generated versions), D6/D11 (Create retries and stale choices), D21, DL-01 (no video download), JOB-CANCEL (no cancel), BREAK-DURATION, BREAK-OFFLINE, BREAK-TWO-TABS.

Found by the product-level audit; evidence, regression tests and fixes in [`PRODUCT_LOGIC_AUDIT_REPORT.md`](../PRODUCT_LOGIC_AUDIT_REPORT.md) (corrected in tag `phase-14-audit`).
