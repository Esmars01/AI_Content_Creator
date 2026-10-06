"""Layered config loader, YAML schemas, validation, config digests.

Status: implemented and tested in Phase 1 (default.yaml → env overlay → environment →
validation of every config file, per-file digests, `ce config validate`). Phase 2 adds the
scheduler, build and timeline sections and the GPU-pool `autoscale` flag.
"""

__version__ = "0.2.0"
IMPLEMENTED_IN_PHASE = 1
