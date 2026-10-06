"""RFC 9457 problem details (§30): every error is `application/problem+json` with a stable `type`.

`type` is `urn:ce:problem:<code>`, where `code` is the stable `CEError.code` (`not_found`,
`immutable`, `locked`, `invalid_input`, …). Validation findings travel in `issues`, each with
`code`, `message` and, when it concerns a spec element, `path` (a SpecPath).
Unexpected exceptions become a 500 without internals; the log line carries the details.
"""

from __future__ import annotations

from typing import Any

from ce_core.errors import CEError, Issue
from ce_obs import get_logger
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = ["PROBLEM_MEDIA_TYPE", "Problem", "ProblemIssue", "install_problem_handlers", "problem_response"]

PROBLEM_MEDIA_TYPE = "application/problem+json"
_log = get_logger("ce.api.problems")

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "invalid_input",
    429: "rate_limited",
    501: "not_implemented",
}


class ProblemIssue(BaseModel):
    code: str
    message: str
    path: str | None = None
    severity: str = "error"
    detail: dict[str, Any] = Field(default_factory=dict)


class Problem(BaseModel):
    type: str = Field(examples=["urn:ce:problem:not_found"])
    title: str = Field(examples=["Not found"])
    status: int = Field(examples=[404])
    detail: str | None = Field(default=None, examples=["creators not found"])
    instance: str | None = Field(default=None, examples=["/v1/creators/0192f0a0-0000-7000-8000-000000000010"])
    code: str = Field(examples=["not_found"])
    issues: list[ProblemIssue] = Field(default_factory=list)


def _issue(issue: Issue) -> ProblemIssue:
    return ProblemIssue(
        code=issue.code, message=issue.message, path=issue.path, severity=issue.severity, detail=issue.detail
    )


def problem_response(
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    *,
    instance: str | None = None,
    issues: list[ProblemIssue] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        type=f"urn:ce:problem:{code}",
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        code=code,
        issues=issues or [],
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(CEError)
    async def ce_error(request: Request, exc: CEError) -> JSONResponse:
        headers = {"Retry-After": str(exc.detail["retry_after_s"])} if "retry_after_s" in exc.detail else None
        return problem_response(
            exc.status,
            exc.code,
            exc.title,
            exc.message,
            instance=request.url.path,
            issues=[_issue(i) for i in exc.issues],
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            ProblemIssue(
                code=str(err.get("type", "invalid")),
                message=str(err.get("msg", "invalid value")),
                path="/" + "/".join(str(p) for p in err.get("loc", ())),
            )
            for err in exc.errors()
        ]
        return problem_response(
            422,
            "invalid_input",
            "Invalid input",
            "the request does not match its schema",
            instance=request.url.path,
            issues=issues,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "error")
        title = code.replace("_", " ").capitalize()
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(
            exc.status_code, code, title, detail, instance=request.url.path, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("unhandled error", path=request.url.path, method=request.method)
        return problem_response(500, "internal", "Internal error", None, instance=request.url.path)
