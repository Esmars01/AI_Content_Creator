# plugins/asr

ASR and alignment adapters.

Implemented (Phase 7): `faster_whisper_cpu/` — `asr.transcribe`, coarse `asr.align` (global word alignment with fuzzy matching against the normalized script) and `asr.lid` with faster-whisper base int8 on CPU (MIT; `cpu_model`, served by `worker-cpu`).

Planned (MASTER_BUILD_PROMPT §8, §39.7): `qwen3_asr` and the forced aligners, `ctc_aligner` for tr/ar (Phase 8).
