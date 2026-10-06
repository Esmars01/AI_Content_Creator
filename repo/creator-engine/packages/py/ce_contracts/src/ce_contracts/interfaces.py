"""The adapter contract (§23): `Adapter`, `BehaviorTranslator` and the typed interfaces.

Every engine is an adapter with `load`, `unload`, `health`, `estimate` and `run`. The typed
interfaces add convenience methods (`VoiceEngine.tts(req, ctx)`, …) that validate the request
model and delegate to `run`, so core code and tests can call engines with types while the worker
runtime calls `run` generically. Core packages never import plugins (I14); they reach adapters
through the plugin registry and the router.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel

from ce_contracts import models as m
from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.capabilities import capability
from ce_contracts.common import Estimate, HardwareInfo, HealthStatus, LoadContext, RunContext

__all__ = [
    "ASREngine",
    "Adapter",
    "AdapterBase",
    "AudioAnalyzer",
    "AvatarEngine",
    "BehaviorTranslator",
    "BodyAnalyzer",
    "CaptionEngine",
    "EffectsEngine",
    "EmbeddingEngine",
    "ExpressionEditor",
    "FaceAnalyzer",
    "FrameInterpolator",
    "IdentityTrainer",
    "ImageEmbedder",
    "ImageGenerator",
    "LLMProvider",
    "LipSyncEngine",
    "MusicEngine",
    "ProvenanceSigner",
    "QCMetric",
    "ResearchEngine",
    "SFXEngine",
    "Upscaler",
    "VideoAnalyzer",
    "VideoGenerator",
    "VisionAnalyzer",
    "VoiceEngine",
    "Watermarker",
]

R = TypeVar("R", bound=BaseModel)


@runtime_checkable
class Adapter(Protocol):
    manifest: Any  # ce_contracts.manifest.PluginManifest (Any avoids an import cycle in the Protocol)

    async def load(self, ctx: LoadContext) -> None: ...

    async def unload(self) -> None: ...

    async def health(self) -> HealthStatus: ...

    def estimate(self, request: BaseModel, hw: HardwareInfo) -> Estimate: ...

    async def run(self, capability: str, request: BaseModel, ctx: RunContext) -> BaseModel: ...


@runtime_checkable
class BehaviorTranslator(Protocol):
    """Shipped by behavior-capable plugins only. The only code that knows engine parameter names (I1).
    Its `version` is part of the route and of the cache key."""

    version: str

    def translate(self, compiled: BehaviorDirectives, base_request: BaseModel) -> BaseModel: ...


class AdapterBase:
    """Convenience base: default lifecycle, request validation and dispatch to `run_<capability>`.

    A subclass implements `async def run_avatar_a2v(self, request, ctx)` (dots become underscores)
    for each capability its manifest declares.
    """

    manifest: Any
    loaded: bool = False
    seconds_per_unit: ClassVar[float] = 1.0

    def __init__(self, manifest: Any) -> None:
        self.manifest = manifest

    async def load(self, ctx: LoadContext) -> None:
        self.load_context = ctx
        self.loaded = True

    async def unload(self) -> None:
        self.loaded = False

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True)

    def estimate(self, request: BaseModel, hw: HardwareInfo) -> Estimate:
        duration = float(getattr(request, "duration_s", 0.0) or 0.0)
        return Estimate(seconds=max(0.5, duration * self.seconds_per_unit), vram_gb=0.0)

    def declares(self, capability_id: str) -> bool:
        return any(c.id == capability_id for c in self.manifest.capabilities)

    async def run(self, capability: str, request: BaseModel, ctx: RunContext) -> BaseModel:
        if not self.declares(capability):
            raise ValueError(f"{self.manifest.id} does not declare {capability}")
        spec = _capability(capability)
        typed = request if isinstance(request, spec.request) else spec.request.model_validate(request)
        handler = getattr(self, "run_" + capability.replace(".", "_"), None)
        if handler is None:
            raise NotImplementedError(f"{self.manifest.id} declares {capability} but has no run_ handler")
        result = await handler(typed, ctx)
        if not isinstance(result, spec.result):
            raise TypeError(
                f"{self.manifest.id}.{capability} returned {type(result).__name__}, not {spec.result.__name__}"
            )
        return cast(BaseModel, result)

    async def _typed(self, capability: str, request: BaseModel, ctx: RunContext, result: type[R]) -> R:
        out = await self.run(capability, request, ctx)
        assert isinstance(out, result)
        return out


def _capability(capability_id: str) -> Any:
    return capability(capability_id)


class LLMProvider(AdapterBase):
    """`llm.structured`; selected by `LLM_PROVIDER`, not by the router (§23)."""

    async def structured(self, request: m.LLMRequest, ctx: RunContext) -> m.LLMResult:
        return await self._typed("llm.structured", request, ctx, m.LLMResult)


class EmbeddingEngine(AdapterBase):
    async def embed_text(self, request: m.TextEmbedRequest, ctx: RunContext) -> m.EmbeddingResult:
        return await self._typed("embed.text", request, ctx, m.EmbeddingResult)


class ResearchEngine(AdapterBase):
    async def fetch(self, request: m.ResearchFetchRequest, ctx: RunContext) -> m.ResearchFetchResult:
        return await self._typed("research.fetch", request, ctx, m.ResearchFetchResult)


class ImageGenerator(AdapterBase):
    async def generate(self, request: m.ImageGenerateRequest, ctx: RunContext) -> m.ImageResult:
        return await self._typed("image.generate", request, ctx, m.ImageResult)

    async def edit(self, request: m.ImageEditRequest, ctx: RunContext) -> m.ImageResult:
        return await self._typed("image.edit", request, ctx, m.ImageResult)


class VideoGenerator(AdapterBase):
    async def t2v(self, request: m.VideoGenerateRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed("video.t2v", request, ctx, m.VideoResult)

    async def i2v(self, request: m.VideoGenerateRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed("video.i2v", request, ctx, m.VideoResult)


class AvatarEngine(AdapterBase):
    async def a2v(self, request: m.AvatarRequest, ctx: RunContext) -> m.AvatarResult:
        return await self._typed("avatar.a2v", request, ctx, m.AvatarResult)


class VoiceEngine(AdapterBase):
    async def tts(self, request: m.TTSRequest, ctx: RunContext) -> m.TTSResult:
        return await self._typed("voice.tts", request, ctx, m.TTSResult)

    async def design(self, request: m.VoiceDesignRequest, ctx: RunContext) -> m.VoiceDesignResult:
        return await self._typed("voice.design", request, ctx, m.VoiceDesignResult)

    async def clone_prepare(self, request: m.VoicePrepareRequest, ctx: RunContext) -> m.VoicePrepareResult:
        return await self._typed("voice.clone_prepare", request, ctx, m.VoicePrepareResult)


class LipSyncEngine(AdapterBase):
    async def dub(self, request: m.LipSyncRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed("lipsync.dub", request, ctx, m.VideoResult)


class ASREngine(AdapterBase):
    async def transcribe(self, request: m.TranscribeRequest, ctx: RunContext) -> m.TranscribeResult:
        return await self._typed("asr.transcribe", request, ctx, m.TranscribeResult)

    async def align(self, request: m.AlignRequest, ctx: RunContext) -> m.AlignResult:
        return await self._typed("asr.align", request, ctx, m.AlignResult)


class MusicEngine(AdapterBase):
    async def music(self, request: m.MusicRequest, ctx: RunContext) -> m.AudioResult:
        return await self._typed("audio.music", request, ctx, m.AudioResult)


class SFXEngine(AdapterBase):
    async def sfx(self, request: m.SfxRequest, ctx: RunContext) -> m.AudioResult:
        return await self._typed("audio.sfx", request, ctx, m.AudioResult)


class VisionAnalyzer(AdapterBase):
    async def image(self, request: m.VisionRequest, ctx: RunContext) -> m.VisionResult:
        return await self._typed("vision.image", request, ctx, m.VisionResult)


class VideoAnalyzer(AdapterBase):
    async def video(self, request: m.VisionRequest, ctx: RunContext) -> m.VisionResult:
        return await self._typed("vision.video", request, ctx, m.VisionResult)


class FaceAnalyzer(AdapterBase):
    async def face_landmarks(self, request: m.MediaAnalysisRequest, ctx: RunContext) -> m.FaceLandmarksResult:
        return await self._typed("face.landmarks", request, ctx, m.FaceLandmarksResult)


class BodyAnalyzer(AdapterBase):
    async def body_landmarks(self, request: m.MediaAnalysisRequest, ctx: RunContext) -> m.BodyLandmarksResult:
        return await self._typed("body.landmarks", request, ctx, m.BodyLandmarksResult)


class AudioAnalyzer(AdapterBase):
    async def prosody(self, request: m.AudioAnalysisRequest, ctx: RunContext) -> m.ProsodyFeaturesResult:
        return await self._typed("audio.prosody", request, ctx, m.ProsodyFeaturesResult)


class ImageEmbedder(AdapterBase):
    async def embed(self, request: m.ImageEmbedRequest, ctx: RunContext) -> m.EmbeddingResult:
        return await self._typed("image.embed", request, ctx, m.EmbeddingResult)


class Upscaler(AdapterBase):
    async def upscale(self, request: m.UpscaleRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed("video.upscale", request, ctx, m.VideoResult)


class FrameInterpolator(AdapterBase):
    async def interpolate(self, request: m.InterpolateRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed("video.interpolate", request, ctx, m.VideoResult)


class QCMetric(AdapterBase):
    async def measure(self, capability: str, request: m.QCMetricRequest, ctx: RunContext) -> m.QCMetricResult:
        return await self._typed(capability, request, ctx, m.QCMetricResult)


class CaptionEngine(AdapterBase):
    async def build(self, request: m.CaptionBuildRequest, ctx: RunContext) -> m.CaptionResult:
        return await self._typed("captions.build", request, ctx, m.CaptionResult)


class EffectsEngine(AdapterBase):
    async def effect(self, capability: str, request: m.EffectRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed(capability, request, ctx, m.VideoResult)


class ExpressionEditor(AdapterBase):
    async def edit(self, request: m.ExpressionEditRequest, ctx: RunContext) -> m.VideoResult:
        return await self._typed("expression.edit", request, ctx, m.VideoResult)


class IdentityTrainer(AdapterBase):
    async def train(self, request: m.IdentityTrainRequest, ctx: RunContext) -> m.IdentityTrainResult:
        return await self._typed("identity.train", request, ctx, m.IdentityTrainResult)


class Watermarker(AdapterBase):
    async def watermark(self, capability: str, request: m.WatermarkRequest, ctx: RunContext) -> m.WatermarkResult:
        return await self._typed(capability, request, ctx, m.WatermarkResult)


class ProvenanceSigner(AdapterBase):
    async def sign(self, request: m.SignRequest, ctx: RunContext) -> m.SignResult:
        return await self._typed("provenance.sign", request, ctx, m.SignResult)

    async def verify(self, request: m.VerifyRequest, ctx: RunContext) -> m.VerifyResult:
        return await self._typed("provenance.verify", request, ctx, m.VerifyResult)
