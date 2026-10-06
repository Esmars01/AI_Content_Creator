# text_embed — `embed.text` on CPU (Phase 12)

The adapter code for the real text-embedding engine of §6 ("a CPU-capable multilingual
text-embedding model with an Apache-2.0 or MIT license; candidates BGE-M3 and multilingual-E5
[RV: license at the pinned revision]"): an ONNX encoder run with onnxruntime, the model's own
`tokenizer.json` (Hugging Face `tokenizers`), CLS or mean pooling, L2-normalized vectors, an
optional per-model text prefix, and a dimension check against `EMBEDDING_DIM`.

## Status: not registered

There is **no `plugin.yaml` and no entry point** yet, so the router never sees it. Missing:

1. a model chosen and **pinned at a revision**, with the ONNX export and tokenizer files listed in
   `scripts/cpu_assets.yaml`;
2. its **license verified at that revision** (`verified_at`, `verified_by`, `text_sha256`) — no
   license is assumed here;
3. a CPU smoke run (the contract suite on the fetched assets) to claim `smoke_passed`.

None of this could be done in the Phase 12 build environment: model hosts were unreachable, so
neither the files nor the license text could be fetched. Until then `embed.text` routes only to
`mock_embed` in dev and test; in production, with no embedding adapter, memory retrieval, the
repetition guard and research retrieval fall back to their keyword paths (ADR 0057) and say so in
their snapshot parameters.

The tests run the adapter's tokenization, pooling, normalization and dimension checks on a
stand-in backend (`testing.py`).
