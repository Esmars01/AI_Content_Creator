"""Build execution shared by the orchestrator and the render worker (ADR 0033): node output
documents, loading a version's references, request builders for model nodes, in-process executors
for CPU and render nodes, and the bookkeeping (cache, BuildManifest, takes, renders, events).

Status: implemented and tested in Phase 2; behavior nodes are real from Phase 3. Phase 6 adds the
edit jobs (`editing`: propose, apply, derived versions with their plan report) and parent builds
(`parents`). QC records measurements without gating until Phase 11.
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2
