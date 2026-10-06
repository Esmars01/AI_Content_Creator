"""Kokoro-82M on CPU (§21, Phase 7): `voice.tts` and preset `voice.clone_prepare` for dev and test.

Kokoro cannot clone; `voice.clone_prepare` selects one of its preset voices deterministically from
the voice description and language. Its phonemizer stack (phonemizer, espeak-ng) is GPL-3.0, so
the plugin is limited to `allowed_envs: [dev, test]` (ADR 0043)."""
