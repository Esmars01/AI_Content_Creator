"""ce_obs: redaction, JSON logs with context and trace ids, tracing export and propagation."""

from __future__ import annotations

import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
from ce_obs import (
    REDACTED,
    bind_context,
    bound_context,
    clear_context,
    configure_logging,
    configure_tracing,
    extract_context,
    get_logger,
    inject_context,
    is_secret_key,
    redact,
    redact_text,
)
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "SECRET_KEY",
        "s3_secret_access_key",
        "S3_ACCESS_KEY_ID",
        "apiKey",
        "api-key",
        "worker_token",
        "Authorization",
        "set-cookie",
        "sentry_dsn",
        "X-Amz-Signature",
        "private_key",
        "accessKeyId",
        "credentials",
    ],
)
def test_secret_keys(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key", ["key", "storage_key", "shot_key", "input_tokens", "tokenizer_version", "author", "auth_provider", "event"]
)
def test_ordinary_keys(key: str) -> None:
    assert not is_secret_key(key)


# Credential-shaped values are assembled at runtime so this file contains no secret shapes
# (rule 16; tests/phase0/test_no_secrets.py scans committed files).
FAKE_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS's documented example id
FAKE_PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQ\n-----END " + "PRIVATE KEY-----"
FAKE_ANTHROPIC = "sk-" + "ant-api03-abcdefghijklmnop"


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Authorization: Bearer abcdef0123456789xyz", "abcdef0123456789xyz"),
        ("postgresql+asyncpg://ce:ce_dev_password@localhost:5432/db", "ce_dev_password"),
        ("http://s3:8333/b/k?X-Amz-Credential=AKIAXX%2F2026&X-Amz-Signature=deadbeef1234", "deadbeef1234"),
        ("http://api/v1/storage/local/b/k?X-CE-Expires=1&X-CE-Signature=cafebabe99", "cafebabe99"),
        (f"key {FAKE_ANTHROPIC} used", FAKE_ANTHROPIC),
        ("hf token hf_abcdefghijklmnopqrstuvwxyz", "hf_abcdefghijklmnopqrstuvwxyz"),
        (FAKE_AWS_KEY_ID, FAKE_AWS_KEY_ID),
        (FAKE_PEM, "MIIEvQ"),
        ("api key ce_live_0123456789abcdefghij", "ce_live_0123456789abcdefghij"),
    ],
)
def test_secret_values_are_masked(text: str, leak: str) -> None:
    out = redact_text(text)
    assert leak not in out and REDACTED in out


def test_redact_is_recursive_and_keeps_ordinary_values() -> None:
    event = {
        "event": "upload",
        "storage_key": "orgs/1/assets/2/original",
        "headers": {"Authorization": "Basic dXNlcjpwYXNz", "content-type": "video/mp4"},
        "attempts": [{"token": "abc"}, "Bearer 0123456789abcdef"],
        "secret_key": SecretStr("zzz"),
        "settings": SecretStr("hidden"),
        "password": None,
    }
    out = redact(event)
    assert out["storage_key"] == "orgs/1/assets/2/original"
    assert out["headers"] == {"Authorization": REDACTED, "content-type": "video/mp4"}
    assert out["attempts"] == [{"token": REDACTED}, f"Bearer {REDACTED}"]
    assert out["secret_key"] == REDACTED and out["settings"] == REDACTED
    assert out["password"] is None  # nothing to hide


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_logs_carry_context_and_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging("api", "debug", stream=stream)
    clear_context()
    log = get_logger("ce.test")
    bind_context(org_id="org-1", request_id="req-9")
    with bound_context(job_id="job-7", node_id=None):
        log.info("asset uploaded", password="hunter2hunter2", url="https://u:p4ss@host/x")
    log.info("after")
    logging.getLogger("uvicorn.error").warning("db at postgresql://ce:supersecret@db/x is down")
    try:
        raise RuntimeError("connect failed: Bearer abcdef0123456789zzz")
    except RuntimeError:
        log.exception("boom")
    clear_context()
    first, after, stdlib, failure = _lines(stream)
    assert first["event"] == "asset uploaded" and first["level"] == "info" and first["service"] == "api"
    assert (first["org_id"], first["request_id"], first["job_id"]) == ("org-1", "req-9", "job-7")
    assert "node_id" not in first and "job_id" not in after
    assert first["password"] == REDACTED and "p4ss" not in first["url"]
    assert first["timestamp"].endswith("Z")
    assert stdlib["logger"] == "uvicorn.error" and "supersecret" not in stdlib["event"]
    assert "abcdef0123456789zzz" not in json.dumps(failure) and "RuntimeError" in failure["exception"]
    assert "hunter2" not in stream.getvalue()


def test_logs_inside_a_span_carry_its_trace_ids() -> None:
    stream = io.StringIO()
    configure_logging("orchestrator", stream=stream)
    exporter = InMemorySpanExporter()
    provider = configure_tracing("orchestrator", exporter=exporter, environment="test")
    with provider.get_tracer("ce.test").start_as_current_span("generate") as span:
        get_logger().info("inside")
        context = span.get_span_context()
    get_logger().info("outside")
    inside, outside = _lines(stream)
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")
    assert "trace_id" not in outside
    (finished,) = exporter.get_finished_spans()
    assert finished.name == "generate" and finished.resource.attributes["service.name"] == "orchestrator"


def test_trace_context_propagates_through_a_carrier() -> None:
    provider = configure_tracing("api", exporter=InMemorySpanExporter())
    tracer = provider.get_tracer("ce.test")
    with tracer.start_as_current_span("api.request") as parent:
        carrier = inject_context()
    assert carrier["traceparent"].split("-")[1] == format(parent.get_span_context().trace_id, "032x")
    with tracer.start_as_current_span("worker.task", context=extract_context(carrier)) as child:
        assert child.get_span_context().trace_id == parent.get_span_context().trace_id
        assert trace.get_current_span() is child


class _Collector(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, str, int]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.received.append((self.path, self.headers.get("Content-Type", ""), length))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


def test_spans_are_exported_over_otlp_http() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = configure_tracing("api", endpoint=f"http://127.0.0.1:{server.server_port}/")
        with provider.get_tracer("ce.test").start_as_current_span("exported"):
            pass
        assert provider.force_flush(10_000)
        provider.shutdown()
    finally:
        server.shutdown()
    assert _Collector.received, "the collector received nothing"
    path, content_type, size = _Collector.received[0]
    assert (path, content_type) == ("/v1/traces", "application/x-protobuf") and size > 0
