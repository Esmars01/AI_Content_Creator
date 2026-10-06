"""Building blocks for real model adapters (Phase 8, ADR 0051).

- `EngineAdapter`: the adapter base for real engines. The model's heavy calls live in a *backend*
  object the adapter builds in `load()` (importing torch and upstream code only there); a CPU
  stand-in can be swapped in with `use_backend()` — the contract suite does that for GPU adapters
  when no GPU is present (manifest `test_backend`, §37 "heavy calls mocked").
- `media`: FFmpeg/NumPy helpers every engine needs to turn tensors into artifacts.
- `translate`: helpers for `BehaviorTranslator`s (coverage bookkeeping, label parsing, prompts
  built from the vocabulary descriptions the directives carry).
- `gpu`: lazy torch helpers (device, seeding, peak VRAM).

The package stays importable on Python 3.10 and never imports torch at module import time.

Status: implemented and tested in Phase 8 on CPU stand-ins (the contract suite swaps in each
adapter's `test_backend`); the real backends are `untested_on_gpu` (docs/GPU_VALIDATION.md).
"""

from ce_plugin_kit.engine import EngineAdapter, ModelPaths, work_units
from ce_plugin_kit.translate import Translation, locate, parse_label, prompt_from_descriptions, spans

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 8

__all__ = [
    "EngineAdapter",
    "ModelPaths",
    "Translation",
    "locate",
    "parse_label",
    "prompt_from_descriptions",
    "spans",
    "work_units",
]
