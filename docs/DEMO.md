# Demo (mock mode)

A reproducible walk through the product, entirely on CPU, with mock engines and the fixture LLM. Two ways
to run it: the scripted demo through the HTTP API (`make demo`), and the same path by hand in the web app.

## 1. Start the stack

```bash
make bootstrap          # once: prerequisites, .env, Python and JS workspaces
make dev                # Compose: infrastructure + services (needs the service images)
# or, where the images cannot be built:
make infra-up && make dev-native
```

`make dev-native` builds the web app once (`pnpm --filter @ce/web build`) and starts the services on the host
(`docs/DEPLOYMENT.md`). Optional: `make obs-up` (or `make obs-native`) for Grafana at <http://localhost:3001>.

## 2. The scripted demo

```bash
make demo               # = uv run python scripts/demo.py [--api http://localhost:8000]
```

It signs in as a throwaway editor of the dev organization and drives the public API the way the web app does,
checking every step and stopping at the first failure. A run on 2026-10-06 (native mode, 4 vCPUs, no GPU):

| At | Step | What happened |
| --- | --- | --- |
| 0.0 s | health | `/readyz`: database ok, Valkey ok |
| 1.6 s | sign in | a fresh editor of the dev organization |
| 2.7 s | research source | a pasted note ingested as a `user_provided` source with its facts |
| 3.8 s | plan | "Create a 30-second TikTok explaining why most people misunderstand AI agents" → the Director (fixture LLM) |
| 7.9 s | previz | measured timings (TTS + alignment), estimated 35.7 s, no blocking findings |
| 101.8 s | build | approved → built → `ready` |
| 102.1 s | final render | the MP4 downloaded to `.data/demo/final.mp4` (≈ 10 MB), `mock_dev` provenance |
| 102.2 s | coverage | 67 requested behavior items; 12 observed as delivered (`*_CONFIRMED`) — the rest are approximated, unsupported or not measurable with mock analyzers, and the report says which |
| 102.2 s | claim ledger | no checkable claims in this script |
| 103.3 s | edit proposed | "make him more skeptical" on the last scene → `set_acting` + `add_behavior_event`, one node to regenerate |
| 149.5 s | edit applied | version 2 built from cache except what changed |
| 149.5 s | template saved | a caption template captured from version 2 |
| 159.0 s | captions translated | German captions (a new version), the translation approved |
| 161.0 s | packaging | TikTok packaging; the fixture LLM has no packaging answer, so the template packaging is used and labelled |
| 161.1 s | export guard | the export is **refused** (409): the render carries mock provenance and cannot be exported — by design |

Total: 161 s. The report is written to `.data/demo/report.json`. Timings depend on the host; the number of
confirmed items varies between runs (each video gets a fresh seed, and the mock observers are seeded with it).

## 3. The same path in the web app

1. <http://localhost:3000> — sign in (`make dev` prints the dev admin; or create an editor with
   `uv run python scripts/e2e_user.py`).
2. **Create** → describe the video → optionally pick sources (Research) → **Plan**.
3. Review the previz: script, storyboard, intent, the performance timeline, predicted coverage, memory,
   claims and findings. **Approve**.
4. Watch the job progress; play the MP4 in the Video Studio. Coverage badges say "delivered" only for
   behaviors the analyzers confirmed.
5. **Edit**: type "make him more skeptical" with a scene selected; review the proposal (operations, impact,
   predicted coverage, estimate); **Apply**. Compare the versions side by side.
6. **Templates**: save one from the version; **Settings → Brand kits**: create a kit and assign it to the
   project.
7. **Export** panel: translate captions, review them, generate packaging, approve it; the export button
   explains why a `mock_dev` render cannot be exported.

## 4. Watch it

With the observability profile up, Grafana's **Creator Engine — overview** shows the requests, the
**scheduler** dashboard the queue and the CPU worker, **builds** the nodes, cache hits and render times,
**quality** the coverage outcomes (expect a high NOT_MEASURABLE share with mock analyzers — the
`NotMeasurableRate` alert goes pending for exactly this reason), and Tempo the API, scheduler, orchestrator
and render-worker traces.

## What the demo does not show

GPU engines (none was validated), a hosted LLM (the Director runs on authored fixtures), real watermarks
(mock in dev, so no export), and video playback in the open-source Chromium used by the automated browser
tests here (it cannot decode H.264; Google Chrome can).
