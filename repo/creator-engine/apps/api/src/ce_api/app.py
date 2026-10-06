"""The FastAPI application (§9 `api`, §30).

`create_app()` builds services from the environment at startup (`uvicorn --factory
ce_api.app:create_app`); tests pass their own `Services`. The API never calls an LLM or a GPU
synchronously (§30): work that needs them is a job.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ce_config.settings import load_effective, startup_issues
from ce_core.ids import new_id
from ce_obs import bound_context, configure_logging, configure_tracing, extract_context, get_logger, get_tracer
from ce_obs.metrics import HTTP_REQUESTS, HTTP_SECONDS, serve_metrics
from ce_obs.tracing import SpanKind, Status, StatusCode
from fastapi import FastAPI, Request, Response

from ce_api import __version__
from ce_api.problems import PROBLEM_MEDIA_TYPE, Problem, install_problem_handlers
from ce_api.routers import (
    assets,
    auth,
    behavior,
    consents,
    edits,
    events,
    exports,
    gpu,
    health,
    identity,
    jobs,
    members,
    memory,
    models,
    planning,
    projects,
    qc,
    research,
    storage_local,
    studio,
    templates,
    videos,
    worlds,
)
from ce_api.services import Services, build_services

__all__ = ["create_app"]

_log = get_logger("ce.api")

DESCRIPTION = """Creator Engine public API (MASTER_BUILD_PROMPT §30).

- Authentication: session cookie from `POST /v1/auth/login` (send `X-CSRF-Token` on changes), or
  `Authorization: Bearer <API key>`.
- Errors are `application/problem+json` with stable `type` URIs (`urn:ce:problem:<code>`).
- Lists use cursor pagination (`?cursor=&limit=`).
- POSTs that start work need an `Idempotency-Key` header and return `202` with a `job_id`.
"""


PROBLEM_REF = {"$ref": "#/components/schemas/Problem"}
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"description": description, "content": {PROBLEM_MEDIA_TYPE: {"schema": PROBLEM_REF}}}
    for status, description in (
        (401, "Not signed in, or the session or API key is invalid"),
        (403, "Forbidden by role, API-key scope, CSRF or policy"),
        (404, "Not found (another organization's resources are indistinguishable from missing ones)"),
        (409, "Conflict: immutable record, failed approval requirements, duplicate"),
        (422, "Invalid input; `issues` lists each finding with its path"),
    )
}


def _openapi_with_problems(app: FastAPI) -> Callable[[], dict[str, Any]]:
    """Adds the Problem schema (and its nested models) to the components the error responses reference."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = generate()
            problem = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            components.update(problem.pop("$defs", {}))
            components["Problem"] = problem
            app.openapi_schema = schema
        return app.openapi_schema

    return openapi


def _services_from_env(config_root: Path) -> Services:
    effective = load_effective(config_root)
    errors = [i for i in startup_issues(effective) if i.severity == "error"]
    if errors:
        raise RuntimeError("refusing to start: " + "; ".join(f"{i.code}: {i.message}" for i in errors))
    return build_services(effective)


_tracer = get_tracer("ce.api")


def create_app(services: Services | None = None, *, config_root: Path | str = "config") -> FastAPI:
    owned = services is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.services is None:
            built = _services_from_env(Path(config_root))
            configure_logging("api", built.settings.log_level)
            configure_tracing("api", built.settings.otel_exporter_otlp_endpoint, environment=built.settings.app_env)
            # Metrics on a side port (§34): never on the public API port, so they are not exposed to clients
            serve_metrics(int(os.environ.get("METRICS_PORT", "0")))
            app.state.services = built
        try:
            yield
        finally:
            if owned and app.state.services is not None:
                await app.state.services.close()

    app = FastAPI(
        title="Creator Engine API",
        version=__version__,
        description=DESCRIPTION,
        openapi_url="/openapi.json",
        lifespan=lifespan,
        telemetry={"logs": False, "metrics": False, "auto_configure": False},
    )
    app.state.services = services
    install_problem_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or str(new_id())
        if len(request_id) > 128 or not request_id.isprintable():
            request_id = str(new_id())
        started = time.perf_counter()
        # one server span per request, continuing the caller's W3C trace context when it sends one
        with (
            _tracer.start_as_current_span(
                request.method,  # renamed to the route template below; a raw path can carry a token
                context=extract_context(request.headers),
                kind=SpanKind.SERVER,
                attributes={"http.request.method": request.method, "ce.request_id": request_id},
            ) as span,
            bound_context(request_id=request_id),
        ):
            response = await call_next(request)
            elapsed = time.perf_counter() - started
            # The matched route template (never the raw path: ids would explode the label set)
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"
            span.update_name(f"{request.method} {route}")
            span.set_attribute("http.route", route)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            HTTP_REQUESTS.labels("api", request.method, route, str(response.status_code)).inc()
            HTTP_SECONDS.labels("api", request.method, route).observe(elapsed)
            principal = getattr(request.state, "principal", None)
            _log.info(
                "request",
                method=request.method,
                route=route,
                # the raw path helps to debug, except where it carries a secret (invitation tokens)
                path=route if "{token}" in route else request.url.path,
                status=response.status_code,
                duration_ms=round(elapsed * 1000, 1),
                org_id=str(principal.org_id) if principal else None,
            )
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/v1/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.openapi = _openapi_with_problems(app)  # type: ignore[method-assign]
    for module in (health, storage_local):
        app.include_router(module.router)
    routers = (
        auth,
        members,
        projects,
        videos,
        planning,
        behavior,
        edits,
        identity,
        worlds,
        memory,
        assets,
        jobs,
        models,
        gpu,
        studio,
        consents,
        qc,
        research,
        templates,
        exports,
    )
    for module in (*routers, events):
        app.include_router(module.router, responses=PROBLEM_RESPONSES)
    return app
