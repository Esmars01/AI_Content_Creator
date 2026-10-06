# plugins/analysis

Behavior and identity analyzers (`cpu_inproc`, real behind `CPU_REAL_ENGINES`, ADR 0042).

Implemented (Phase 7):

- `mediapipe/` — `mediapipe_face` (`face.detect`, `face.landmarks`) and `mediapipe_body` (`body.landmarks`), MediaPipe Tasks (Apache-2.0)
- `prosody_features/` — `audio.prosody` with librosa (ISC)
- `image_embed/` — `dinov2_embed`, `image.embed` with DINOv2 ViT-S/14 ONNX (Apache-2.0)

Tests: `*/tests` (synthetic input; the MediaPipe fixture-clip test needs owner clips, `eval/fixtures/analyzer_clips`).

Planned (MASTER_BUILD_PROMPT §8, §39.7): `speaker_embed` (Phase 8, DECISIONS D80), `audio_emotion` (Phase 11, sandbox only).
