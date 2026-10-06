"""Mock provenance for `PROVENANCE_MODE=mock_dev` (ADR 0013, I11).

The watermarks are pass-throughs and the "signature" is a labelled JSON manifest; the render
worker burns the visible "MOCK PROVENANCE — NOT FOR DISTRIBUTION" label on such renders. The
plugin is registered only with `MOCK_GPU=true`, refuses to run outside dev and test, and
production refuses to start with mock provenance.
"""

from __future__ import annotations

import json
import os
import shutil

from ce_contracts.common import RunContext
from ce_contracts.interfaces import ProvenanceSigner, Watermarker
from ce_contracts.models import (
    SignRequest,
    SignResult,
    VerifyRequest,
    VerifyResult,
    WatermarkRequest,
    WatermarkResult,
)

from ce_plugins_mock._base import MockAdapter

__all__ = ["MockProvenance"]


class MockProvenance(MockAdapter, Watermarker, ProvenanceSigner):
    def _guard(self) -> None:
        if os.environ.get("APP_ENV", "dev") not in ("dev", "test"):
            raise RuntimeError("mock provenance is only allowed in dev and test (I11)")

    async def _passthrough(self, request: WatermarkRequest, ctx: RunContext, kind: str) -> WatermarkResult:
        self._guard()
        source = await ctx.read_artifact(request.media)
        out = self.workdir(ctx, f"wm-{kind}") / source.name
        shutil.copyfile(source, out)
        ref = await self.write(
            ctx,
            out,
            request.media.kind,
            role="media",
            mime=request.media.mime,
            watermark="mock_dev",
            payload_id=request.payload_id,
        )
        return WatermarkResult(media=ref, payload_id=request.payload_id, mode="mock_dev")

    async def run_provenance_watermark_video(self, request: WatermarkRequest, ctx: RunContext) -> WatermarkResult:
        return await self._passthrough(request, ctx, "video")

    async def run_provenance_watermark_audio(self, request: WatermarkRequest, ctx: RunContext) -> WatermarkResult:
        return await self._passthrough(request, ctx, "audio")

    async def run_provenance_sign(self, request: SignRequest, ctx: RunContext) -> SignResult:
        self._guard()
        manifest = {
            "mock": True,
            "notice": "MOCK PROVENANCE - NOT FOR DISTRIBUTION",
            "claim": request.manifest,
            "media_sha256": request.media.sha256,
        }
        out = self.workdir(ctx, "sign") / "manifest.json"
        out.write_text(json.dumps(manifest, sort_keys=True, indent=1), encoding="utf-8")
        ref = await self.write(ctx, out, "other", role="c2pa_manifest", mime="application/json")
        return SignResult(media=request.media, manifest=ref, mode="mock_dev")

    async def run_provenance_verify(self, request: VerifyRequest, ctx: RunContext) -> VerifyResult:
        """Mock layers carry nothing to detect: the answer says so (never a false 'present')."""
        self._guard()
        return VerifyResult(
            layer=request.layer,
            present=False,
            mode="mock_dev",
            payload_id=request.payload_id,
            detail={"notice": "mock provenance: no invisible watermark or signature was embedded"},
        )
