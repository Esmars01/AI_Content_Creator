"""The capability catalogue (§23): every capability id with its interface, request and result
models and its closed feature vocabulary.

Features describe input/output modes only. Behavior abilities and engine properties (segment
control, multi-person, pose guidance, chunk continuation) are declared only in the behavior
matrix; manifest validation rejects them as features (§23).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from ce_contracts import models as m

__all__ = [
    "BEHAVIOR_ABILITY_NAMES",
    "CAPABILITIES",
    "INTERFACE_ONLY",
    "Capability",
    "capability",
]

# Names that belong in the behavior matrix, never in `features` (§23).
BEHAVIOR_ABILITY_NAMES = frozenset(
    {
        "segment_control",
        "native_segment",
        "multi_person",
        "pose_guided",
        "pose_guidance",
        "chunk_continuation",
        "gaze_control",
        "emotion_control",
        "expression_control",
        "prosody_coupling",
        "temporal_control",
        "object_interaction",
    }
)


@dataclass(frozen=True)
class Capability:
    id: str
    interface: str
    request: type[BaseModel]
    result: type[BaseModel]
    features: frozenset[str] = field(default_factory=frozenset)
    routed: bool = True  # False: selected by configuration, not by the router (llm.structured)
    behavior_capable: bool = False  # True: requests may carry behavior directives for a translator


def _c(
    id: str,
    interface: str,
    request: type[BaseModel],
    result: type[BaseModel],
    features: tuple[str, ...] = (),
    *,
    routed: bool = True,
    behavior: bool = False,
) -> Capability:
    return Capability(id, interface, request, result, frozenset(features), routed, behavior)


_ALL = [
    _c("llm.structured", "LLMProvider", m.LLMRequest, m.LLMResult, ("json_schema", "tool_use"), routed=False),
    _c("embed.text", "EmbeddingEngine", m.TextEmbedRequest, m.EmbeddingResult, ("multilingual",)),
    _c("research.fetch", "ResearchEngine", m.ResearchFetchRequest, m.ResearchFetchResult, ("html", "pdf")),
    _c("research.ingest", "ResearchEngine", m.ResearchIngestRequest, m.ResearchIngestResult, ("pdf", "docx", "text")),
    _c("research.search", "ResearchEngine", m.ResearchSearchRequest, m.ResearchSearchResult, ("keyword", "vector")),
    _c("image.generate", "ImageGenerator", m.ImageGenerateRequest, m.ImageResult, ("t2i", "reference", "lora")),
    _c("image.edit", "ImageGenerator", m.ImageEditRequest, m.ImageResult, ("instruct", "inpaint", "multi_reference")),
    _c("video.t2v", "VideoGenerator", m.VideoGenerateRequest, m.VideoResult, ("t2v",)),
    _c("video.i2v", "VideoGenerator", m.VideoGenerateRequest, m.VideoResult, ("i2v", "first_last_frame")),
    _c("video.r2v", "VideoGenerator", m.VideoGenerateRequest, m.VideoResult, ("r2v",)),
    _c("video.edit", "VideoGenerator", m.VideoGenerateRequest, m.VideoResult, ("mask_edit", "restyle")),
    _c("video.extend", "VideoGenerator", m.VideoGenerateRequest, m.VideoResult, ("extend",)),
    _c("video.joint_av", "VideoGenerator", m.VideoGenerateRequest, m.VideoResult, ("joint_av",)),
    _c(
        "avatar.a2v",
        "AvatarEngine",
        m.AvatarRequest,
        m.AvatarResult,
        ("i2v", "v2v_dub", "long_form_streaming"),
        behavior=True,
    ),
    _c(
        "voice.tts",
        "VoiceEngine",
        m.TTSRequest,
        m.TTSResult,
        ("inline_tags", "instruction", "word_timestamps", "streaming"),
        behavior=True,
    ),
    _c("voice.design", "VoiceEngine", m.VoiceDesignRequest, m.VoiceDesignResult, ("description",)),
    _c("voice.clone_prepare", "VoiceEngine", m.VoicePrepareRequest, m.VoicePrepareResult, ("zero_shot", "preset")),
    _c("voice.convert", "VoiceEngine", m.VoiceConvertRequest, m.AudioResult, ("any_to_any",)),
    _c("lipsync.dub", "LipSyncEngine", m.LipSyncRequest, m.VideoResult, ("region_patch",)),
    _c("asr.transcribe", "ASREngine", m.TranscribeRequest, m.TranscribeResult, ("word_timestamps",)),
    _c("asr.align", "ASREngine", m.AlignRequest, m.AlignResult, ("word_level", "char_level")),
    _c("asr.lid", "ASREngine", m.LidRequest, m.LidResult, ("lid",)),
    _c("audio.music", "MusicEngine", m.MusicRequest, m.AudioResult, ("instrumental", "bpm")),
    _c("audio.sfx", "SFXEngine", m.SfxRequest, m.AudioResult, ("ambience", "one_shot")),
    _c("vision.image", "VisionAnalyzer", m.VisionRequest, m.VisionResult, ("structured",)),
    _c("vision.video", "VideoAnalyzer", m.VisionRequest, m.VisionResult, ("structured", "windowed")),
    _c("vision.ocr", "VisionAnalyzer", m.OcrRequest, m.OcrResult, ("boxes",)),
    _c("face.detect", "FaceAnalyzer", m.MediaAnalysisRequest, m.FaceDetectResult, ("boxes",)),
    _c("face.landmarks", "FaceAnalyzer", m.MediaAnalysisRequest, m.FaceLandmarksResult, ("blendshapes", "iris")),
    _c("face.embed", "FaceAnalyzer", m.MediaAnalysisRequest, m.EmbeddingResult, ("identity",)),
    _c("body.landmarks", "BodyAnalyzer", m.MediaAnalysisRequest, m.BodyLandmarksResult, ("pose", "hands")),
    _c("audio.prosody", "AudioAnalyzer", m.AudioAnalysisRequest, m.ProsodyFeaturesResult, ("pitch", "energy")),
    _c("audio.emotion", "AudioAnalyzer", m.AudioAnalysisRequest, m.AudioEmotionResult, ("classes",)),
    _c("voice.embed", "AudioAnalyzer", m.AudioAnalysisRequest, m.EmbeddingResult, ("speaker",)),
    _c("image.embed", "ImageEmbedder", m.ImageEmbedRequest, m.EmbeddingResult, ("masked",)),
    _c("video.upscale", "Upscaler", m.UpscaleRequest, m.VideoResult, ("2x", "4x")),
    _c("video.interpolate", "FrameInterpolator", m.InterpolateRequest, m.VideoResult, ("fps_convert",)),
    _c("qc.lipsync", "QCMetric", m.QCMetricRequest, m.QCMetricResult, ("confidence", "offset")),
    _c("qc.vqa", "QCMetric", m.QCMetricRequest, m.QCMetricResult, ("no_reference",)),
    _c("qc.speech_quality", "QCMetric", m.QCMetricRequest, m.QCMetricResult, ("mos",)),
    _c("captions.build", "CaptionEngine", m.CaptionBuildRequest, m.CaptionResult, ("ass", "srt", "vtt", "rtl")),
    _c("captions.translate", "CaptionEngine", m.CaptionTranslateRequest, m.CaptionResult, ("review_state",)),
    _c("effects.transition", "EffectsEngine", m.EffectRequest, m.VideoResult, ("crossfade", "whip")),
    _c("effects.title", "EffectsEngine", m.EffectRequest, m.VideoResult, ("text",)),
    _c("effects.overlay", "EffectsEngine", m.EffectRequest, m.VideoResult, ("logo", "lower_third")),
    _c("effects.disclosure", "EffectsEngine", m.EffectRequest, m.VideoResult, ("text",)),
    _c("expression.edit", "ExpressionEditor", m.ExpressionEditRequest, m.VideoResult, ("blink", "glance", "smile")),
    _c("identity.train", "IdentityTrainer", m.IdentityTrainRequest, m.IdentityTrainResult, ("lora",)),
    _c("provenance.watermark_video", "Watermarker", m.WatermarkRequest, m.WatermarkResult, ("invisible",)),
    _c("provenance.watermark_audio", "Watermarker", m.WatermarkRequest, m.WatermarkResult, ("invisible",)),
    _c("provenance.sign", "ProvenanceSigner", m.SignRequest, m.SignResult, ("c2pa",)),
    # Not routed: `GET /v1/renders/{id}/verify` asks the adapters a render's manifest recorded.
    _c("provenance.verify", "ProvenanceSigner", m.VerifyRequest, m.VerifyResult, ("c2pa", "watermark"), routed=False),
]

CAPABILITIES: dict[str, Capability] = {c.id: c for c in _ALL}

# Interfaces only in the MVP; their node kinds arrive with the roadmap items that use them (§23).
INTERFACE_ONLY = frozenset({"video.r2v", "video.edit", "video.extend", "video.joint_av", "voice.convert"})


def capability(capability_id: str) -> Capability:
    try:
        return CAPABILITIES[capability_id]
    except KeyError:
        raise KeyError(f"unknown capability {capability_id!r}") from None
