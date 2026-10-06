# plugins/voice

TTS and voice-design adapters.

Implemented (Phase 7): `kokoro_cpu/` — Kokoro-82M ONNX int8 on CPU (`voice.tts`, preset `voice.clone_prepare`, translator `kokoro_v1`); **dev/test only** because its phonemizer stack is GPL-3.0 (ADR 0043).

Planned (MASTER_BUILD_PROMPT §8, §39.7): `chatterbox`, `qwen3_tts`, `voxcpm2` (Phase 8).
