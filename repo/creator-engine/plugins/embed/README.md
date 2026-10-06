# plugins/embed

Embedding adapters.

- `speaker_ecapa` — `voice.embed` (speaker similarity).
- `text_embed` — `embed.text` on CPU (Phase 12): adapter code and tests; **not registered** until a
  model is pinned and its license verified at that revision (see `text_embed/README.md`). Until
  then `embed.text` routes only to `mock_embed` (dev/test).

Every registered plugin ships a `plugin.yaml` validated by `ce_contracts` (§24).
