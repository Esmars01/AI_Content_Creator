# Phase 12 — Memory loop, research, templates, brand, packaging and exports: progress report

**Date:** 2026-10-05 · **Status:** DoD passed in mock mode. **Mock engines and the fixture LLM only. No real `embed.text` model is registered (none could be pinned or license-verified: model hosts were unreachable from the build container), `captions_llm` was never run against a real LLM provider (no paid calls), and every dev render carries mock provenance — no generated media was exported outside tests.** · **Path taken:** a fresh container (4 vCPUs, no GPU) with the Phase 11 checkpoint reconstructed from `bundles/creator-engine-phase11.bundle`; Docker Compose infrastructure only (Postgres, Valkey, Temporal, SeaweedFS) because the service images cannot be built here (the Debian package mirror answers 403); the application services ran in-process in the tests. No money was spent.

## Definition of Done

> the memory suites [12] pass; closed-book tests pass (no unsupported claims get through); the template composition, export preset and packaging-limit tests pass. (§40)

| DoD item | Where | Result |
| --- | --- | --- |
| Memory suites [12] | `packages/py/ce_memory/tests/test_loop.py` (write-path decisions, habit merge and promotion thresholds, mock evidence, authored conflicts, edit signatures and preferences, embedding retrieval and the hook guard); `tests/e2e/test_memory_loop_mock.py` (ready → hook embedding and observation habits proposed, never promoted from mock evidence; an authored item indexed by a job; approved → proposed → exported → active with an `exported` usage event; repeated edits → one preference); `apps/api/tests/test_memory_api.py` | pass: 33 tests |
| Closed book: no unsupported claim gets through | `packages/py/ce_research/tests` (`enforce`: web evidence ignored, unreported statistics detected, closed-book unsupported is blocking and not overridable; ingestion and the SSRF guard); `packages/py/ce_director/tests/test_closed_book.py` (a statistic the input never gave is blocking and not overridable; web facts are not evidence; the user's own source supports it and is traceable to its fact); `apps/api/tests/test_research_api.py` (closed-book claims cannot be overridden); `tests/e2e/test_research_mock.py` (a closed-book plan with ingested sources) | pass: 108 tests; exports refuse blocking claims that are not overridden (`apps/api/tests/test_exports_api.py`, below) |
| Template composition | `packages/py/ce_core/tests/test_spec_templates.py`; `apps/api/tests/test_templates_api.py`; `packages/py/ce_director/tests/test_brand_default.py` | pass: 14 tests (strict bodies per kind, composition order and conflicts, apply as an edit proposal with a preview, versioning, brand kits as the default brand of new plans) |
| Export presets | `packages/py/ce_director/tests/test_packaging.py::test_export_presets_match_their_platforms`; the rule "preset of another platform" in `apps/api/tests/test_exports_api.py` | pass |
| Packaging limits | `packages/py/ce_director/tests/test_packaging.py` (verified vs design-default limits, every limit enforced, the template packaging always fits); `apps/api/tests/test_exports_api.py` (edits validated, every export refusal rule); `tests/e2e/test_packaging_mock.py`; `plugins/translate/captions_llm` | pass: 19 tests across these files |

## What was built

- **Memory loop** (ADR 0057): every write path is a `memory_update` job (`MemoryUpdateWorkflow`) — `approved` (persona facts and stances → proposed items), `ready` (hook embedding; measured habits merged per video, promoted only past evidence and confidence thresholds and never from mock evidence), `exported` (activates the video's proposals), `edit_applied` (a preference after repeated same-direction edits), `embed` (authored items). Usage events are logged per write. Conflicts are linked both ways and resolvable (keep one, keep both scoped to creator-version ranges, merge); provenance (source, evidence count, confidence, embedding model) is shown in the creator's memory panel.
- **Embeddings**: `embed.text` adapters run in-process; vectors record their model and cosine is only computed between vectors of the same model; retrieval, the hook repetition guard and research retrieval use them and fall back to keyword forms, saying so. `plugins/embed/text_embed` (ONNX) exists as code tested on a stand-in backend but is **not registered**: no model could be pinned and license-verified here.
- **Persistent research** (ADR 0058): sources (URL through the SSRF guard, uploaded PDF/DOCX/HTML/SRT/VTT/text, pasted notes) ingested by a job into facts with exact spans; hybrid retrieval; claims checked in code after the fact-check stage; the claim ledger across versions with overrides (audited) where allowed; closed book counts only user-provided evidence and its unsupported claims are blocking and not overridable. All fetched and uploaded content is data (I10).
- **Spec templates and brand kits** (ADR 0059): portable slots per template kind, strict validation against configuration and vocabularies, composition with conflicts, apply as an ordinary `from_template` edit proposal, immutable versions; brand kits (logo asset, colors, fonts, caption style) as the default brand of a project's plans, entering `render.final` by content digest with the logo burned in.
- **Caption translation**: the `captions_llm` adapter (`cpu_inproc`, `untested_on_gpu`, no model license asserted — the provider is the operator's) translates ASS/SRT/VTT in batches with the text as data; `captions:translate` is an auto-applied edit; per-language review state that follows the file's content.
- **Packaging and exports**: Director stage 12 (`PackagingWorkflow`) — titles, descriptions, hashtags and thumbnail texts validated against verified platform limits or our design defaults labelled as such, template packaging labelled with its reason when no LLM is usable, thumbnails cut from the final render; exports with every refusal rule (mock provenance, version not ready, wrong preset, unchecked disclosure items, unapproved packaging, blocking claims, unapproved translations when required), an export metadata artifact and audit; migration `0004` (artifact reference types `packaging` and `export`).
- **Web**: sources panel and claim ledger, memory provenance and scoped keep, templates page, brand kits in settings and per project, captions/packaging/export panels with the platform checklist.
- **API**: the routes above, in the OpenAPI document and the generated TypeScript client.

## Tests run and results

- Full Python suite on the Phase 12 working tree (`uv run pytest -q`, Compose infrastructure up, mock engines, CPU-engine assets not fetched): **1848 passed, 2 failed, 14 skipped** in 27 min 39 s. The two failures were real Phase 12 omissions, fixed before this checkpoint: `docs/MODELS.md` had not been regenerated for `captions_llm`, and three new request bodies lacked OpenAPI examples while two `ApplyBody` models collided by name (the templates one is now `TemplateApplyBody`). After the fixes: `apps/api/tests/test_openapi.py`, `tests/phase8`, `tests/phase0` and the template and export API tests pass.
- Skipped (14): tests that need the CPU-engine assets (`make fetch-cpu-assets`; Hugging Face is unreachable from this container) and the PP-OCR golden tests.
- DoD groups re-run after the fixes: memory 33, closed book 108, templates 14, packaging and exports 19, invariants 391 — all pass.
- `make lint`, `make typecheck` (mypy on 756 source files, `tsc`), web unit tests (Vitest: 80 Phase 12 tests in 15 files), `next build`, the migration round trip (downgrade to `0003`, upgrade to head, `alembic check` clean) — pass.
- The checkpoint commit was re-run in a clean clone and from its bundle: see "Checkpoint" below.

## What is untested, and why

- The Playwright flow `apps/web/e2e/phase12.spec.ts` was written but not run at this checkpoint: it needs the whole stack serving the web app, and the service images cannot be built here (run in Phase 14 through the native mode, see `docs/progress/phase-14.md`).
- A real text-embedding model and a real LLM for packaging and caption translation: none registered or called (no license-verified pin; no paid calls).
- Platform limits: every platform's rules are still unverified (`verified_at: null`, D16); packaging uses labelled design defaults.

## Important decisions

ADR 0057 (memory loop and embeddings), ADR 0058 (persistent research and the claim ledger), ADR 0059 (templates, brand kits, caption translation, packaging and exports); D128–D140 in `docs/DECISIONS.md`.

## Known issues and limitations

- Dev renders are `mock_dev` and cannot be exported; the export success path is tested with real-provenance render rows.
- Without a configured LLM, packaging is always the labelled template packaging and translation routes only to the mock translator.
- The abandoned Phase 12 work-in-progress commit `3e4fc12` was not revived; this phase was rebuilt from the Phase 11 checkpoint.

## Final Phase 12 state

Phases 0–12 complete in mock mode. Phase 13 was intentionally skipped by the product owner. Phase 14 (hardening, observability, load test, security review, operations and documentation) follows.

## Checkpoint

`bundles/creator-engine-phase12.bundle` on branch `claude/phase12-14-work`, added by the commit after the checkpoint. It holds `HEAD`, `refs/heads/main` and the tag `phase-12` at the Phase 12 checkpoint `755bdcb5af05470fc99da650aef0b44d95a7222e`, plus the tags `phase-8` … `phase-11` and `phase-11-implementation` of the earlier checkpoints (the same layout as the Phase 11 bundle).

Verified on 2026-10-05:
- `git bundle verify`: "The bundle records a complete history".
- Reconstruction: `git clone creator-engine-phase12.bundle` into an empty directory — `HEAD` is `755bdcb`, the tree is identical to the checkpoint's, `git fsck --full` is clean; after `uv sync --all-packages --frozen` and `pnpm install --frozen-lockfile`, `tests/phase0` and `tests/invariants` pass (511 tests).
- The whole suite on the checkpoint commit in a clean clone (Compose infrastructure up, mock engines, CPU-engine assets not fetched): **1850 passed, 14 skipped, 0 failed** in 27 min 17 s; `make lint`, `make typecheck` (748 source files) and the web unit tests (80) pass.

## Post-audit corrections (2026-10, after Phase 14)

- **A7** — a retried export returned expired presigned URLs. **A6** — the API's `sources` field did not carry `PlanRequest`'s limit of 50.

The historical record above is unchanged. The corrections are in the final code (tag `phase-14-audit` in `bundles/creator-engine-phase14.bundle`, sources in `repo/creator-engine/`); IDs and evidence in [`FINAL_AUDIT_AND_FIX_REPORT.md`](../FINAL_AUDIT_AND_FIX_REPORT.md). This phase's own bundle keeps its original commits and carries an audit note (`git notes --ref=audit show phase-12`).
