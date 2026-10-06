"""`GET /v1/renders/{id}/verify` (§27): checks both provenance layers of a delivered render.

The render's file is fetched from storage and handed to the provenance adapters its build
recorded (the signer's `provenance.verify` for the C2PA layer, each watermarker's for its layer),
through the plugin registry — the API never imports a plugin or c2pa itself (I14). The adapters run
in-process and only read: verifying a manifest is milliseconds of work.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from ce_contracts import models as m
from ce_contracts.common import ArtifactRef, CancellationToken, LoadContext
from ce_contracts.plugins import PluginRegistry, discover
from ce_policy.c2pa_verify import classify_c2pa

__all__ = ["verify_render_file"]


@lru_cache(maxsize=4)
def _registry(app_env: str, include_mocks: bool) -> PluginRegistry:
    return discover(app_env=app_env, include_mocks=include_mocks)


class _FileContext:
    """A read-only run context over one downloaded file."""

    def __init__(self, path: Path, scratch: Path) -> None:
        self.path = path
        self.scratch_dir = scratch
        self.seed = 0
        self.cancel = CancellationToken()

    async def read_artifact(self, ref: ArtifactRef) -> Path:
        return self.path

    async def write_artifact(
        self, path: Path, kind: str, meta: dict[str, Any] | None = None, *, role: str = "", mime: str = ""
    ) -> ArtifactRef:
        raise PermissionError("verification is read-only")

    async def progress(self, fraction: float, message: str = "") -> None:
        return None


async def _run(
    registry: PluginRegistry, adapter_id: str, request: m.VerifyRequest, ctx: _FileContext, settings: Any
) -> m.VerifyResult | None:
    if adapter_id not in registry.plugins:
        return None
    plugin = registry.get(adapter_id)
    if plugin.manifest.capability("provenance.verify") is None:
        return None
    adapter = plugin.adapter()
    await adapter.load(
        LoadContext(
            model_cache_dir=settings.model_cache_dir, scratch_dir=str(ctx.scratch_dir), app_env=settings.app_env
        )
    )
    result = await adapter.run("provenance.verify", request, ctx)
    assert isinstance(result, m.VerifyResult)
    return result


async def verify_render_file(
    path: Path,
    *,
    sha256: str,
    mime: str,
    routes: dict[str, str],
    payload_id: str | None,
    provenance_mode: str,
    settings: Any,
) -> dict[str, Any]:
    """`routes`: layer (`c2pa`, `watermark_video`, `watermark_audio`) → the adapter that wrote it."""
    registry = await asyncio.to_thread(_registry, settings.app_env, bool(settings.mock_gpu))
    scratch = Path(tempfile.mkdtemp(prefix="ce-verify-"))
    try:
        ctx = _FileContext(path, scratch)
        ref = ArtifactRef(artifact_id=f"render:{sha256[:16]}", sha256=sha256, kind="video", mime=mime)
        out: dict[str, Any] = {"provenance_mode": provenance_mode}
        c2pa_id = routes.get("c2pa")
        c2pa = (
            await _run(registry, c2pa_id, m.VerifyRequest(media=ref, layer="c2pa"), ctx, settings) if c2pa_id else None
        )
        if c2pa is None:
            out["c2pa"] = {"present": False, "adapter_id": c2pa_id, "detail": "no verifier available for this render"}
        else:
            verdict = classify_c2pa(c2pa.present, c2pa.success, c2pa.failures)
            out["c2pa"] = {
                **verdict.as_dict(),
                "adapter_id": c2pa_id,
                "mode": c2pa.mode,
                "state": c2pa.state,
                "payload_id": c2pa.payload_id,
                "assertions": c2pa.detail.get("assertions", {}),
            }
        marks: dict[str, Any] = {}
        for layer in ("watermark_video", "watermark_audio"):
            adapter_id = routes.get(layer)
            result = (
                await _run(
                    registry, adapter_id, m.VerifyRequest(media=ref, layer=layer, payload_id=payload_id), ctx, settings
                )  # type: ignore[arg-type]
                if adapter_id
                else None
            )
            marks[layer.removeprefix("watermark_")] = (
                {"present": False, "adapter_id": adapter_id, "detail": "no detector available for this layer"}
                if result is None
                else {
                    "present": result.present,
                    "mode": result.mode,
                    "adapter_id": adapter_id,
                    "payload_id": result.payload_id,
                    "payload_matches": bool(result.present and result.payload_id == payload_id),
                    **result.detail,
                }
            )
        out["watermarks"] = marks
        c2pa_ok = bool(out["c2pa"].get("ok_untrusted_root"))
        marks_ok = all(v.get("payload_matches") for v in marks.values())
        if provenance_mode != "real":
            out["verdict"] = "mock_dev" if c2pa_ok else "invalid"
        elif c2pa_ok and marks_ok:
            out["verdict"] = "valid" if out["c2pa"].get("trusted") else "valid_untrusted_root"
        else:
            out["verdict"] = "invalid"
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
