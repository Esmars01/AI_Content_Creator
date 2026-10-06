"""Structured logging (§34): structlog JSON lines with trace and job context, always redacted.

`configure_logging` routes both structlog and standard-library loggers (uvicorn, SQLAlchemy,
botocore, Temporal) through one processor chain, so every line is JSON, carries the bound
context (`org_id`, `job_id`, `node_id`, `attempt_id`, `worker_id`, `request_id`) and the active
OpenTelemetry `trace_id`/`span_id`, and passes the secret redactor last.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import IO, Any

import structlog
from opentelemetry import trace

from ce_obs.redact import redact_processor

__all__ = ["CONTEXT_KEYS", "bind_context", "bound_context", "clear_context", "configure_logging", "get_logger"]

CONTEXT_KEYS = ("org_id", "user_id", "request_id", "job_id", "node_id", "attempt_id", "worker_id")


def add_trace_ids(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict.setdefault("trace_id", format(context.trace_id, "032x"))
        event_dict.setdefault("span_id", format(context.span_id, "016x"))
    return event_dict


def _shared_processors(service: str) -> list[Any]:
    def add_service(_l: Any, _m: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service)
        return event_dict

    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_service,
        add_trace_ids,
    ]


def configure_logging(service: str, level: str = "info", *, json: bool = True, stream: IO[str] | None = None) -> None:
    """Idempotent; call once at service start (and in tests with a captured stream)."""
    shared = _shared_processors(service)
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_ce_obs", False):
            root.removeHandler(existing)
    handler._ce_obs = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    # HTTP clients log every request at INFO (worker long polls, presigned transfers): keep them for debug.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG if level.lower() == "debug" else logging.WARNING)
    # Servers that install their own handlers (uvicorn) would bypass the JSON chain and the redactor.
    # An access logger left without handlers means "access log off" (uvicorn --no-access-log): keep it off.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        if name == "uvicorn.access" and not server_logger.handlers and not server_logger.propagate:
            continue
        server_logger.handlers.clear()
        server_logger.propagate = True
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.stdlib.get_logger(name)


def bind_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**{k: str(v) for k, v in values.items() if v is not None})


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


@contextmanager
def bound_context(**values: Any) -> Iterator[None]:
    tokens = structlog.contextvars.bind_contextvars(**{k: str(v) for k, v in values.items() if v is not None})
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
