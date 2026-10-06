# plugins/vision

Vision adapters.

Implemented (Phase 7, `cpu_inproc`):

- `ppocr/` — `vision.ocr` with PP-OCRv4 mobile through RapidOCR (Apache-2.0; the v6 swap is Phase 8, DECISIONS D78); used by screen analysis (ADR 0049)
- `auraface/` — `face.embed` with AuraFace glintr100 (Apache-2.0), aligned with MediaPipe landmarks (InsightFace-derived files are not used)

Planned: `vllm_vlm` (Phase 8; the VLM is the mock until then, §39.2).
