"""Liveness and readiness probes (Compose healthchecks, load balancers)."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ce_api.deps import ServicesDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(services: ServicesDep) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with services.db.session() as session:
            await session.execute(sa.text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = type(exc).__name__
    try:
        await services.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = type(exc).__name__
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)
