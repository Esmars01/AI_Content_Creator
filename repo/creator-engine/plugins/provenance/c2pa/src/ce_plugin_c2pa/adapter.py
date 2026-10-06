"""`provenance.sign`: a C2PA manifest embedded in the render (§27).

The manifest records: `c2pa.actions.v2` with `c2pa.created` and the digital source type
(`trainedAlgorithmicMedia`, or `compositeWithTrainedAlgorithmicMedia` when real footage is
included), and a `com.creatorengine.generation` assertion with the routes (adapter@revision), the
consent ids, the watermark payload id and the provenance mode of each watermark layer.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import ProvenanceSigner
from ce_contracts.models import SignRequest, SignResult, VerifyRequest, VerifyResult

from ce_plugin_c2pa.dev_ca import ensure_dev_ca

__all__ = ["SOURCE_TYPES", "C2paSigner", "build_manifest"]

SOURCE_TYPES = {
    "trainedAlgorithmicMedia": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
    "compositeWithTrainedAlgorithmicMedia": "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia",
}


def build_manifest(claim: dict[str, Any], *, generator: str, version: str, title: str, mime: str) -> dict[str, Any]:
    source = SOURCE_TYPES.get(
        str(claim.get("digital_source_type", "trainedAlgorithmicMedia")), SOURCE_TYPES["trainedAlgorithmicMedia"]
    )
    generation = {k: v for k, v in claim.items() if k != "digital_source_type"}
    return {
        "claim_generator_info": [{"name": generator, "version": version}],
        "title": title,
        "format": mime,
        "assertions": [
            {
                "label": "c2pa.actions.v2",
                "data": {
                    "actions": [
                        {
                            "action": "c2pa.created",
                            "digitalSourceType": source,
                            "softwareAgent": {"name": generator, "version": version},
                        }
                    ]
                },
            },
            {"label": "com.creatorengine.generation", "data": generation},
        ],
    }


class C2paSigner(ProvenanceSigner):
    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self.cert_pem: bytes = b""
        self.key_pem: bytes = b""
        self.dev_root: Path | None = None
        self._ctx = LoadContext(model_cache_dir=".", scratch_dir=tempfile.gettempdir(), app_env="dev")

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        self._ctx = ctx
        if ctx.app_env == "prod" and not (
            self.config.get("c2pa_signing_cert_path") and self.config.get("c2pa_signing_key_path")
        ):
            raise RuntimeError("C2PA_SIGNING_CERT_PATH and C2PA_SIGNING_KEY_PATH are required in production (§27)")

    async def _credentials(self) -> None:
        """Operator certificates when configured; otherwise (dev/test) a generated test CA."""
        if self.cert_pem:
            return
        cert = self.config.get("c2pa_signing_cert_path")
        key = self.config.get("c2pa_signing_key_path")
        if cert and key:
            self.cert_pem, self.key_pem = Path(cert).read_bytes(), Path(key).read_bytes()
            return
        directory = Path(self._ctx.scratch_dir or tempfile.gettempdir()) / "c2pa-dev-ca"
        certs = await asyncio.to_thread(
            ensure_dev_ca, directory, validity_days=int(self.config["dev_ca_validity_days"])
        )
        self.cert_pem, self.key_pem = certs.chain_pem.read_bytes(), certs.key_pem.read_bytes()
        self.dev_root = certs.root_pem

    def _sign(self, source: Path, dest: Path, manifest: dict[str, Any]) -> str:
        import c2pa

        info = c2pa.C2paSignerInfo(str(self.config["algorithm"]).encode(), self.cert_pem, self.key_pem, None)
        signer = c2pa.Signer.from_info(info)
        try:
            with c2pa.Builder(json.dumps(manifest)) as builder:
                builder.sign_file(str(source), str(dest), signer)
        finally:
            signer.close()
        with c2pa.Reader(str(dest)) as reader:
            return str(reader.json())

    async def run_provenance_sign(self, request: SignRequest, ctx: RunContext) -> SignResult:
        if not self.loaded:
            raise RuntimeError("c2pa_signer is not loaded")
        await self._credentials()
        source = await ctx.read_artifact(request.media)
        suffix = {"video/mp4": ".mp4", "audio/wav": ".wav", "image/png": ".png", "image/jpeg": ".jpg"}.get(
            request.media.mime, source.suffix or ".mp4"
        )
        work = Path(ctx.scratch_dir) / "c2pa"
        work.mkdir(parents=True, exist_ok=True)
        unsigned = work / f"unsigned{suffix}"
        shutil.copyfile(source, unsigned)  # c2pa picks the format from the extension
        signed = work / f"signed{suffix}"
        manifest = build_manifest(
            dict(request.manifest),
            generator=str(self.config["claim_generator"]),
            version=str(self.config["claim_generator_version"]),
            title=str(request.labels.get("title") or f"render {request.media.sha256[:12]}"),
            mime=request.media.mime or "video/mp4",
        )
        store = await asyncio.to_thread(self._sign, unsigned, signed, manifest)
        manifest_file = work / "manifest_store.json"
        manifest_file.write_text(store, encoding="utf-8")
        media = await ctx.write_artifact(
            signed,
            request.media.kind,
            {"c2pa": True, "signed_from": request.media.sha256},
            role="media",
            mime=request.media.mime,
        )
        stored = await ctx.write_artifact(
            manifest_file, "other", {"c2pa": True}, role="c2pa_manifest", mime="application/json"
        )
        return SignResult(media=media, manifest=stored, mode="real")

    def _read(self, path: Path) -> VerifyResult:
        import c2pa

        try:
            with c2pa.Reader(str(path)) as reader:
                state = reader.get_validation_state()
                results = reader.get_validation_results() or {}
                store = json.loads(reader.json())
        except Exception as exc:  # no manifest, unsupported container or a corrupt store
            return VerifyResult(
                layer="c2pa", present=False, mode="real", detail={"error": f"{type(exc).__name__}: {exc}"[:300]}
            )
        active = results.get("activeManifest") or {}
        manifest = (store.get("manifests") or {}).get(store.get("active_manifest") or "", {})
        assertions = {str(a.get("label")): a.get("data") for a in manifest.get("assertions", []) if isinstance(a, dict)}
        generation = assertions.get("com.creatorengine.generation") or {}
        return VerifyResult(
            layer="c2pa",
            present=True,
            mode="real",
            state=state,
            success=sorted({str(e.get("code")) for e in active.get("success", [])}),
            failures=sorted({str(e.get("code")) for e in active.get("failure", [])}),
            payload_id=generation.get("watermark_payload_id") if isinstance(generation, dict) else None,
            detail={
                "claim_generator": manifest.get("claim_generator_info"),
                "assertions": {
                    k: v for k, v in assertions.items() if k in ("c2pa.actions.v2", "com.creatorengine.generation")
                },
            },
        )

    async def run_provenance_verify(self, request: VerifyRequest, ctx: RunContext) -> VerifyResult:
        if request.layer != "c2pa":
            raise ValueError(f"{self.manifest.id} verifies the c2pa layer, not {request.layer}")
        return await asyncio.to_thread(self._read, await ctx.read_artifact(request.media))
