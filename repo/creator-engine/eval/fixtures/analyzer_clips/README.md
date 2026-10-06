# Analyzer fixture clips (owner-supplied)

Short clips with labelled behavior events for testing and calibrating the real CPU analyzers
(MediaPipe face and body) — §16.2, §16.7, §40 Phase 7.

**Licensing (§16.7, §41):** every clip must be a recording made with the subject's documented consent
for this use, or a clip under a license that permits it (for example CC0 or CC BY with attribution
recorded below). Nothing is committed here until the product owner supplies such clips; until then
the analyzer fixture tests and the visual calibration skip with that reason.

Layout: put the clips next to a `manifest.yaml`:

```yaml
version: 1
set: owner-2026-10            # a name recorded with every calibration row
clips:
  - file: alex_desk_01.mp4
    license: "consent:7f3c…"  # consent id, or an SPDX id plus attribution
    events:                   # labelled ground truth (seconds)
      - {proxy: look_away, start_s: 2.1, end_s: 2.9}
      - {proxy: blink_event, start_s: 4.02, end_s: 4.35}
      - {proxy: smile_score, start_s: 6.0, end_s: 7.4}
      - {proxy: lean_in, start_s: 9.0, end_s: 11.0}
      - {proxy: nod, start_s: 12.2, end_s: 13.0}
```

Then run `make calibrate-analyzers` (writes the measured reliability per analyzer revision) and
`FIXTURE_CLIPS_DIR=eval/fixtures/analyzer_clips uv run pytest plugins/analysis` (fixture tests).
