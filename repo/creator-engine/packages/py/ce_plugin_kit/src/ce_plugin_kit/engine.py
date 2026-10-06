"""`EngineAdapter`: the adapter base of real model engines (Phase 8, ADR 0051).

An engine adapter is the plugin's contract-facing half: it validates the request, turns the
(translated) request into the engine's native parameters, calls its *backend* for the heavy part
and turns the backend's files into artifacts. The backend is the only object that imports torch,
diffusers or upstream research code, and only when the adapter loads.

That split is what makes GPU adapters testable on a CPU machine: `use_backend()` swaps in a stand-in
that produces media of the right shape with FFmpeg (the manifest's `test_backend`), while the
adapter's own logic — parameter building, chunking, encoding, artifact bookkeeping — runs for real
in the contract suite (§37: "contract tests with heavy calls mocked"). Nothing here claims GPU
validation: that comes only from `scripts/smoke/<plugin>.py` on a GPU host (rule 5).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from ce_contracts import models as m
from ce_contracts.common import Estimate, HardwareInfo, HealthStatus, LoadContext, RunContext
from ce_contracts.interfaces import AdapterBase
from pydantic import BaseModel

__all__ = ["EngineAdapter", "ModelPaths", "cache_layout_path", "media_seconds", "work_units"]

T = TypeVar("T")


def cache_layout_path(
    root: Path | str, source_type: str, repo: str | None, revision: str, uri: str | None, files: Sequence[str] = ()
) -> Path:
    """Where the worker's model cache keeps a source (`ce_worker.model_cache` layout, §25):
    `hf/<repo>@<revision>`, `s3/<bucket/key>`, `url/<host/path>@<revision>`; a subset of a source
    (file patterns) lives in `…~<12 hex of sha256 of the sorted patterns>`."""
    root = Path(root)
    if source_type == "huggingface" and repo:
        base = root / "hf" / f"{repo}@{revision}"
    elif source_type == "s3" and uri:
        base = root / "s3" / uri.removeprefix("s3://")
    elif source_type == "url" and uri:
        base = root / "url" / f"{uri.split('://', 1)[-1].rstrip('/')}@{revision}"
    else:
        return root / "builtin" / (repo or "model")
    if not files:
        return base
    digest = hashlib.sha256("\n".join(sorted(files)).encode("utf-8")).hexdigest()[:12]
    return base.with_name(f"{base.name}~{digest}")


class ModelPaths:
    """Local directories of a plugin's weights: models by `key`, fetchable dependencies by `role`.

    The worker runtime resolves them through the model cache before `load()` and passes them in
    `LoadContext.config["model_paths"]`; without that (a GPU host running a smoke script against a
    pre-filled cache) the cache layout under `model_cache_dir` is assumed."""

    def __init__(self, manifest: Any, ctx: LoadContext) -> None:
        self.manifest = manifest
        self.root = Path(ctx.model_cache_dir)
        self.resolved: dict[str, str] = dict(ctx.config.get("model_paths") or {})

    def model(self, key: str | None = None) -> Path:
        decl = self._model_decl(key)
        if decl.key in self.resolved:
            return Path(self.resolved[decl.key])
        src = decl.source
        return cache_layout_path(self.root, src.type, src.repo, src.revision, src.uri, decl.files)

    def dependency(self, role: str, *, model_key: str | None = None) -> Path:
        decl = self._model_decl(model_key)
        for dep in decl.dependencies:
            if dep.role != role:
                continue
            if role in self.resolved:
                return Path(self.resolved[role])
            if dep.source is None:
                raise KeyError(f"{self.manifest.id}: dependency {role!r} has no source to fetch")
            src = dep.source
            return cache_layout_path(self.root, src.type, src.repo, src.revision, src.uri, dep.files)
        raise KeyError(f"{self.manifest.id}: no dependency with role {role!r}")

    def _model_decl(self, key: str | None) -> Any:
        for decl in self.manifest.models:
            if key is None or decl.key == key:
                return decl
        raise KeyError(f"{self.manifest.id}: no model {key!r}")


def media_seconds(ref: Any, default: float) -> float:
    """Duration recorded on an ArtifactRef (`meta.duration_s`, set by the runtime's media probe), or a default."""
    if ref is None:
        return default
    try:
        value = float((ref.meta or {}).get("duration_s", default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def work_units(request: BaseModel) -> float:
    """The quantity an engine's cost scales with: output (or input) seconds for media, items for
    images and candidates. Used by `estimate()` with the manifest's `pricing.seconds_per_unit`."""
    if isinstance(request, m.VideoGenerateRequest | m.MusicRequest | m.SfxRequest):
        return float(request.duration_s)
    if isinstance(request, m.AvatarRequest):
        return media_seconds(request.audio, 10.0)
    if isinstance(request, m.TTSRequest):
        words = len(request.words) or len(request.text.split())
        return max(1.0, words / max(request.wpm, 1.0) * 60.0)
    if isinstance(request, m.ImageGenerateRequest | m.ImageEditRequest):
        return 1.0
    if isinstance(request, m.VoiceDesignRequest):
        return float(request.count)
    if isinstance(request, m.TranscribeRequest | m.AlignRequest | m.LidRequest | m.AudioAnalysisRequest):
        return media_seconds(request.audio, 10.0)
    if isinstance(request, m.LipSyncRequest | m.UpscaleRequest | m.InterpolateRequest):
        return media_seconds(request.video, 10.0)
    if isinstance(request, m.VisionRequest):
        if request.window_s is not None:
            return max(0.1, request.window_s[1] - request.window_s[0])
        return media_seconds(request.media, 5.0)
    if isinstance(request, m.QCMetricRequest | m.WatermarkRequest | m.MediaAnalysisRequest | m.OcrRequest):
        return media_seconds(request.media, 10.0)
    return 1.0


class EngineAdapter(AdapterBase):
    """Base of real engines. Subclasses implement `create_backend(ctx)` (heavy imports there) and a
    `run_<capability>` handler per declared capability (see `AdapterBase`)."""

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.backend: Any = None
        self._injected: Any = None
        self.paths: ModelPaths | None = None
        self.load_seconds: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def use_backend(self, backend: Any) -> None:
        """Swap in a backend (a CPU stand-in in contract tests); takes effect at the next `load()`."""
        self._injected = backend

    def create_backend(self, ctx: LoadContext) -> Any:  # pragma: no cover - overridden by every engine
        raise NotImplementedError(f"{self.manifest.id} must implement create_backend()")

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.paths = ModelPaths(self.manifest, ctx)
        started = time.monotonic()
        if self._injected is not None:
            self.backend = self._injected
        else:
            self.backend = await asyncio.to_thread(self.create_backend, ctx)
        self.load_seconds = round(time.monotonic() - started, 3)

    async def unload(self) -> None:
        backend, self.backend = self.backend, None
        close = getattr(backend, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
        await super().unload()

    async def health(self) -> HealthStatus:
        if self.backend is None:
            return HealthStatus(ok=False, detail="not loaded")
        probe = getattr(self.backend, "health", None)
        if callable(probe):
            detail = probe()
            if isinstance(detail, HealthStatus):
                return detail
        return HealthStatus(ok=True, detail=type(self.backend).__name__)

    # ------------------------------------------------------------------ helpers for handlers
    @property
    def defaults(self) -> dict[str, Any]:
        """Manifest defaults, overridden by `LoadContext.config["defaults"]` (pool or smoke overrides)."""
        merged = dict(self.manifest.defaults)
        ctx = getattr(self, "load_context", None)
        if ctx is not None:
            merged.update(ctx.config.get("defaults") or {})
        return merged

    def require_backend(self) -> Any:
        if self.backend is None:
            raise RuntimeError(f"{self.manifest.id} is not loaded")
        return self.backend

    async def call(self, ctx: RunContext, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Runs a (blocking) backend call in a thread, honoring cancellation before and after; the
        worker keeps heartbeating meanwhile. Long jobs are split by the adapter (chunks, segments)
        so a cancel takes effect between calls."""
        ctx.cancel.raise_if_cancelled()
        result = await asyncio.to_thread(fn, *args, **kwargs)
        ctx.cancel.raise_if_cancelled()
        return result

    async def start_image(self, ctx: RunContext, request: m.AvatarRequest, work: Path) -> Path:
        """The image a talking clip starts from: the keyframe, or — for a continuation chunk (§25
        checkpoints) — the last frame of the previous chunk."""
        if request.continuation_frames is None:
            return await ctx.read_artifact(request.keyframe)
        from ce_plugin_kit.media import extract_frame, probe

        previous = await ctx.read_artifact(request.continuation_frames)
        if previous.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm"):
            return previous
        info = probe(previous)
        return extract_frame(previous, work / "continuation.png", at_s=max(0.0, info.duration_s - 0.05))

    def scratch(self, ctx: RunContext, name: str) -> Path:
        path = Path(ctx.scratch_dir) / self.manifest.id / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ estimate
    def estimate(self, request: BaseModel, hw: HardwareInfo) -> Estimate:
        """`pricing.load_seconds` (cold model load) + `pricing.seconds_per_unit` × work units, on the
        manifest's reference GPU (`defaults.reference_gpu`). Uncalibrated until completed attempts
        feed the p50/p90 tables (§25); the notes say so."""
        pricing: Mapping[str, float] = self.manifest.pricing
        units = work_units(request)
        seconds = float(pricing.get("seconds_per_unit", 1.0)) * units + float(pricing.get("load_seconds", 0.0))
        vram = float(self.manifest.runtime.min_vram_gb)
        notes = f"manifest estimate for {units:g} unit(s); uncalibrated (validation: {self.manifest.validation})"
        if hw.vram_gb and vram and hw.vram_gb < vram:
            notes += f"; needs {vram:g} GB VRAM, worker has {hw.vram_gb:g} GB"
        return Estimate(seconds=round(max(seconds, 0.1), 3), vram_gb=vram, notes=notes)
