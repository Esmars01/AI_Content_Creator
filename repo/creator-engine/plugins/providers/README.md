# plugins/providers

GPU, LLM, storage and KMS provider plugins.

Implemented:

- `gpu/mock` (Phase 2) and `gpu/local` (Phase 7: the informational `cpu_local` pool of the `cpu_model` engines; it never provisions)
- `llm/` (Phase 4: `anthropic`, `openai_compatible`, `fixture`)
- `storage/{s3,local_fs}` (Phase 1 built-ins of `ce_storage`, packaged with a `plugin.yaml`, ADR 0030)

Planned (MASTER_BUILD_PROMPT §8, §39.7): `gpu/{local_docker,runpod_pod,runpod_serverless}` (Phase 9), `kms/local` (Phase 13).
