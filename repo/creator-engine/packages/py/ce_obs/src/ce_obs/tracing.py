"""OpenTelemetry tracing (§34): one tracer provider per service, W3C context propagation.

With `OTEL_EXPORTER_OTLP_ENDPOINT` set, spans are exported over OTLP/HTTP to
`<endpoint>/v1/traces`; without it, spans are still created (so logs carry trace ids) but not
exported. The API opens a server span per request (continuing an incoming `traceparent`) and
each locally executed build node runs in a span. `inject_context` / `extract_context` are the W3C
helpers; the trace is not yet carried across the Temporal boundary, so a job's node spans start
their own traces (docs/OBSERVABILITY.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

__all__ = [
    "SpanKind",
    "Status",
    "StatusCode",
    "configure_tracing",
    "current_trace_id",
    "extract_context",
    "get_tracer",
    "inject_context",
]


def configure_tracing(
    service: str,
    endpoint: str | None = None,
    *,
    exporter: SpanExporter | None = None,
    environment: str | None = None,
) -> TracerProvider:
    """Build the provider for `service`. `exporter` overrides OTLP (tests use an in-memory exporter).

    The global provider can be set once per process (an OpenTelemetry rule); the returned
    provider is usable directly either way.
    """
    attributes: dict[str, Any] = {"service.name": service}
    if environment:
        attributes["deployment.environment"] = environment
    provider = TracerProvider(resource=Resource.create(attributes))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    return provider


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def inject_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """The current trace context as W3C headers (`traceparent`, `tracestate`)."""
    out: dict[str, str] = {} if carrier is None else carrier
    propagate.inject(out)
    return out


def extract_context(carrier: Mapping[str, str]) -> otel_context.Context:
    return propagate.extract(dict(carrier))
