"""Worker runtime: lease loop, presigned artifact I/O, heartbeats, cancellation, model cache (§25).

Status: implemented and tested in Phase 2 against the scheduler's worker API with the mock
adapters (`worker-cpu`). The model cache is a skeleton: manifest, verification and LRU eviction
work; fetchers for `hf://` and `s3://` weights are registered with the first GPU family (Phase 8).
Importable on Python 3.10 (§7).
"""

from ce_worker.context import ArtifactIOError, PresignedRunContext
from ce_worker.model_cache import ModelCache, ModelCacheError
from ce_worker.runtime import SchedulerClient, WorkerConfig, WorkerRuntime, classify, usable_registry

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2

__all__ = [
    "ArtifactIOError",
    "ModelCache",
    "ModelCacheError",
    "PresignedRunContext",
    "SchedulerClient",
    "WorkerConfig",
    "WorkerRuntime",
    "classify",
    "usable_registry",
]
