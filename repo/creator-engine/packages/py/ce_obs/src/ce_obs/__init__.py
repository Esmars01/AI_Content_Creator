"""Logging, tracing and metrics helpers (§34).

Status: logging (structlog JSON, bound job context, trace ids, secret redaction) and tracing
(OpenTelemetry provider, OTLP/HTTP export, W3C propagation) are implemented and tested in
Phase 1. Phase 2 adds Prometheus metrics (`ce_obs.metrics`) and moves the org event streams
(`ce_obs.events`) here so the orchestrator and scheduler publish the same SSE events as the API.
"""

from ce_obs.logs import CONTEXT_KEYS, bind_context, bound_context, clear_context, configure_logging, get_logger
from ce_obs.redact import REDACTED, is_secret_key, redact, redact_text
from ce_obs.tracing import configure_tracing, current_trace_id, extract_context, get_tracer, inject_context

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2

__all__ = [
    "CONTEXT_KEYS",
    "REDACTED",
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "configure_tracing",
    "current_trace_id",
    "extract_context",
    "get_logger",
    "get_tracer",
    "inject_context",
    "is_secret_key",
    "redact",
    "redact_text",
]
