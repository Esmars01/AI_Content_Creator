"""Domain models: VideoSpec, SpecPath, anchors, tokenizer, IDs, enums, errors, spec utils, vocab loader.

Status: Phase 1 — IDs, keys, canonical JSON, tokenizer, anchors, SpecPath, closed vocabularies,
VideoSpec with context validation, Creator/Appearance/Voice/Wardrobe/World DNA, memory items and
snapshots, CBS, CompiledBehavior, ObservedBehavior, coverage and plan reports, BuildManifest.
The execution graph lives in ce_build (Phase 2). Phase 6 adds `ce_core.edit`: the closed
EditOperation union (§28), SpecPatch, lock checks (I8), anchor rebase, the structured spec diff and
operation → patch translation. QC reports arrive in Phase 11.
"""

__version__ = "0.0.0"
IMPLEMENTED_IN_PHASE = 1
