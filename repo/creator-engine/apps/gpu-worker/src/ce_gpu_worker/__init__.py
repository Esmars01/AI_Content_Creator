"""Generic worker entrypoint (§9 `gpu-worker`): loads the plugins of its runtime family and runs
the `ce_worker` lease loop. The `worker-cpu` build runs the `cpu_model` family (mock adapters
and CPU engines).

Status: implemented in Phase 2 for `worker-cpu`; GPU family images arrive in Phase 8.
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2
