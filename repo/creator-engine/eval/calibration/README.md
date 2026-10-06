# Recorded proxy calibrations

Rows written by `scripts/calibrate_analyzers.py --json` (ADR 0045), kept as the record of a
calibration run. Store them into a database without measuring again:

    uv run python scripts/calibrate_analyzers.py --load eval/calibration/speech-v1.json

| File | Measured | Method | Result |
| --- | --- | --- | --- |
| `speech-v1.json` | 2026-10-04, Phase 7, `prosody_features` revision 1 | 6 sentences × 6 relative rates spoken by Kokoro (CPU), word times from faster-whisper base, rate measured by `prosody_features` against the same sentence at 1.0 (30 trials) | `speech_rate_slow` F1 0.91, `speech_rate_fast` F1 0.96 (n = 30) → `high` |

Visual proxies are calibrated only on owner-supplied clips (`eval/fixtures/analyzer_clips`);
none are recorded yet.
