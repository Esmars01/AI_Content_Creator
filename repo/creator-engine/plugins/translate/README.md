# plugins/translate

Caption translation.

- `captions_llm` (Phase 12) — `captions.translate` with the deployment's configured LLM provider
  (`LLM_PROVIDER`); `validation: untested_on_gpu` until an operator's smoke run against a real
  provider (no paid calls were made while building it).

Every plugin ships a `plugin.yaml` validated by `ce_contracts` (§24).
