# Product Logic Audit Report — AI Creator Engine

**Date:** 2026-10-07 · **Scope:** the merged final state of the technical audit (`main` = `612fcff`, PR #1)
and the product-level audit on top of it (branch `claude/admiring-curie-pm74e9`) · **Companions:**
[`FINAL_AUDIT_AND_FIX_REPORT.md`](FINAL_AUDIT_AND_FIX_REPORT.md) (the technical audit, its baseline),
[`GPU_READINESS_REPORT.md`](GPU_READINESS_REPORT.md)

> Mock mode only. No GPU, no hosted LLM and no paid provider were used. Every result below says what proved
> it: a black-box browser run, a measurement of the produced file, a component or API test, or code reading.
> Nothing marked BLOCKED was counted as PASS.

## 1. Executive Summary

This audit treated the app as a product. A real browser (Playwright, Chromium 141) signed in as real users and
went through each journey through the UI only. The produced files were then checked: frames, audio pitch,
captions, durations, byte hashes and downloads. Each defect found was diagnosed, fixed with a regression
test, and re-tested the same way.

- **109 scenarios** were executed. Final status:
  - 72 PASS
  - 2 FAIL
  - 3 PARTIAL
  - 9 MISSING
  - 5 BLOCKED — MOCK LIMITATION
  - 6 BLOCKED — REQUIRES REAL MODEL/GPU
  - 2 BLOCKED — ENVIRONMENT
  - 6 INTENTIONAL LIMITATION
  - 4 PRODUCT DECISION REQUIRED
- **47 defects were found and fixed**, and every one re-tests PASS:
  - by severity: 0 P0, 4 P1, 13 P2, 27 P3, 3 P4;
  - by kind: 36 wrong behaviour, 10 capabilities that were implemented but unreachable, 1 partial.
- **The four P1s:**
  - an edit proposal leaked to another video and was applied there (EDIT-D1);
  - a creator's voice or look could never be changed, and new creators could not even be created (CR-VOICE, CR-CREATE);
  - a plan that could not be routed hung at "Planning… 5 %" forever (PLAN-FAIL);
  - every video with a designed voice failed to plan (CR-VOICE-TRANSCRIPT).
- **Regression tests:** 67 new test cases were added (54 web, 13 Python, one of them parametrised over six camera
  moves), and 5 existing tests were strengthened.
- **Final regression:** see section 29. Every suite passes. The one exception is the Playwright playback step,
  which needs H.264 and is BLOCKED — ENVIRONMENT as in the baseline.
- **Still open, none blocking GPU testing:**
  - 2 FAIL: the disclosure span is ignored (P3); caption placement `bottom` equals the safe zone (P4);
  - 3 PARTIAL;
  - 9 MISSING capabilities, all P3/P4, which need a product decision or more than a small change.

**GPU readiness: YELLOW** (section 30). The product logic that GPU work depends on is now correct in mock mode:

- property propagation;
- edit isolation;
- recovery;
- cancel and resume.

What remains for GPUs is what only GPUs can show:

- identity consistency;
- acting quality and take variety;
- real voices, including the pitch offset;
- model routing.

There is also a short list of P3 gaps to schedule.

## 2. Audit Objective

Decide whether a user can make, change and deliver videos with persistent creators, and whether each choice
reaches the artifact. Specifically:

- Every choice the user makes reaches the artifact, and only where it should (intended change, no unintended change).
- Every implemented capability can be reached from the UI.
- State survives reloads, restarts and interruptions.
- Failures say what happened and offer a way out.
- Roles are respected in the UI as well as the API.

Fix what is confirmed, within the low-complexity rule, before money is spent on GPUs.

## 3. Environment

| Item | Value |
|---|---|
| Host | Linux 6.18 container, 4 vCPU, 15 GB RAM, no GPU |
| Stack | `make dev-native` (API, scheduler, orchestrator, render worker, CPU worker and web on the host); Postgres (pgvector), Temporal, Valkey and SeaweedFS in Docker |
| Runtimes | Python 3.12.3 (uv), Node 24.21.0, pnpm 10.28.0, FFmpeg 6.1.1, Next.js 16, React 19 |
| Browser | Playwright with Chromium 141.0.7390.37 (open-source build: no H.264 decoding) |
| Engines | All mock adapters; `LLM_PROVIDER=fixture` (21 authored fixtures, otherwise the labelled template planner); `CPU_REAL_ENGINES` assets absent |
| Users | Seeded `Dev Org` members: owner, admin, editor, developer, viewer |

## 4. Exact Commit Tested

- Baseline: `main` at `612fcff` (the merge of PR #1, which carried the technical audit).
- Audit branch: `claude/admiring-curie-pm74e9`. It holds 37 product-audit commits on top of `612fcff`, plus this
  report. They are listed in section 22.
- The final regression (section 29) ran on the last code commit. The final commit, the bundles and the ZIP
  hashes are recorded in `docs/MANIFEST.txt` and `docs/SHA256SUMS.txt`.
- History was not rewritten: no rebase and no force-push.

## 5. Product Capability Inventory

The inventory comes from three sources:

- the 199 OpenAPI operations;
- a static trace of each operation to the control that calls it;
- the browser crawl.

| Area | Capabilities | UI reachability after this audit |
|---|---|---|
| Planning | Create wizard (10 steps: idea/script, creator, world, format, style, voice, duration, language, advanced, plan), previz review, approve, replan, claim ledger | All reachable. Replan only for planned versions (D2). |
| Generation | Build graph, live progress (SSE plus polling), cancel, resume, final and proxy renders, download | All reachable. Cancel added (JOB-CANCEL). Download added (DL-01). |
| Editing | NL edit, structured edits (Advanced performance and intent editors), locks, takes, shot regenerate, scene reorder/remove, restore, branch, duplicate, compare | All reachable. Scene reorder/remove (SCENE-ORDER) and Duplicate (EXTRA-DUP) added. Scene-level regenerate is still API only (SCENE-REGEN). |
| Creators | Create, Creator DNA drafts and approval with adult attestation, identity links (appearance, voice, default outfits and worlds), appearance pack, voice design/select/test/lexicon, wardrobe, memory, tests, consistency, consent | Create, identity links (CR-CREATE, CR-VOICE), memory add/delete (MEM-CRUD) and lexicon remove added. Appearance DNA edit, outfit revision and memory history are still API only. |
| Worlds | Create, World DNA, plates, versions, continuity | Create added (WORLD-NEW). |
| Publish | Captions (download, translate, review), packaging, export with disclosure checklist, extra platform renders, past exports | Caption downloads (DL-02), Render for platform (EXTRA-RENDER) and past-export downloads (EXPORT-REDL) added. Export of mock renders is refused by design. |
| QC | QC report, critique, consistency check, ratings, benchmarks (admin) | Consistency run added (EXTRA-CONSIST). |
| Research and templates | Sources, claim ledger, spec templates, brand kits | Template target picker added (TPL-UUID). |
| Operations | Jobs, GPU fleet, models, developer viewers | Reachable. |
| Team | Members, roles, invitations, API keys | **Not reachable** (TEAM, API-KEYS). |

## 6. Screens and Routes Tested

All 20 routes were exercised in the browser:

- `/login`, `/` (Dashboard), `/create`, `/projects`, `/projects/[id]`;
- `/videos/[id]` (Studio), `/videos/[id]/versions/[id]/previz`, `/videos/[id]/compare`;
- `/creators`, `/creators/[id]` (9 tabs), `/worlds`, `/worlds/[id]`;
- `/templates`, `/ratings`, `/models`, `/models/benchmarks/[id]`, `/jobs`, `/jobs/[id]`, `/gpu`, `/settings`, `/developer`.

Each was opened as an editor, and the main ones as a viewer.

Broken deep links were tested for:

- a random version;
- a malformed version id;
- a random previz;
- a random project, job, video and creator.

At 390, 768 and 1440 px, nothing scrolls horizontally after the fix in E2E-RESPONSIVE.

## 7. UI Controls Tested

Every control on the journeys below was driven by its accessible role and name, never by CSS or API shortcuts.
The controls covered were:

- the wizard steps and every select;
- Plan video, Approve and generate, Regenerate plan;
- Propose, Apply, the scene and time-range selection;
- Save locks, take Select, shot Regenerate;
- Restore, Resume, Branch, Duplicate, Cancel, Download;
- caption downloads and Translate;
- packaging edit, thumbnail and Save and approve;
- Export, Render for platform, Run consistency check;
- Create creator and Create world;
- the appearance, voice, wardrobe and identity-link controls, attestation and approve;
- memory Add and Delete;
- the template picker, Preview and Propose edit;
- the ratings buttons.

The accessibility scan found:

- 0 unlabeled inputs;
- 0 unnamed buttons or links;
- 0 images without alt;
- one h1 per page;
- no duplicate ids.

The first Tab reaches "Skip to content", and focus is visible.

## 8. User Journeys Tested

| Journey | Result |
|---|---|
| New user: sign in → Create (all choices) → previz → approve → progress → play → download | PASS (CRE-01, DL-01); in-browser playback BLOCKED — ENVIRONMENT (E2E-PLAY) |
| Iterative: NL edits scoped and unscoped, structured edits, locks, takes, scene reorder, restore, compare | PASS after fixes (EDIT-D1, NL-02, EDIT-D16, TAKE-SEL, SCENE-ORDER, BREAK-RESTORE) |
| Creator lifecycle: new creator Maya → face pack → designed voice → outfit → identity links → attest → approve → video with Maya | PASS after fixes (CR-JOURNEY). Maya's frames name her, her outfit and world; her voice is ~195 Hz against Alex's ~130 Hz |
| World lifecycle: create world → World Studio draft | PASS (WORLD-NEW) |
| Publish: captions (translate, review, download), packaging, Render for platform, export | PASS; export of mock renders refused by design (EXPORT-MOCK) |
| Team member: viewer and developer roles | PASS after fixes (ROLE-01, ROLE-02) |
| Recovery: impossible plan, cancel, resume, worker kill | PASS after fixes (PLAN-FAIL, JOB-CANCEL, JOB-RESUME, BREAK-INTERRUPT) |
| Multi-tab and multi-video | PASS after fixes (EDIT-D1, BREAK-TWO-TABS) |

## 9. Feature-by-Feature Results

The full matrix is in the appendix, with these columns:

- ID, FEATURE, USER JOURNEY, USER ACTION, EXPECTED, ACTUAL;
- UI, BACKEND, ARTIFACT;
- INTENDED CHANGE, UNINTENDED CHANGE, PERSISTENCE;
- STATUS, SEVERITY, ROOT CAUSE, FIX, REGRESSION TEST, GPU REQUIRED?;
- FOUND AS: the status before the fix.

Final counts by status:

| Status | Count |
|---|---|
| PASS | 72 |
| FAIL | 2 |
| PARTIAL | 3 |
| MISSING | 9 |
| BLOCKED — MOCK LIMITATION | 5 |
| BLOCKED — REQUIRES REAL MODEL/GPU | 6 |
| BLOCKED — ENVIRONMENT | 2 |
| INTENTIONAL LIMITATION | 6 |
| PRODUCT DECISION REQUIRED | 4 |
| **Total** | **109** |

## 10. User-Intent Propagation

**Every wizard choice reaches the spec, and where the mock can show it, the MP4** (CRE-01, measured):

- mode;
- platform and its preset (1080×1920);
- target versus measured duration;
- world, shown as the background colour;
- camera profile (look and motion);
- caption style (karaoke highlight colour);
- music mood;
- takes per shot;
- strategy pack, recorded in the spec.

**A creator's identity reaches the video.** This now holds after CR-VOICE, CR-VOICE-TRANSCRIPT and
OUT-LABELS. The keyframe of Maya's video reads "creator: Maya / wardrobe: denim jacket / world: Alex's home
office", the acting state is drawn, and the dialogue f0 is about 195 Hz (Alex: about 130 Hz).

**Edits reach the artifact**:

- acting: "state: skeptical" in the scoped shot only (OUT-01);
- take selection: the frame reads "take 2" (TAKE-SEL);
- scene order: the MP4 opens on scn_2's shot (SCENE-ORDER);
- platform or aspect change: the render preset follows (OUT-ASPECT);
- camera moves: all nine move types change the frames (CAM-MOVES);
- the cast member's voice offsets (OUT-PROSODY);
- the camera microphone: the room sound now rebuilds (OUT-ROOM).

**Without an LLM**, the template planner:

- speaks the idea verbatim (LLM-01);
- understands only common edits, and refuses others honestly (NL-01, NL-WARMER, TPL-PLANNER);
- ignores authored memory (LLM-MEMORY).

## 11. Before/After Output Verification

Every comparison was made on the downloaded final MP4s, never on API status:

- per-frame grey-level diffs at 2 fps;
- decoded PCM audio;
- `ffprobe` durations and streams;
- crops of the burned labels;
- an f0 estimate;
- SHA256.

| Change | Before → after evidence |
|---|---|
| "make him more skeptical", last scene only | Frames differ only at 31–34.5 s; sht_9 'state: warm' → 'state: skeptical'; audio differs only from 31 s (OUT-01) |
| Caption translation | 0 changed frames, audio diff 0.0; German .ass downloadable (OUT-02) |
| Take tk_1_1 → tk_1_2 | Frame at 1.5 s 'take 1' → 'take 2'. The block diff misses it because mock takes differ only in the label (TAKE-SEL, MOCK-TAKES) |
| Move scn_2 first | v3 opens on scn_1's shot; v4 opens on sht_3 (scn_2); 68 of 72 sampled frames changed; same 35.95 s (SCENE-ORDER) |
| Restore v1 | Restored MP4 byte-identical to v1 (sha256 808241330ecf…) (BREAK-RESTORE) |
| New creator voice | f0 195 Hz (Maya) vs 130 Hz (Alex) (CR-VOICE) |
| Duplicate video | Ready in 8 s from the cache, the same final render (EXTRA-DUP) |
| Worker killed at 57 % | After restart the MP4 decodes completely: 174 frames, 5.8 s (BREAK-INTERRUPT) |

The earlier output-truth pass found five properties that were accepted and validated, then silently did
nothing:

- six camera move types;
- the aspect and platform edit;
- the cast voice prosody;
- the room microphone key;
- the mock labels for outfit and world.

All five are fixed and verified.

## 12. Property Isolation

- A scene-scoped edit changes only that scene's frames and audio (OUT-01). Earlier shots stay identical in frames
  and audio.
- A caption translation leaves picture and sound identical (OUT-02).
- An edit proposal is now isolated by version and video (EDIT-D1). Before the fix, a proposal made on video A
  showed in video B's Studio, and Apply changed A while the user worked on B.
- Restore leaves the old version untouched and reproduces it byte for byte (BREAK-RESTORE).
- A lock change re-builds nothing visible and keeps the selected take (D3, TAKE-SEL).
- Remaining: a disclosure effect scoped to one scene is burned over the whole video (DISCLOSURE-SPAN). This is
  over-disclosure, so it is safe, but it is wrong.

## 13. Cross-Feature Propagation

- **Creator → video:** the voice, appearance, outfit and default world linked on the creator version are used by
  new videos (CR-VOICE, CR-JOURNEY). The creator's default world is preselected in the wizard as "— default".
- **Voice version → creator:** approving a new voice version does not move the creator. The creator draft has to
  link it, and the UI now says which version is linked and whether it is "the voice's current version".
- **Camera profile → audio:** the microphone of the camera profile now keys the room sound (OUT-ROOM).
- **Edit → captions, mix, render:** reorder moves the captions and segments with the scenes (SCENE-ORDER);
  pacing and script edits re-time the captions (output-truth pass).
- **Claim ledger → Approve:** an override in the ledger now counts in the Approve card (D20).

## 14. State Machine Results

The version states are:

`planned → previz_running → previz_ready → approved → generating → ready | partial | needs_review | failed | cancelled`

and every state is shown correctly in the Studio. Checked:

- **Approve:** only from `previz_ready`. A double click and a second, stale tab give exactly one generate job; the
  stale tab is told "approval needs a previz_ready version; this one is generating" (BREAK-DOUBLE-APPROVE).
- **Render-dependent panels:** wait for the render, disabled with a reason (D4).
- **Resume:** offered only for versions that started generating (D5).
- **Replan:** offered only for planned versions (D2).
- **Approve while a world approval is pending:** blocked with a link to the World Studio (D9).
- **Cancel:** `generating → cancelled` in 5.2 s, with 47 nodes cancelled (JOB-CANCEL).
- **Resume after cancel:** `cancelled → ready` in 7.4 s from the cache (JOB-RESUME).
- **A plan that cannot be routed:** the job is now `failed` with a reason (PLAN-FAIL). Before, it stayed
  `running` forever.
- **Creator approval:** needs approved appearance and voice links plus the adult attestation (CR-JOURNEY).

## 15. Persistence Results

- **Survives a full stack restart:** creators, voices, worlds, versions, proposals, memory and packaging.
- **A reload during generation** picks the run back up by itself (BREAK-REFRESH).
- **Packaging:** the edited title survives a thumbnail change and a reload (PKG-D7, PKG-D8).
- **Memory:** an added item persists; a deleted item becomes `forgotten` with its text erased (MEM-CRUD).
- **Navigation:** back and forward keep the version in the URL (BREAK-BACK).
- **A proposal open on a version** stays when another tab creates a newer version (BREAK-TWO-TABS).

## 16. Async / Interruption Results

- **Workers killed mid-generation:** the render and CPU workers were `kill -9`ed at 57 % and restarted 60 s
  later. The job resumed and was ready 24 s after the restart; the file is whole (BREAK-INTERRUPT).
- **Network drop:** a request made offline waits and is sent when the connection returns, and the app now says
  so (BREAK-OFFLINE).
- **Edits, translations and approvals while a version generates:** each creates a derived version, and both
  versions finish (BREAK-EDIT-WHILE-GEN, BREAK-TRANSLATE-WHILE-GEN).
- **Session expiry:** sends the user to `/login?next=<page>` (BREAK-SESSION).

## 17. Error / Recovery Results

- **An impossible plan** (Azerbaijani: no voice speaks it):
  - The previz page now says "Planning stopped / Planning failed: No available engine can do voice.tts for this
    video (…)" within 1.2 s and offers "Plan a video again".
  - The video's Studio offers the same.
  - Remaining P4: the message lists every engine (PLAN-MSG).
- **Broken links:** they now say what is missing ("This version/project does not exist") instead of waiting
  forever (NAV-D17).
- **Packaging without an LLM:** says in words that a template wrote it (PKG-MSG).
- **Inputs the API would reject:** refused before sending, with a message:
  - an invalid duration (BREAK-DURATION);
  - an over-long instruction (BREAK-LONG);
  - a half or inverted time range (EDIT-D16);
  - an empty idea (BREAK-EMPTY-IDEA).
- **Markup and script in user text:** shown as text, never executed (BREAK-XSS).

## 18. UX / Discoverability Results

Fixed:

- **Roles:** write controls are disabled for viewers, with the reason (ROLE-01).
- **Downloads:** videos (DL-01), caption files (DL-02), past exports (EXPORT-REDL).
- **Stale errors:** removed (D21).
- **Double submits:** voice Select (D14), templates and brand kits (D13–D15).
- **Wizard choices that go stale:** dropped (D6, D11).
- **Upload input:** cleared (D12).
- **Templates:** no longer need a pasted UUID (TPL-UUID).
- **Tablet layout:** the dashboard wraps (E2E-RESPONSIVE).

Remaining:

- touch targets under 24 px on dense pages (P4, BREAK-PHONE);
- 32 Tabs from the top to the edit instruction (P4).

## 19. Implemented-but-Unreachable Features

These were implemented in the API but not reachable from the UI, and the audit made them reachable. Each is
verified in the browser or by a component test:

- create a creator; create a world;
- link a creator's appearance, voice, outfits and worlds;
- cancel a job;
- duplicate a video;
- render for another platform;
- run a consistency check;
- download caption files;
- download past exports;
- author and delete memory;
- reorder and remove scenes;
- pick a template's target video;
- remove a lexicon term.

Still API only. Each needs more than a small change or a product decision:

- scene-level regenerate (SCENE-REGEN);
- memory history (MEM-HISTORY);
- project archive (PROJECT-ARCHIVE);
- outfit revision (WARDROBE-REV);
- appearance DNA edit (APPEARANCE-EDIT);
- API keys (API-KEYS);
- take observation scores (TAKE-OBS);
- provenance verification (PROV-VERIFY);
- members, roles and invitations (TEAM);
- the Assets library (ASSETS).

## 20. Product Gaps

Product decisions required:

- **Team management:** there is no members, roles or invitations UI and no invitation-acceptance page, so the
  only way to add a person is the API (TEAM).
- **Effects:** only the AI disclosure is rendered (EFFECTS).
- **Caption language** without a translation (CAP-LANGUAGE).
- **Video-level intent:** the edit changes nothing in the render (INTENT-VIDEO).

Deferred features:

- Assets library (ASSETS);
- variants and remix, which answer 501 (VARIANTS);
- digital-twin consent (CONSENT).

No backend exists for:

- renaming or deleting a video;
- archiving a creator or world;
- duplicating a scene;
- retrying a failed plan with its original request (the failed job does not keep it).

## 21. Confirmed Bugs

47 were found. The matrix gives each one with its root cause:

| Severity | IDs |
|---|---|
| P1 (4) | EDIT-D1, CR-VOICE, PLAN-FAIL, CR-VOICE-TRANSCRIPT |
| P2 (13) | NL-02, DL-02, PKG-D7, PKG-D8, CR-CREATE, CAM-MOVES, OUT-ROOM, OUT-ASPECT, OUT-PROSODY, JOB-CANCEL, WORLD-NEW, MEM-CRUD, SCENE-ORDER |
| P3 (27) | ROLE-01, EDIT-D16, NAV-D17a/b/c/e, DL-01, PKG-MSG, OUT-LABELS, D2, D4, D5, D6-D11, D9, D13-D15, D14, D20, EXTRA-DUP, EXTRA-RENDER, EXTRA-CONSIST, BREAK-DURATION, BREAK-LONG, BREAK-OFFLINE, BREAK-TWO-TABS, TPL-UUID, EXPORT-REDL, E2E-RESPONSIVE |
| P4 (3) | D12, D21, LEXICON-REMOVE |

One of them, E2E-RESPONSIVE, was introduced by this audit's own JOB-CANCEL fix. The regression run caught it
and it was fixed.

## 22. Fixes Applied

The fixes are in 37 commits on `claude/admiring-curie-pm74e9` after `612fcff`, one logical change each.

**Backend and render:**

| Commit | Fix |
|---|---|
| `96cca37` | PLAN-FAIL: fail_job activity and workflow handlers |
| `5fcebf8` | NL-02: template fallback when a fixture slot does not resolve |
| `9ea980e` | CAM-MOVES: camera ramps for push/pull/pan/tilt/whip/static hold; `post.camera` impl 4 |
| `7eda0fe` | OUT-ROOM: `audio.room` keys on the mic profile |
| `387abd1` | OUT-ASPECT: `set_meta` re-derives `render.outputs` |
| `9f3d4a5` | OUT-PROSODY: cast voice offsets applied; `compile_voice` impl 3 |
| `ae1b260` | OUT-LABELS: mock frames name outfit, world and acting; plate edits draw no figure |
| `6682e4d` | PKG-MSG |
| `909b33c` | CR-VOICE-TRANSCRIPT |
| `d009e8b` | D14: idempotent candidate select |

**Web:**

| Commit | Fix |
|---|---|
| `a95ebd3` | D1, D16 |
| `53a84bf` | D7, D8, DL-01, DL-02 |
| `6ef9384` | ROLES |
| `41488e3` | JOB-CANCEL |
| `2a2ea88` | NAV-D17 |
| `3dea453` | D2, D9 |
| `9559c3a` | D4 |
| `f1980dc` | D5 |
| `4735824` | D6, D11 |
| `35e1fa0` | D12 |
| `7bda4b5` | D13, D15 |
| `bf8e85f` | EXTRA |
| `9f19a9c` | CR-VOICE, CR-CREATE |
| `09ae6b0` | Failed or cancelled job states |
| `457fc8e` | D20 |
| `81611e9` | D21 |
| `1e226df` | Offline notice |
| `e6cd950` | Two tabs |
| `fbb883b` | Duration |
| `be2b1e4` | Instruction limit |
| `bed6a26` | Template picker |
| `ba739f0` | Memory |
| `a7e05f2` | Scene order |
| `adb9213` | Plan again |
| `de66643` | Tablet layout |
| `3ead74c` | Past exports |
| `6e5b612` | Lexicon |

The `impl_version` bumps (§12.2) make every changed output rebuild instead of being served from the cache.

## 23. Regression Tests Added

67 new test cases were added.

**Python (13 new):**

- `test_workflows.py::test_a_crashed_plan_activity_still_fails_the_plan_job`
- `test_planning_api.py::test_planning_in_a_language_no_voice_speaks_fails_the_job_with_a_reason`
- `test_studio_api.py::test_a_voice_planning_cannot_read_is_refused_at_approval`
- `test_graph.py::test_the_room_sound_keys_on_the_camera_mic`
- `test_acceptance_edits.py::test_make_him_more_skeptical_with_nothing_selected_changes_the_whole_video`
- three `test_meta_edits.py` cases
- `ce_render` `test_planned_camera_moves_change_the_picture` (parametrised over six moves) and `test_a_static_hold_stops_the_handheld_motion`
- e2e: `test_camera_moves_mock`, `test_mock_labels_mock`, `test_voice_prosody_mock`

**Web (54 new):** in these files:

- `edit-panel`, `export-panels`, `player`, `role-gating`, `cancel-job`, `missing-version`
- `plan`, `studio-panels`, `create/page`, `research-panels`, `templates/page`, `reachability`
- `creator-identity`, `creator-memory`, `creator-voice`
- `offline-notice`, `pin-version`, `create-form`

**Strengthened (5):**

- `test_studio_mock`: transcript, second Select;
- `test_packaging_mock`: user-facing fallback;
- `test_dirty_sets`: the camera-profile expectation corrected;
- `test_contracts`: wire-only field;
- the mock voice goldens: only the new key.

Each fix's test was confirmed to fail on the code before the fix.

## 24. Remaining Issues

| ID | Status | Severity | What |
|---|---|---|---|
| DISCLOSURE-SPAN | FAIL | P3 | A scene-scoped disclosure is burned over the whole video |
| CAP-PLACEMENT | FAIL | P4 | Caption placement `bottom` gives the same ASS as `platform_safe_zone` |
| REALISM-ABR | PARTIAL | P3 | `post.realism` ABR encodes are not byte-deterministic (perceptually identical, about 56 dB PSNR) |
| PLAN-MSG | PARTIAL | P4 | The failed-plan reason lists every engine |
| QC-DURATION | PARTIAL | P4 | `qc.render` never checks the duration (the previz discloses it) |
| SCENE-REGEN, PROJECT-ARCHIVE, WARDROBE-REV, APPEARANCE-EDIT, API-KEYS, REACTION-RANGE | MISSING | P3 | See section 19 |
| MEM-HISTORY, TAKE-OBS, PROV-VERIFY | MISSING | P4 | See section 19 |
| BREAK-PHONE (touch targets), BREAK-DOUBLE-APPROVE (wording) | PASS with notes | P4 | Small UX polish |

## 25. Mock Limitations

These cannot be judged on mock output:

- **Face identity:** the face colour is seeded per prompt (MOCK-FACE).
- **Take variety:** takes differ only in their label (MOCK-TAKES).
- **Regenerate:** the avatar frames have no seed (MOCK-REGEN).
- **Emotion intensity:** only the emotion name is drawn (MOCK-INTENSITY).
- **Caption translation text:** "[de] <English>" (MOCK-TRANS).
- **The voice:** tone bursts, not words.

The output-truth pass also lists behaviours that live only in the behavior track and coverage report:

- gaze, gestures, events;
- posture;
- framing and angle (these only re-seed the colour).

## 26. Real Model/GPU Requirements

**Needs a hosted LLM (not a GPU):**

- script writing to length and strategy (LLM-01, DUR-TEMPLATE);
- natural-language edits beyond the template rules (NL-01, NL-WARMER, TPL-PLANNER);
- memory used in plans (LLM-MEMORY);
- caption translation.

**Needs real models on GPU:**

- identity consistency across shots;
- acting quality, take variety and ranking;
- real voices: accent, emotion, the pitch offset (PITCH-REAL);
- lipsync;
- world plates and outfits as pictures;
- real provenance (C2PA and watermark) for export (EXPORT-MOCK).

**Needs Google Chrome (environment):**

- in-browser H.264 playback (E2E-PLAY, TAKE-01).

## 27. Intentional Limitations

- Export refuses mock-provenance renders (EXPORT-MOCK).
- The duration tolerance is ±25 % and is disclosed in previz (DUR-01, DUR-TEMPLATE).
- The Assets nav item is labelled "later" (ASSETS).
- Variants and remix answer 501 (VARIANTS).
- Consent writes are gated off until digital twins (CONSENT).
- Without an LLM, planning uses the labelled template planner.

## 28. What We Almost Missed

- **CR-VOICE-TRANSCRIPT** appeared only when a brand-new creator went all the way to a video. Every existing test
  used the seeded creator, whose voice reference has a transcript. Without the full new-creator journey this P1
  would have shipped. Its symptom was also hidden behind a second defect: "This version does not exist".
- **The take selection *did* reach the video.** The automatic frame diff said "0 frames changed". Reading the
  burned label showed "take 2". Without that check the audit would have reported a false FAIL.
- **NL-02 was hidden** because every test and the demo pass a selection. Only the Studio's own placeholder
  instruction, with nothing ticked, failed.
- **The tablet overflow was caused by this audit's own Cancel button** (E2E-RESPONSIVE). The full Playwright
  regression caught it, not the component tests.
- **Two tabs on one video** lost a proposal silently. No single-tab test can see this.
- **A failed plan leaves no version at all**, so the Studio, not only the previz page, needed a way out.

## 29. Final Regression Results

All of the following ran on the final code. The stack was `make dev-native`, with the infrastructure running.

| Suite | Command | Result |
|---|---|---|
| Python, full | `CE_REQUIRE_INFRA=1 uv run pytest` | see the run record below |
| Web unit and contract | `pnpm --filter @ce/web run test` | see the run record below |
| Lint | `ruff check .`, `ruff format --check .`, `eslint .`, `prettier --check .` | clean |
| Types | `mypy` (772 files), `tsc` (api-client and web) | clean |
| Spec and config | `scripts/verify_spec.py`, `ce config validate --env dev` / `--env prod` | 0 errors, 0 warnings |
| Demo | `make demo` | done in 325 s: plan, previz, approve, build, edit, template, translation, packaging, export guard |
| Playwright | `CE_E2E_CHROME=… playwright test` (9 specs) | see the run record below |

Run record (final commit):

<!-- REGRESSION-RECORD -->

## 30. GPU Readiness Decision

**YELLOW: go for GPU testing, with the known list.**

Why not RED:

- No P0 or P1 is open.
- Every journey a GPU run depends on works end to end in mock mode and is verified on the artifact:
  - planning;
  - approval;
  - generation with cancel, resume and worker-kill recovery;
  - edits that change only what they should;
  - new creators whose voice and look reach the video.

Why not GREEN:

1. **What GPUs are for cannot be shown on mock output.** That is identity, acting and voice quality
   (section 25). The pitch offset is honoured only by the mock voice (PITCH-REAL).
2. **Open P3 gaps change what a GPU run would show:**
   - the disclosure span (DISCLOSURE-SPAN);
   - ABR non-determinism (REALISM-ABR), which makes byte comparison of GPU re-runs meaningless (use PSNR);
   - the missing scene-level regenerate (SCENE-REGEN).
3. **No hosted LLM was tested.** NL edits and plans beyond the templates are unproven.
4. **The GPU control plane is CPU/STAND-IN validated only** (`GPU_READINESS_REPORT.md`, section 3).

**First GPU session:**

1. Follow `GPU_READINESS_REPORT.md` section 6.
2. Re-run the journeys in section 8 with one real adapter family at a time. Start with the voice: check that
   `pitch_semitones` is honoured.
3. Then avatar or lipsync: identity across shots and take variety.
4. Compare versions with PSNR or SSIM, not SHA.

---

## Appendix — Test Matrix

| ID | FEATURE | USER JOURNEY | USER ACTION | EXPECTED | ACTUAL | UI | BACKEND | ARTIFACT | INTENDED CHANGE | UNINTENDED CHANGE | PERSISTENCE | STATUS | SEVERITY | ROOT CAUSE | FIX | REGRESSION TEST | GPU REQUIRED? | FOUND AS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NAV-01 | Navigation | New user | Sign in as editor; follow every sidebar link and the first detail page behind each list (crawler, 60 screens) | Every screen renders a heading and its controls | All 12 nav routes and detail pages render; Assets nav item disabled with 'later' | renders | 200s | screenshots out/explore-editor_*.png | — | none | — | PASS | — | — | — | — | no | — |
| OUT-01 | Edit output truth | Iterative | Compare final MP4 of v1 and v2 of the demo video (edit 'make him more skeptical' scoped to the last scene) | Only the last scene's frames and audio change; acting label becomes skeptical | Frames differ only at 31-34.5 s (last shot sht_9 'state: warm' → 'state: skeptical', face shading); audio differs only from 31 s; shots 1-8 identical | Performance timeline shows Skeptical at the end | set_acting + add_behavior_event on scn_5 | out/artifacts/sheet_end.png, sheet_v1_v2.png | yes | none (earlier shots byte-identical in frames, audio diff only re-encode noise) | v1 unchanged after edit | PASS | — | — | — | — | real acting quality needs GPU | — |
| OUT-02 | Caption translation output | Iterative | Compare v2 and v3 (German captions translated) final renders | Picture and sound identical; German captions available as a track/file | Picture and sound identical between v2 and v3 (0 changed frames, audio diff 0.0); captions.de.ass downloadable after DL-02 (its text is the mock's '[de] <English>', see MOCK-TRANS) | — | v3 derived | out/artifacts | partial | none | — | PASS | — | — | — | — | no | incomplete check |
| DUR-01 | Duration intent | New user | Demo brief asks for a 30-second TikTok | Final about 30 s or a disclosed deviation | Final 35.74 s; previz shows 'estimated 35.74 s' before approval; within the documented ±25 % tolerance (config spec.duration_tolerance) | previz shows the estimate | target 30, estimate 35.74 | ffprobe 35.74 s | partial | — | — | INTENTIONAL LIMITATION | P4 | — | — | — | no | — |
| CRE-01 | Create wizard → spec → artifact | New user | Create wizard: Dev project, idea text, creator Alex, world Modern office, mode Educational, platform YouTube Shorts, camera Desktop webcam, captions Karaoke box, music 'calm piano', 15 s, English, Draft, 2 takes, Myth vs reality; Plan video; Approve and generate | Each choice reaches the spec and, where the mock can show it, the MP4 | Plan 8 s, build 95 s, Ready. Spec: mode educational, platform youtube_shorts, preset youtube_shorts_1080x1920_30 (MP4 1080x1920), target 15 → measured 14.7 s, world Modern office v0040 (MP4 background steel-blue vs cyan home office), camera profile webcam, captions karaoke_box (active word purple), music cue mood 'calm piano', 2 takes per shot (sht_1 uses selected take 2 in the MP4), strategy_pack myth_vs_reality recorded | summary step lists choices; previz shows target 15.0 / measured 14.7 | spec fields set | out/artifacts/sheet_j1.png; 01a1164a_v1_final.mp4 14.70 s 1080x1920 | yes | none seen | — | PASS | — | — | — | — | visual quality of camera profile needs GPU | — |
| LLM-01 | Planning without an LLM | New user | Plan a non-fixture idea | A written script, scenes per strategy | Labelled 'template plan': the user's idea text is spoken verbatim as the script ('Explain in a short educational video…'), one scene, strategy pack not applied; brief.assumptions says so | 'template plan' badge on previz | template Director | captions show the idea text | no (by design without LLM) | — | — | BLOCKED — REQUIRES REAL MODEL/GPU | — | — | — | — | needs a hosted LLM (not a GPU) for script writing, strategy, NL edits | — |
| NL-01 | NL edit interpretation | Iterative | Propose 'make the hook more energetic' on the demo video | An edit or an honest refusal | Proposal failed: 'Without an LLM only common edits are understood; rephrase or use operations.' (status failed, ops []) | to be checked in screenshot | template edit planner not_understood | — | honest refusal | none | — | BLOCKED — REQUIRES REAL MODEL/GPU | — | — | — | — | hosted LLM | — |
| TAKE-01 | Takes gallery thumbnails | Iterative | Open Shots and takes | A frame of each take | Black boxes: thumbnails are <video #t=0.5> and Playwright's Chromium cannot decode H.264 | black | take video URLs present | j1-03-studio.png | — | — | — | BLOCKED — ENVIRONMENT | — | — | — | — | no (needs Google Chrome) | — |
| EDIT-D1 | Edit proposal scope | Iterative / multi-video | Video A: type 'make him more skeptical', tick scn_1, Propose (card 'proposed'). Sidebar → Projects → project → video B (no reload). Click Apply on the card shown in B's Edit panel | B's Edit panel shows no card (A's proposal belongs to A); nothing B does changes A | Before: B's Edit panel shows A's proposal ('Acting in scn_1' — B has no scn_1). Apply applies A's edit: A gets version 2 (1→2), B unchanged (2→2); URL becomes /videos/B?version=<A's new version> and the page shows A's title and spec under B's URL; 404s for /versions/<id>/intent, previz, coverage — After fix: B's Edit panel shows no card; A still shows its own; Apply on A creates A v3 and stays on A (browser, verify1-d1) | wrong card, wrong page content after Apply | A's proposal applied (correct for the proposal, wrong for the user's intent) | A rebuilt; B untouched | no | A changed while the user worked on B | global zustand activeProposal survives client navigation | PASS | P1 | lib/store.ts activeProposal is one global value, not keyed by video/version; edit-panel ProposalCard navigates with the current page's videoId | a95ebd3 proposals keyed by version; Apply navigates to the proposal's own video | edit-panel.test.tsx (D1 ×2) | no | FAIL P1 |
| NL-02 | NL edit, whole video (the placeholder instruction) | Iterative | Studio of a 1-scene video: type 'make him more skeptical' (the Instruction placeholder), no scene ticked, Propose | The whole video (the host) becomes more skeptical, or a clear refusal | Before: Card: 'failed · Planned by: structured · no @selection in this edit'. The authored LLM fixture for this instruction hard-codes scope @selection; with no selection the reference does not resolve and the edit fails instead of falling back to the template planner (which handles 'more <emotion>' with scope @him) — After fix: Card: 'proposed · Planned by: template · Acting: displayed skeptical, intensity +0.2' for the whole video (browser verify1) | cryptic failure text, a Reject button on a failed card | proposal failed unknown_reference | none | no | — | failed proposal kept in Earlier proposals | PASS | P2 | ce_director/edit.py plan(): a fixture answer whose slot is absent raises EditError instead of counting as a fixture miss; message is internal | 5fcebf8 an unresolvable fixture slot falls back to the template planner; friendly message | test_acceptance_edits.py::test_make_him_more_skeptical_with_nothing_selected_changes_the_whole_video | real LLM would not reference an absent slot; mock-mode behaviour | FAIL P2 |
| ROLE-01 | Roles in the UI | Team member (viewer, developer) | Sign in as viewer and as developer; open a Studio, Settings, Create; press Propose | Write controls hidden or disabled with a reason; backend refuses | Before: Every write control shown and enabled exactly as for an editor (Restore, Regenerate, Write packaging, Critique, Create brand kit, Next…); Propose → 403 'your role (viewer) does not allow write_content' shown after the attempt — After fix: Viewer: Restore, Regenerate, Write packaging, Critique, Plan video disabled with the title 'Your role (viewer) can view but not change content.'; a note on each page (browser verify2) | enabled controls | 403 (correct) | out/roles-*-studio.png | blocked (yes) | — | — | PASS | P3 | the web app never reads the membership role | 6ef9384 useCan/deniedReason from /v1/me; RoleNote | role-gating.test.tsx | no | FAIL P3 |
| ROLE-02 | Roles enforced by the API | Team member | viewer/developer Propose; owner/admin/editor Propose | viewer/developer refused; others allowed | as expected | — | 403 / 202 | — | yes | none | — | PASS | — | — | — | — | no | — |
| EDIT-D16 | Edit time range | Iterative | Studio Edit: 'make him less skeptical', Range start 2, end empty, Propose | Ask for the end, or say the range was not used | Before: Proposal planned for the whole video (selection null); no message about the ignored range — After fix: 'Enter both a start and an end, or leave both empty.' / 'The end must be after the start.'; Propose disabled (browser verify1) | no warning | selection {kind: edit} | — | no | scope widened to the whole video | — | PASS | P3 | edit-panel.tsx drops a half or inverted range silently | a95ebd3 range validation | edit-panel.test.tsx (D16) | no | FAIL P3 |
| NAV-D17a | Deep link to a missing version | Returning user / shared link | Open /videos/<id>?version=<random uuid> | 'This version does not exist' with a way back | Before: 'Creating the new version…' forever — After fix: 'This version does not exist. Open the video's current version' within the poll budget (browser verify1) | spinner text, wrong | 404 | out/d17-versionRandom.png | — | — | — | PASS | P3 | — | 2a2ea88 useVersionLookup ends the wait on 404/422 | missing-version.test.tsx | no | FAIL P3 |
| NAV-D17b | Deep link with a malformed version id | Returning user | Open /videos/<id>?version=not-a-uuid | An error message | Before: Blank main area (skeleton) forever — After fix: 'This version does not exist. Open the video's current version' within the poll budget (browser verify1) | blank | 422 | out/d17-versionGarbage.png | — | — | — | PASS | P3 | — | 2a2ea88 useVersionLookup ends the wait on 404/422 | missing-version.test.tsx | no | FAIL P3 |
| NAV-D17c | Deep link to a missing previz | Returning user | Open /videos/<id>/versions/<random>/previz | An error message | Before: 'Planning… This page updates by itself.' forever — After fix: 'This version does not exist. Open the video's current version' within the poll budget (browser verify1) | misleading | 404 | out/d17-previzRandom.png | — | — | — | PASS | P3 | — | 2a2ea88 useVersionLookup ends the wait on 404/422 | missing-version.test.tsx | no | FAIL P3 |
| NAV-D17d | Deep links to missing job, video, creator | Returning user | Open /jobs/<random>, /videos/<random>, /creators/<random> | 'does not exist' | 'This job/video/creator does not exist.' | correct | 404 | — | — | — | — | PASS | — | — | — | — | no | — |
| NAV-D17e | Deep link to a missing project | Returning user | Open /projects/<random> | 'This project does not exist' | Before: Page renders 'Project', a New video button, a brand-kit selector and 'Could not load this project's videos: projects not found' — After fix: 'This project does not exist.' (browser verify1) | half-rendered page for a project that does not exist | 404 | out/d17-projectRandom.png | — | — | — | PASS | P3 | — | 2a2ea88 | missing-version.test.tsx (missing project) | no | FAIL P3 |
| DL-01 | Download the video | New user (render → play → download) | Ready video in the Studio: look for a way to download the MP4 | A Download action (dev renders carry mock provenance; downloading them for review is allowed by the player's URL) | Before: No download link or button anywhere in the Studio; only the browser's native video menu (⋮ → Download) and Export, which refuses mock renders by design — After fix: Download link on the final render; the downloaded tiktok_1080x1920_30.mp4 matches the version's final render | no explicit control | GET /v1/renders/{id}/download exists | — | partial | — | — | PASS | P3 | — | 53a84bf | player.test.tsx | no | PARTIAL P3 |
| DL-02 | Caption files (incl. translations) | Iterative (translate → use) | Captions card after a German translation | The user can download en/de caption files (FRONTEND.md says so) | Before: Card lists 'de ass approved', 'en ass, srt, vtt' as plain text; no links; translated captions are never burned in and export refuses mock renders, so the German captions cannot be seen or obtained at all — After fix: Buttons 'Download de ass captions' etc.; captions.de.ass downloaded (browser verify1) | text only | caption download endpoint exists, never called | — | no | — | — | PASS | P2 | export-panels.tsx Captions card renders formats as text | 53a84bf | export-panels.test.tsx | no | FAIL P2 |
| PKG-D7 | Packaging editor | Publish | Packaging: change Title (no Save), click another thumbnail | Title edit kept | Before: Title reverts to the saved value (edit lost) — After fix: Title edit kept after choosing another thumbnail (browser verify2) | edit lost | thumbnail saved alone | out/d7-d8-packaging.png | no | unsaved title discarded | — | PASS | P2 | export-panels.tsx thumbnail click saves only the thumbnail; the editor is keyed on updated_at and remounts | 53a84bf editor not remounted; thumbnail saves with the draft | export-panels.test.tsx (D7) | no | FAIL P2 |
| PKG-D8 | Packaging approve | Publish | Packaging: change Title, click Approve without Save; reload | Approved packaging carries the edited title (or Approve asks to save first) | Before: Approved with the old title; edit gone after reload — After fix: Button reads 'Save and approve'; after reload the approved packaging carries the edited title (browser verify2) | looks approved with the edit | approves the stored copy | — | — | — | edit lost | PASS | P2 | Approve ignores the dirty form | 53a84bf | export-panels.test.tsx (D8) | no | FAIL P2 |
| PKG-MSG | Packaging without an LLM | Publish | Write packaging for TikTok | Template packaging with a plain explanation | Before: Template packaging written, but the card shows 'No usable LLM answer (FixtureMiss: no LLM fixture for scenario 'packaging:tiktok' in /tmp/…/eval/llm_fixtures)' — internal exception name and a server path — After fix: Fallback reason in words ('No AI writer is configured, so a template wrote this packaging…'), exception kept in the detail for developers | internal error text | template fallback works | — | — | — | — | PASS | P3 | — | 6682e4d _FALLBACK_REASONS | tests/e2e/test_packaging_mock.py (assertion added) | no (LLM) | FAIL P3 |
| CR-VOICE | Change a creator's voice or look | Creator lifecycle | Creator Alex: Voice tab (shows 'Version 2 · Approved'), Creator DNA → Start a draft | A newer voice/appearance can become the creator's voice/look used by new videos | Before: Creator v1 pins voice v1 and appearance; the draft editor saves only `dna`; approving a voice/appearance version only moves that asset's current version; there is no control to point a creator draft at another voice/appearance/outfit/world. Voice tab headlines the newest voice version (v2) though the creator uses v1 — After fix: Creator draft 'Identity links' card: Appearance/Voice selects, default outfits/worlds; Maya re-linked to her voice v2, creator v2 approved; a video with Maya speaks at ~195 Hz vs Alex ~130 Hz (f0 of the final MP4) | no control; misleading 'Version 2' label | PATCH creator-version accepts appearance/voice/defaults (never sent) | new videos keep the old voice | no | — | — | PASS | P1 | creator-panels.tsx DnaEditor sends only dna | 9f19a9c IdentityLinks; 909b33c voice transcript | creator-identity.test.tsx; test_studio_api.py; tests/e2e/test_studio_mock.py | no | FAIL P1 |
| CR-CREATE | Create a creator / a world | Creator lifecycle | Creators and Worlds pages | A way to create a new creator / world | Before: List pages only (no New button); POST /v1/creators and POST /v1/worlds exist for any writer — After fix: 'Create creator' (blank or from a creator's DNA) and 'Create world' (copy of a world's DNA); creator Maya and world 'Bedroom 436' created through the UI (browser) | missing | implemented | — | — | — | — | PASS | P2 | — | 9f19a9c new-identity.tsx | creator-identity.test.tsx (NewCreatorForm, NewWorldForm) | no | MISSING P2 |
| PLAN-FAIL | Planning failure | Recovery | Create: language 'Azerbaijani — unsupported', Plan video; wait 5 min | 'Planning failed: no voice engine supports Azerbaijani' and a way to change the request | Before: 'Planning… 5 %' forever. The plan activity raised GraphError (no voice.tts route for az), the workflow failed in 1 s, the job row stays running/5 % with no error — After fix: In 1.2 s the previz page says 'Planning stopped / Planning failed: No available engine can do voice.tts for this video (…)' with 'Plan a video again' → /create; the video's Studio offers the same | endless progress | job stuck running | — | no | — | — | PASS | P1 | ce_exec/planning.run_plan catches only PlanningError/LookupError; GraphError escapes and PlanVideoWorkflow has no failure handler | 96cca37 fail_job activity + workflow handlers; 09ae6b0 previz/studio job-ended states; adb9213 Studio 'Plan a video again' | test_workflows.py::test_a_crashed_plan_activity_still_fails_the_plan_job; test_planning_api.py; missing-version.test.tsx | no | FAIL P1 |
| CAM-MOVES | Camera moves in the render | Plan → render | Shots with push_in, pull_out, pan, tilt, whip or static_hold moves (vocabulary moves the Director/editor may plan) | The move appears in the video (spec §22: 'punch-ins and punch-outs… optional whip and pan') | Before: post.camera (CPU FFmpeg, not a mock) implements only punch_in, punch_out and handheld_drift; the six others are silently dropped (output MP4 identical — output-truth run) — After fix: push_in, pull_out, pan, whip, tilt and static_hold change the frames (ffmpeg); a static hold stops the handheld drift (reachable through the planner/LLM or operations, not from the template planner) | plan shows the move | move in spec | identical MP4 | no | — | — | PASS | P2 | ce_exec/post.py post_camera_node reads only three move types | 9ea980e CameraRamp in ce_render.video.camera_post; post.camera impl_version 4 | ce_render test_render.py::test_planned_camera_moves_change_the_picture[*], ::test_a_static_hold_stops_the_handheld_motion; tests/e2e/test_camera_moves_mock.py | no — CPU code, GPU testing would not fix it | FAIL P2 |
| OUT-ROOM | Room sound vs camera microphone | Iterative | Change the camera profile (its mic) of a built version | The room sound re-renders for the new microphone | Before: audio.room keyed only on the room; the new mic left the cached room tone in the mix. After: the room node keys on the mic profile digest; audio.room and mix.audio rebuild | — | — | — | — | none seen | — | PASS | P2 | ce_build graph audio.room spec lacked the mic | 7eda0fe | test_graph.py::test_the_room_sound_keys_on_the_camera_mic; test_dirty_sets.py (expectation corrected) | no | FAIL P2 |
| OUT-ASPECT | Platform / aspect edit | Iterative | Edit that sets the platform or aspect (set_meta) | The render uses a preset of the new platform/aspect | Before: render.outputs kept the old preset, so the MP4 did not change. After: op_set_meta re-derives render.outputs; an impossible combination is refused (unknown_preset) | — | — | — | — | none seen | — | PASS | P2 | translate.op_set_meta did not touch render.outputs | 387abd1 | ce_director tests/test_meta_edits.py | no | FAIL P2 |
| OUT-PROSODY | Cast voice offsets | Plan → render | A cast member with rate/energy/pitch offsets | The speech changes accordingly | Before: the offsets were dropped by compile_voice. After: _with_voice_prosody applies rate×, energy and pitch_semitones; the mock f0 moves with pitch | — | — | — | — | none seen | — | PASS | P2 | ce_exec/behavior.py ignored the cast prosody | 9f3d4a5 (behavior.compile_voice impl 3; goldens regenerated, only the new key) | tests/e2e/test_voice_prosody_mock.py; test_contracts (wire-only field) | real TTS honouring pitch_semitones must be checked on GPU (PITCH-REAL) | FAIL P2 |
| OUT-LABELS | Mock frames show what was asked | Plan → render | Build a video with creator Maya, outfit 'denim jacket', world 'Alex's home office' | The mock frame names the creator, outfit, world and acting state, so propagation can be checked by eye | Before: keyframes named only creator/shot; plates drew a figure. After (frames of Maya's video at 2 s and 7.5 s): 'MOCK KEYFRAME creator: Maya / wardrobe: denim jacket / world: Alex's home office / shot: sht_1', 'state: confident', plate header 'MOCK WORLD PLATE camera position: cam_desk_front, time of day: late_afternoon' | — | — | out/artifacts/maya_t2.png, maya_t75.png | — | none seen | — | PASS | P3 | mock labels incomplete | ae1b260 (world.plate/image.keyframe/avatar.render impl 2) | tests/e2e/test_mock_labels_mock.py | no | FAIL P3 |
| CR-VOICE-TRANSCRIPT | Designed voice in a video | Creator lifecycle | New creator Maya with a designed voice → plan a video | Plans and renders with Maya's voice | Before: every plan failed 'VoiceDNA references.0.transcript String should have at least 1 character' (the selected candidate stored an empty transcript) and the previz page said 'This version does not exist'. After: the reference carries the studio test sentence; approval validates the voice DNA; Maya's 12 s video planned in 6 s and built in 38 s | — | — | 01a116d2…mp4 8.17 s 1080x1920 | — | none seen | — | PASS | P1 | studio.select_voice_candidate stored sample_text or '' | 909b33c; 09ae6b0 | tests/e2e/test_studio_mock.py (transcript); test_studio_api.py::test_a_voice_planning_cannot_read_is_refused_at_approval | no | FAIL P1 |
| CR-JOURNEY | New creator end to end | Creator lifecycle | Create creator → appearance (face candidates, canonical, approve) → voice (design, select, approve) → outfit (references, approve) → link → attest → approve → plan → build | A usable creator whose identity reaches the video | Works through the UI only (after CR-CREATE, CR-VOICE, CR-VOICE-TRANSCRIPT); frames name Maya, her outfit and world; voice f0 195 Hz (Alex 130 Hz) | all steps by role and label | — | out/artifacts/01a116d2_v1_final.mp4 | — | none seen | creator v2 current, voice v2 approved | PASS | — | — | — | — | no | — |
| MOCK-FACE | Identity consistency in frames | Plan → render | Compare the face across shots of Maya's video | Same face | The mock face colour comes from a per-prompt seed (tan in sht_1, pink in sht_2); identity consistency cannot be judged on mock frames | — | — | — | — | none seen | — | BLOCKED — MOCK LIMITATION | — | — | — | — | yes: face identity across shots needs the real image/avatar models | — |
| D2 | Regenerate plan on derived versions | Iterative | Open the previz of an edit-derived version | Only actions the API accepts | No 'Regenerate plan'; note 'Derived versions are changed with edits in the Studio…' (browser verify5) | — | — | — | — | none seen | — | PASS | P3 | previz page offered replan for any version | 3dea453 | plan.test.tsx (D2) | no | FAIL P3 |
| D3 | Auto-applied lock/take/translate proposals | Iterative | Add a Voice lock → Save locks; watch the Edit card | No stale Apply on an auto-applied change | Not reproduced: v7 'lock_change' built ready, no Apply/Reject left on the card (browser verify3) (timing-dependent in the code review; not seen live) | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| D4 | Panels while generating | Plan → render | While the version is generating, look at Critique, Write packaging, consistency | Disabled with a reason | All three disabled, title 'Available when the version is rendered.'; Render for <platform> hidden (browser verify5) | — | — | — | — | none seen | — | PASS | P3 | panels shown for generating versions | 9559c3a | export-panels.test.tsx (D4) | no | FAIL P3 |
| D5 | Resume offered for never-generated versions | Recovery | Versions panel with a failed version that never reached approval | Previz link instead of Resume | Component test passes; live, a failed plan creates no version (see PLAN-FAIL), so the case needs a previz failure (verified by component test) | — | — | — | — | none seen | — | PASS | P3 | Resume shown for any failed version | f1980dc | studio-panels.test.tsx (D5) | no | FAIL P3 |
| D6-D11 | Create wizard retries and stale choices | New user | Plan fails 422 then retry; change creator after choosing a voice | One project; voice and sources reset | Component tests pass (one 'My videos' project; voice and sources reset on creator/project change) | — | — | — | — | none seen | — | PASS | P3 | project id not kept; dependent fields not reset | 4735824 | create/page.test.tsx | no | FAIL P3 |
| D9 | Approve while a world approval is pending | Plan → render | Previz whose plan uses a proposed world version | Approve disabled with a link to the World Studio | Component test passes; not reproduced live (needs a pending world proposal) | — | — | — | — | none seen | — | PASS | P3 | previz ignored the flag | 3dea453 | plan.test.tsx (D9) | no | FAIL P3 |
| D12 | Source upload input | Research | Add a document source twice | The file input clears after a successful add | Component test passes | — | — | — | — | none seen | — | PASS | P4 | uncontrolled input kept the file | 35e1fa0 | research-panels.test.tsx | no | FAIL P4 |
| D13-D15 | Double submits on templates and brand kits | Templates/brand | Double-click Propose edit / Save template / Create brand kit | One request each; the form resets | Component tests pass | — | — | — | — | none seen | — | PASS | P3 | no pending guard / reset | 7bda4b5 | templates/page.test.tsx | no | FAIL P3 |
| D14 | Voice candidate Select | Creator lifecycle | Double-click Select on a voice candidate | One draft voice version; the candidate shows '· selected' | Before: one draft per click; marker stale. After (browser verify4): versions 2 → 3 on a double click, another click 3 → 3; selected = [true,false,false]; the cell shows '· selected' | — | — | — | — | none seen | — | PASS | P3 | API created a version per call; UI did not refetch candidates | d009e8b (API idempotent for the open draft; button disabled while pending) | tests/e2e/test_studio_mock.py (second select) | no | FAIL P3 |
| D20 | Claim override vs Approve | Plan → render | Override an unsupported claim in the ledger, then look at Approve | The finding shows as overridden; Approve needs no extra tick | Before: still listed with an unticked override box. After: 'overridden in the claim ledger' badge, no checkbox, Approve enabled (component test; live needs an open-book claim from an LLM plan) | — | — | — | — | none seen | — | PASS | P3 | ApprovePanel ignored the ledger | 457fc8e | plan.test.tsx (D20) | no | FAIL P3 |
| D21 | Stale error in the Versions panel | Iterative | Resume refused, then Restore succeeds | The old error goes away | Before: the Resume error stayed. After: starting any panel action clears the others' errors | — | — | — | — | none seen | — | PASS | P4 | errors of four mutations OR-ed | 81611e9 | studio-panels.test.tsx (D21) | no | FAIL P4 |
| D10 | Project page after the Dashboard | Navigation | Dashboard → Projects → project within 5 s | All videos listed | Not reproduced live (list complete) (shared cache key with limit 5/50 from the code review; not seen) | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| JOB-CANCEL | Cancel a running generation | Recovery | Apply an edit, click Cancel on the running Generate job, confirm | Job stops, no GPU work left queued, version marked cancelled | Confirm 'Cancel this job? Work already done is kept.'; cancelled in 5.2 s; nodes cancelled 47, cached 10, succeeded 1; version 'Cancelled' with Resume (browser verify_cancel) | — | — | — | — | none seen | — | PASS | P2 | no cancel control existed | 41488e3 | cancel-job.test.tsx | no | MISSING P2 |
| JOB-RESUME | Resume a cancelled version | Recovery | Versions → Resume on the cancelled v5 | Finishes from the cache | Ready in 7.4 s, 58 nodes cached (browser verify_resume) | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| TAKE-SEL | Select another take | Iterative | Shots and takes → Select tk_1_2 on sht_1 | New version uses take 2 for sht_1, others unchanged; 'selected' marker on tk_1_2 | v6 'take_select' ready; API takes sht_1 tk_1_2 selected; frame at 1.5 s reads 'shot sht_1 take 2' (v5: 'take 1'); later lock version keeps it (mock takes differ only in their label (MOCK-TAKES)) | — | — | out/artifacts/top_v5…png, top_v6…png | — | none seen | — | PASS | — | — | — | — | no | — |
| MOCK-TAKES | Take variety | Iterative | Compare takes of a shot | Different performances | Mock takes are identical apart from 'take N' in the label (pixel diff 0 at 90×160) | — | — | — | — | none seen | — | BLOCKED — MOCK LIMITATION | — | — | — | — | yes: take variety and ranking need the real avatar model | — |
| EXTRA-DUP | Duplicate video | Iterative | Versions → Duplicate video | A new video in the same project from this version | New video '… (copy)' v1 origin duplicate, ready in 8 s from the cache (browser verify3) | — | — | — | — | none seen | — | PASS | P3 | unreachable in the UI | bf8e85f | reachability.test.tsx | no | MISSING P3 |
| EXTRA-RENDER | Render for another platform | Publish | Export → Platform TikTok on a YouTube Shorts video → Render for TikTok | An extra final render in the TikTok preset | tiktok_1080x1920_30 ready; offered in Export's render list (browser verify3) | — | — | — | — | none seen | — | PASS | P3 | unreachable in the UI | bf8e85f | reachability.test.tsx | no | MISSING P3 |
| EXTRA-CONSIST | Creator consistency check | QC | Creator consistency → Run consistency check | A report | 'Job Succeeded · Warn creator history · Worlds: Out of band · … Face identity: In band (mock)' (browser verify3) | — | — | — | — | none seen | — | PASS | P3 | unreachable in the UI | bf8e85f | reachability.test.tsx | no | MISSING P3 |
| WORLD-NEW | Create a world | World lifecycle | Worlds → Name 'Bedroom 436', Start from 'Modern office' → Create world | A draft world opens in the World Studio | Opens /worlds/<id>: 'Bedroom 436 · World DNA — version 1 (Draft)' (browser verify3) | — | — | — | — | none seen | — | PASS | P2 | — | 9f19a9c | creator-identity.test.tsx (NewWorldForm) | no | MISSING P2 |
| BREAK-XSS | Markup in user text | Hostile input | Idea: '<img src=x onerror=alert(1)> <script>alert(2)</script> Tip: 🌙 sleep "8h" & don't skip it — naïve café' | Shown as text everywhere; nothing runs | No dialog fired, no injected <img>, the script shown as text on previz and as the Studio title; emoji and accents kept | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-DOUBLE-APPROVE | Approve twice | Impatient user | Double-click 'Approve and generate', then Approve in a second stale tab | One generation; the stale tab is told why | 1 generate job; stale tab: 'approval needs a previz_ready version; this one is generating' (the stale-tab message is technical wording (remaining P4)) | — | — | — | — | none seen | — | PASS | P4 | — | — | — | no | — |
| BREAK-REFRESH | Reload during generation | Impatient user | Reload the Studio while generating | The page picks the run back up | Ready 20 s after the reload without any action | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-EDIT-WHILE-GEN | Edit while generating | Impatient user | Propose and Apply 'make him more skeptical' while v1 generates | Either refused with a reason or a derived version | Proposal planned; Apply created v2 (edit), both v1 and v2 reached ready | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-TRANSLATE-WHILE-GEN | Translate captions while generating | Impatient user | Captions → German → Translate while v1 generates | Derived version or a reason | v2 derived and ready with de captions; v1 ready; info note in the card | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-EMPTY-IDEA | Empty idea | Hostile input | Next through every step with no idea | Plan refused with a reason | 'Write an idea or a script in step 1 first.'; Plan video disabled | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-DURATION | Invalid duration | Hostile input | Target duration -5, 0, 100000 | Refused before sending | Before: sent; API answered 'the request does not match its schema /body/target_duration_s…'. After: Plan video disabled, 'Set a target duration between 1 and 600 seconds in step 7.' (browser verify4) | — | — | — | — | none seen | — | PASS | P3 | no client check | fbb883b | create-form.test.ts | no | FAIL P3 |
| BREAK-LONG | Very long instruction | Hostile input | Type 9,600 characters in the edit instruction | Held to the limit | Before: accepted; API 'String should have at most 2000 characters'. After: the field stops at 2,000 and shows '2000 / 2000 characters' (browser verify4) | — | — | — | — | none seen | — | PASS | P3 | no maxLength | be2b1e4 | edit-panel.test.tsx (BREAK-LONG) | no | FAIL P3 |
| BREAK-OFFLINE | Network drop | Flaky network | Go offline, Propose; back online | Say it is offline; send when back | Before: 'Sending…' with no reason (sent once online). After: 'You are offline. What you send now waits and goes out when the connection is back.' while offline, gone online (browser verify4) | — | — | — | — | none seen | — | PASS | P3 | no offline state | 1e226df | offline-notice.test.tsx | no | FAIL P3 |
| BREAK-TWO-TABS | Two tabs on one video | Concurrent users | Tab A and tab B propose on v3; A applies; then B | B keeps its proposal; Apply creates a version from v3 | Before: B followed the new current version and its card vanished. After: B's URL pinned to v3, card kept, Apply worked: v5 and v6 both children of v4… (browser verify4) | — | — | — | — | none seen | — | PASS | P3 | Studio follows current_version_id when no ?version= | e6cd950 | pin-version.test.tsx | no | FAIL P3 |
| BREAK-SESSION | Session expiry | Returning user | Clear cookies, open a Studio | Login with a way back | /login?next=%2Fvideos%2F<id> | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-BACK | Back/forward | Navigation | Studio latest → ?version=v1 → Back → Forward | Each shows its version | Back shows v3, Forward v1 | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-RESTORE | Restore an old version | Iterative | Versions → Restore v1 (now v3) | The old content as a new version | v3 origin restore, parent v1; final MP4 byte-identical to v1 (sha256 808241330ecf9e80…) | — | — | — | — | none seen | v1 untouched | PASS | — | — | — | — | no | — |
| BREAK-INTERRUPT | Workers killed mid-generation | Infrastructure failure | kill -9 the render and CPU workers at 57 %, restart them after 60 s | The job resumes and finishes; the file is whole | Stuck at 0.567 while down; resumed after restart and ready 24 s later; the MP4 decodes (174 frames, 5.8 s) | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| BREAK-PHONE | Phone width | Mobile | 390×844 touch: dashboard, create, studio, creators, jobs, settings | No horizontal scroll; usable | No horizontal overflow on any page; menu button 'Open navigation'. Many controls under 24 px (studio 31, jobs 40) (small touch targets remain (P4)) | — | — | — | — | none seen | — | PASS | P4 | — | — | — | no | — |
| BREAK-A11Y | Accessibility basics | Keyboard / screen reader | Create, Studio, Creators: labels, names, alt, h1, ids; Tab order | All controls named; skip link; visible focus | 0 unlabeled inputs, 0 unnamed controls, 0 images without alt, one h1, no duplicate ids; first Tab 'Skip to content'; Instruction reached in 32 Tabs; focus visible | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| TENANCY | Organisation isolation | Security | Another org's ids through the API | 404 / no rows | Covered by tests/invariants/test_i12_tenancy*.py and API tests (run in the full regression) (test-level, not black-box) | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| MEM-CRUD | Creator memory authoring | Creator lifecycle | Memory tab: add 'A topic the creator avoids: crypto', then Delete | Add and delete from the UI | Before: only status actions; no add, no delete (API had both). After: added row appears; Delete (confirm) → item forgotten, text erased, row gone after reload (browser verify4) | — | — | — | — | none seen | — | PASS | P2 | UI never called POST/DELETE memory | ba739f0 | creator-memory.test.tsx | no | MISSING P2 |
| SCENE-ORDER | Reorder scenes | Iterative | Scenes → 'Move scn_2 up' → Apply | scn_2 first in spec, script and video | Before: no control (API had move_scene/remove_scene). After: v4 order scn_2, scn_1, …; segments seg_2, seg_1…; the MP4 opens on sht_3 (scn_2's first shot) where v3 opened on scn_1 | — | — | out/artifacts/order_v3.png, order_v4.png | — | none seen | — | PASS | P2 | unreachable | a7e05f2 | edit-panel.test.tsx (SceneOrderButtons) | no | MISSING P2 |
| TPL-UUID | Pick a template's target | Templates | Template → Project + Video selects → Preview | No UUID typing | Before: only a version-id field. After: the picker fills the id; Preview enables Propose edit (browser verify4) | — | — | — | — | none seen | — | PASS | P3 | no picker | bed6a26 | templates/page.test.tsx (version picker) | no | FAIL P3 |
| EXPORT-MOCK | Export a mock render | Publish | Export → platform, render, all checklist boxes | Refused with reasons | Export disabled: 'This render carries mock provenance (MOCK PROVENANCE — NOT FOR DISTRIBUTION) and cannot be exported.' and 'Approve the packaging for this platform first.' | — | — | — | — | none seen | — | INTENTIONAL LIMITATION | — | — | — | — | yes: real provenance (C2PA, watermark) needs real renders | — |
| RATINGS | Human ratings | QC | Open Ratings | Rating prompts per version | Prompts with 1–5 buttons render | — | — | — | — | none seen | — | PASS | — | — | — | — | no | — |
| MOCK-TRANS | Translated captions content | Iterative | Download captions.de.ass | German text | '[de] <English line>' (mock translator) | — | — | — | — | none seen | — | BLOCKED — MOCK LIMITATION | — | — | — | — | LLM translation | — |
| NL-WARMER | NL edit outside the template rules | Iterative | Propose 'make him warmer' | An edit | 'Without an LLM only common edits are understood; rephrase or use operations.' (failed, no ops) | — | — | — | — | none seen | — | BLOCKED — REQUIRES REAL MODEL/GPU | — | — | — | — | hosted LLM | — |
| LLM-MEMORY | Authored memory in plans | Creator lifecycle | Author 'Maya runs every morning', replan | The fact can shape the script | The template planner speaks the idea verbatim and uses no memory item | — | — | — | — | none seen | — | BLOCKED — REQUIRES REAL MODEL/GPU | — | — | — | — | hosted LLM | — |
| DUR-TEMPLATE | Requested duration vs template script | New user | Plan a one-sentence idea with 10–12 s requested | Disclosed | Video 4.9–8.2 s; previz warns 'Requested 12 s; the script needs about 7 s (the template script is the input's own sentences).' | — | — | — | — | none seen | — | INTENTIONAL LIMITATION | P4 | — | — | — | hosted LLM writes to length | — |
| PLAN-MSG | Planning failure wording | Recovery | Read the failed-plan message | A short reason and what to change | Correct but long: lists every voice engine and its pool/asset reasons (remaining: summarise ('no voice speaks Azerbaijani'), details for developers) | — | — | — | — | none seen | — | PARTIAL | P4 | — | — | — | no | — |
| TEAM | Team management | Team | Invite or change a member's role from the UI | Possible for owners/admins | No members, roles or invitations UI and no invitation-acceptance page; memberships come from seed/API | — | — | — | — | none seen | — | PRODUCT DECISION REQUIRED | P3 | — | — | — | no | — |
| ASSETS | Assets library | Navigation | Sidebar 'Assets' | — | Disabled, labelled 'later' | — | — | — | — | none seen | — | INTENTIONAL LIMITATION | P4 | — | — | — | no | — |
| EFFECTS | Planned effects in the render | Plan → render | Effects other than the AI disclosure in a spec | Rendered or refused | Dropped by the renderer (only the disclosure is drawn) | — | — | — | — | none seen | — | PRODUCT DECISION REQUIRED | P3 | — | — | — | no — CPU render code | — |
| PITCH-REAL | Pitch offset on real voices | Plan → render | pitch_semitones with a real TTS engine | Pitch shifts | The real voice translators ignore pitch_semitones (only the mock applies it) | — | — | — | — | none seen | — | BLOCKED — REQUIRES REAL MODEL/GPU | P3 | — | — | — | yes | — |
| REALISM-ABR | Deterministic post.realism | Build | Rebuild post.realism with the same inputs | Identical bytes (content-addressed cache) | Average-bitrate encode is not byte-deterministic across runs (remaining: constant-QP or a fixed-seed encode) | — | — | — | — | none seen | — | PARTIAL | P3 | — | — | — | no | — |
| TPL-PLANNER | Template edit planner routing | Iterative | Instructions near the template rules ('warmer', 'more energetic hook') | Routed or refused | Refused honestly; some phrasings route to a different rule | — | — | — | — | none seen | — | BLOCKED — REQUIRES REAL MODEL/GPU | P3 | — | — | — | hosted LLM | — |
| EXPORT-REDL | Download a past export | Publish | Export history row → Download | Fresh links for that export | Before: links only right after Export. After: 'Download' fetches GET /v1/exports/{id} and lists the files (component test; dev exports are refused for mock renders, so none exist live) | — | — | — | — | none seen | — | PASS | P3 | history table had no links | 3ead74c | export-panels.test.tsx (EXPORT-REDL) | no | MISSING P3 |
| LEXICON-REMOVE | Remove a lexicon term | Creator lifecycle | Draft voice version → Remove next to a term | Term removed | Before: add only. After: PATCH with the remaining terms (component test) | — | — | — | — | none seen | — | PASS | P4 | no control | 6e5b612 | creator-voice.test.tsx | no | MISSING P4 |
| E2E-RESPONSIVE | Dashboard at 768 px | Mobile/tablet | Playwright responsive spec, 4 viewports | No horizontal overflow | Before: '/ overflows horizontally at 768px' (12 px): the Running jobs row with the new Cancel button (introduced by JOB-CANCEL). After: the row wraps; 4/4 viewports pass | — | — | — | — | none seen | — | PASS | P3 | flex row without wrap | de66643 | e2e/responsive.spec.ts (existing) | no | FAIL P3 |
| E2E-PLAY | Playback in the browser | New user | Playwright create-to-play | Plays the final MP4 | Every step passes up to play(): plan, previz, approve, progress, ready → player ≤ 15 s, final render, 206 range with an ftyp box; then 'this browser cannot decode H.264' | — | — | — | — | none seen | — | BLOCKED — ENVIRONMENT | — | — | — | — | no (needs Google Chrome: CE_E2E_CHROME) | — |
| DISCLOSURE-SPAN | Disclosure effect span | Plan → render | A disclosure effect scoped to one scene | Shown during that scene | Burned over the whole video (span ignored) (remaining; over-disclosure, not under-disclosure) | — | — | — | — | none seen | — | FAIL | P3 | executors render.final reads only effects[type=disclosure].params.text | — | — | no — CPU render code | — |
| CAP-PLACEMENT | Caption placement 'bottom' vs safe zone | Iterative | set_captions placement bottom / platform_safe_zone | Different positions | Identical ASS (remaining) | — | — | — | — | none seen | — | FAIL | P4 | ass_renderer maps both to the same margin | — | — | no | — |
| CAP-LANGUAGE | Caption language without translation | Iterative | set_captions language es (no translation) | Spanish captions or a refusal | English captions labelled 'es' (language ≠ translation; the Translate flow is the supported path) | — | — | — | — | none seen | — | PRODUCT DECISION REQUIRED | P4 | — | — | — | no | — |
| MOCK-REGEN | Regenerate the acting | Iterative | Regenerate → Avatar video on a shot | A different performance | Mock avatar frames have no seed: the picture is identical | — | — | — | — | none seen | — | BLOCKED — MOCK LIMITATION | — | — | — | — | yes | — |
| MOCK-INTENSITY | Emotion intensity | Iterative | 'less skeptical' (intensity −0.2) | Visible change | Only the behavior track changes; the mock label shows the emotion name, not the intensity | — | — | — | — | none seen | — | BLOCKED — MOCK LIMITATION | — | — | — | — | yes | — |
| REACTION-RANGE | Reaction source range/layout | Plan → render | shot.reaction_source.range_s / layout | Used | Ignored by screen.prepare ('roadmap') | — | — | — | — | none seen | — | MISSING | P3 | — | — | — | no | — |
| QC-DURATION | Duration QC | QC | qc.render on a video far from its target | A duration finding | qc.render hashes target_duration_s but never checks it (previz discloses the deviation) | — | — | — | — | none seen | — | PARTIAL | P4 | — | — | — | no | — |
| SCENE-REGEN | Regenerate one scene | Iterative | Scenes row → regenerate | Scene-level regenerate | POST /v1/versions/{id}/scenes/{key}:regenerate never called; shot-level Regenerate exists (remaining (needs a component picker per scene)) | — | — | — | — | none seen | — | MISSING | P3 | — | — | — | no | — |
| MEM-HISTORY | Memory item history | Creator lifecycle | A memory item's history | Visible | GET /v1/memory-items/{id}/history never called | — | — | — | — | none seen | — | MISSING | P4 | — | — | — | no | — |
| PROJECT-ARCHIVE | Rename/archive a project | Projects | Project header | Rename and archive | DELETE /v1/projects/{id} (archive) never called; no rename API | — | — | — | — | none seen | — | MISSING | P3 | — | — | — | no | — |
| WARDROBE-REV | Revise an outfit | Creator lifecycle | Approved outfit → new draft, edit description | Possible | No 'New draft version' or description edit for outfits | — | — | — | — | none seen | — | MISSING | P3 | — | — | — | no | — |
| APPEARANCE-EDIT | Edit appearance DNA | Creator lifecycle | Draft appearance → edit age/hair; add a second appearance | Possible | Set once at creation; PATCH /v1/appearance-versions/{id} unused | — | — | — | — | none seen | — | MISSING | P3 | — | — | — | no | — |
| API-KEYS | API keys | Developer | Settings → API keys | Create, list, revoke | /v1/api-keys never called from the UI | — | — | — | — | none seen | — | MISSING | P3 | — | — | — | no | — |
| TAKE-OBS | Take observation scores | Iterative | Takes gallery details | QC and observation scores per take | GET /v1/takes/{id}/observations never called | — | — | — | — | none seen | — | MISSING | P4 | — | — | — | no | — |
| PROV-VERIFY | Verify provenance | Publish | Render → verify | C2PA verification result | GET /v1/renders/{id}/verify never called (dev renders are mock provenance) | — | — | — | — | none seen | — | MISSING | P4 | — | — | — | no | — |
| VARIANTS | Variants and remix | Iterative | — | — | API answers 501 (V1 scope) | — | — | — | — | none seen | — | INTENTIONAL LIMITATION | P4 | — | — | — | no | — |
| CONSENT | Digital-twin consent | Creator lifecycle | Consent tab | — | Read-only; writes gated by digital_twins_enabled=false (V1) | — | — | — | — | none seen | — | INTENTIONAL LIMITATION | P4 | — | — | — | no | — |
| INTENT-VIDEO | Video-level intent edit | Iterative | set_intent without a scene | A replan or a note | No node reads /intent/video: the edit changes nothing in the render | — | — | — | — | none seen | — | PRODUCT DECISION REQUIRED | P4 | — | — | — | no | — |

Totals: PASS 72, MISSING 9, INTENTIONAL LIMITATION 6, BLOCKED — REQUIRES REAL MODEL/GPU 6, BLOCKED — MOCK LIMITATION 5, PRODUCT DECISION REQUIRED 4, PARTIAL 3, BLOCKED — ENVIRONMENT 2, FAIL 2 — 109 scenarios.
