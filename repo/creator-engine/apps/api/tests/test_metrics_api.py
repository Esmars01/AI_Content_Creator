"""Request metrics (§34, Phase 14): every request is counted and timed under its route template —
never the raw path, whose ids would explode the label set — and the API serves no `/metrics` on
its public port (metrics are on the side port `METRICS_PORT`)."""

from __future__ import annotations

import re
import uuid

import pytest
from ce_api.testing import ApiTenant
from ce_obs.metrics import REGISTRY

pytestmark = pytest.mark.infra
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-")


def _count(method: str, route: str, status: str) -> float:
    labels = {"service": "api", "method": method, "route": route, "status": status}
    return REGISTRY.get_sample_value("ce_http_requests_total", labels) or 0.0


async def test_requests_are_counted_by_route_template(owner: ApiTenant) -> None:
    route = "/v1/projects/{project_id}"
    before = _count("GET", route, "404")
    for _ in range(2):
        response = await owner.client.get(f"/v1/projects/{uuid.uuid4()}")
        assert response.status_code == 404
    assert _count("GET", route, "404") == before + 2
    samples = [s for m in REGISTRY.collect() if m.name == "ce_http_requests" for s in m.samples]
    assert not any(UUID_RE.search(s.labels.get("route", "")) for s in samples), "raw ids in route labels"
    seconds = REGISTRY.get_sample_value(
        "ce_http_request_seconds_count", {"service": "api", "method": "GET", "route": route}
    )
    assert seconds is not None and seconds >= 2


async def test_metrics_are_not_served_on_the_public_port(owner: ApiTenant) -> None:
    response = await owner.client.get("/metrics")
    assert response.status_code == 404
    assert _count("GET", "unmatched", "404") >= 1


async def test_each_request_is_a_server_span_that_continues_the_callers_trace(
    owner: ApiTenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ce_api.app
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # a local provider (the global one can be set only once per process)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ce_api.app, "_tracer", provider.get_tracer("ce.api"))
    caller_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    response = await owner.client.get(
        f"/v1/projects/{uuid.uuid4()}", headers={"traceparent": f"00-{caller_trace}-00f067aa0ba902b7-01"}
    )
    assert response.status_code == 404
    (span,) = exporter.get_finished_spans()
    assert span.name == "GET /v1/projects/{project_id}"
    assert format(span.context.trace_id, "032x") == caller_trace
    assert span.attributes is not None and span.attributes["http.response.status_code"] == 404
