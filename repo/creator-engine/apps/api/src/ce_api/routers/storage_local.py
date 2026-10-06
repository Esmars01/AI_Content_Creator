"""Serves presigned URLs of the local-filesystem storage provider (native fallback, ADR 0030).

Answers only when the configured provider serves its own presigned URLs
(`STORAGE_PROVIDER=local_fs`). The route trusts nothing: the provider verifies
the signature, method, expiry and signed content type before reading or writing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ce_storage.base import LOCAL_ROUTE_PREFIX, PresignedEndpoint
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from ce_api.deps import ServicesDep

router = APIRouter(include_in_schema=False)


@router.api_route(LOCAL_ROUTE_PREFIX + "/{path:path}", methods=["GET", "PUT"])
async def local_object(path: str, request: Request, services: ServicesDep) -> Response:
    storage = services.storage
    if not isinstance(storage, PresignedEndpoint):
        return Response(status_code=404)
    max_bytes = services.config.uploads.max_bytes
    with tempfile.TemporaryDirectory(prefix="ce-local-put-") as tmp:
        body: Path | None = None
        if request.method == "PUT":
            body = Path(tmp) / "body"
            size = 0
            with body.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        return Response(status_code=413)
                    out.write(chunk)
        result = await storage.serve_presigned(request.method, str(request.url), dict(request.headers), body)
    if result.file is not None:
        return FileResponse(result.file, headers=result.headers, media_type=result.headers.get("Content-Type"))
    return Response(content=result.body, status_code=result.status, headers=result.headers)
