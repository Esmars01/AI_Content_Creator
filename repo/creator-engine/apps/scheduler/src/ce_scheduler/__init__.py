"""GPU scheduler (§9, §25): task queue and leases, placement scoring, the internal worker API,
the lease reaper, Temporal async completion, a basic fleet manager and the cost ledger.

Status: implemented and tested in Phase 2 with the mock GPU provider and `worker-cpu`. OOM
escalation to larger VRAM classes and provider fallback on capacity loss arrive with the real
GPU families (Phase 8).
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2
