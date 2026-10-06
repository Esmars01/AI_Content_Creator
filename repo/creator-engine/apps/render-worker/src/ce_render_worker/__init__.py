"""Render worker (§9): Temporal activity worker on queue `render` (FFmpeg, `cpu_inproc` plugins:
captions, provenance, effects). It runs the render nodes of `ce_exec` through the shared
`run_local_node` activity.

Status: implemented and tested in Phase 2 (real FFmpeg assembly with `PROVENANCE_MODE=mock_dev`).
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2
