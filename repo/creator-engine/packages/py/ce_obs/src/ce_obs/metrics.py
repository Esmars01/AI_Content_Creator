"""Prometheus metrics (§34): one registry per process, named `ce_*`.

Every service serves them on a side port (`serve_metrics(METRICS_PORT)`): the API, scheduler,
orchestrator, render worker and GPU worker. Neither the public API port nor the scheduler's worker
API port (which remote GPU workers reach) serves them. Labels are low-cardinality only (capability, kind, status,
pool), never ids.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "FLEET_DESIRED",
    "GPU_COMPLETIONS_DEFERRED",
    "GPU_LEASES",
    "GPU_LEASE_EXPIRED",
    "GPU_QUEUE",
    "GPU_TASKS_DONE",
    "HTTP_REQUESTS",
    "HTTP_SECONDS",
    "NODES",
    "NODE_SECONDS",
    "REGISTRY",
    "RENDER_SECONDS",
    "WORKERS",
    "WORKER_TASKS",
    "render_latest",
    "serve_metrics",
]

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "ce_http_requests_total", "HTTP requests", ["service", "method", "route", "status"], registry=REGISTRY
)
HTTP_SECONDS = Histogram(
    "ce_http_request_seconds", "HTTP request latency", ["service", "method", "route"], registry=REGISTRY
)
GPU_QUEUE = Gauge("ce_gpu_tasks", "GPU tasks by state", ["state", "capability"], registry=REGISTRY)
GPU_LEASES = Counter("ce_gpu_leases_total", "Tasks leased to workers", ["capability"], registry=REGISTRY)
GPU_TASKS_DONE = Counter("ce_gpu_tasks_done_total", "Finished GPU tasks", ["capability", "status"], registry=REGISTRY)
GPU_LEASE_EXPIRED = Counter("ce_gpu_lease_expired_total", "Leases that expired and were requeued", registry=REGISTRY)
GPU_COMPLETIONS_DEFERRED = Counter(
    "ce_gpu_completions_deferred_total",
    "Task results Temporal could not take at once, kept on the task and redelivered",
    registry=REGISTRY,
)
WORKERS = Gauge("ce_workers", "Registered workers by state", ["state", "family"], registry=REGISTRY)
FLEET_DESIRED = Gauge("ce_fleet_desired_workers", "Desired workers per pool", ["pool"], registry=REGISTRY)
# Phase 9: fleet and providers (§25, §34)
COLD_START_SECONDS = Histogram(
    "ce_worker_cold_start_seconds", "Provision request → worker registration", ["gpu_type"], registry=REGISTRY,
    buckets=(5, 15, 30, 60, 120, 300, 600, 900, 1800),
)  # fmt: skip
MODEL_LOAD_SECONDS = Histogram(
    "ce_model_load_seconds", "Adapter load time on a worker (fetch excluded)", ["adapter", "gpu_type"],
    registry=REGISTRY, buckets=(0.5, 2, 5, 15, 30, 60, 120, 300, 600),
)  # fmt: skip
MODEL_FETCH_SECONDS = Histogram(
    "ce_model_fetch_seconds", "Model cache fetch time before a load", ["adapter"], registry=REGISTRY,
    buckets=(1, 10, 30, 60, 300, 900, 1800, 3600),
)  # fmt: skip
LEASE_WAIT_SECONDS = Histogram(
    "ce_gpu_lease_wait_seconds", "Queued → leased", ["capability"], registry=REGISTRY,
    buckets=(0.5, 1, 5, 15, 30, 60, 300, 900, 3600),
)  # fmt: skip
FLEET_SPEND = Gauge("ce_fleet_spend_usd", "Today's spend (UTC) and its projection", ["kind"], registry=REGISTRY)
FLEET_PROVISIONS = Counter(
    "ce_fleet_provisions_total", "Provision attempts by provider and result", ["provider", "result"], registry=REGISTRY
)
TASKS_HELD = Gauge("ce_gpu_tasks_held", "Queued tasks held by budget rules", ["reason"], registry=REGISTRY)
WORKER_TASKS = Counter("ce_worker_tasks_total", "Tasks run by this worker", ["capability", "status"], registry=REGISTRY)
NODES = Counter("ce_build_nodes_total", "Build nodes by outcome", ["kind", "outcome"], registry=REGISTRY)
NODE_SECONDS = Histogram("ce_build_node_seconds", "Build node wall time", ["kind"], registry=REGISTRY)
RENDER_SECONDS = Histogram("ce_render_seconds", "Render activity wall time", ["kind"], registry=REGISTRY)


# Phase 14: database-derived operations metrics (`ce_db.ops_metrics`), set by the scheduler leader
# over a trailing window (`ce_ops_window_seconds`); one writer per series.
OPS_WINDOW = Gauge("ce_ops_window_seconds", "Trailing window of the ce_ops_* gauges", registry=REGISTRY)
OPS_QC = Gauge("ce_ops_qc_shots", "Shot QC verdicts", ["adapter", "language", "verdict"], registry=REGISTRY)
OPS_ATTEMPTS = Gauge(
    "ce_ops_attempts", "Node attempts by reason, status and error class", ["reason", "status", "error_class"],
    registry=REGISTRY,
)  # fmt: skip
OPS_COVERAGE = Gauge(
    "ce_ops_coverage_observations", "Behavior observations by outcome", ["adapter", "dimension", "outcome"],
    registry=REGISTRY,
)  # fmt: skip
OPS_NOT_MEASURABLE = Gauge(
    "ce_ops_not_measurable", "NOT_MEASURABLE observations by dimension", ["dimension"], registry=REGISTRY
)
OPS_WORLD_QC = Gauge("ce_ops_world_qc", "World and continuity QC verdicts", ["verdict"], registry=REGISTRY)
OPS_CONSISTENCY = Gauge("ce_ops_consistency", "Creator consistency verdicts", ["verdict"], registry=REGISTRY)
OPS_MEMORY_CONFLICTS = Gauge("ce_ops_memory_conflicts_open", "Unresolved memory conflicts", registry=REGISTRY)
OPS_SPEND = Gauge("ce_ops_spend_usd", "Ledger spend in the window", registry=REGISTRY)
OPS_RENDERED_MINUTES = Gauge(
    "ce_ops_rendered_minutes", "Planned minutes of the versions with a final render in the window", registry=REGISTRY
)
OPS_COLLECT_ERRORS = Counter("ce_ops_collect_errors_total", "Failed ops-metrics collections", registry=REGISTRY)


def render_latest() -> bytes:
    return generate_latest(REGISTRY)


def serve_metrics(port: int, addr: str = "0.0.0.0") -> None:  # noqa: S104 - metrics bind inside the container network
    """Serves `/metrics` on a side port (Temporal workers and GPU workers have no HTTP app)."""
    if port > 0:
        start_http_server(port, addr=addr, registry=REGISTRY)
