"""Request and result models for every capability of §23.

Engines receive typed requests and return typed results; media travel as `ArtifactRef`s that the
runtime resolves to local files (`RunContext.read_artifact`). `engine` holds what a
`BehaviorTranslator` produced (native prompt text, parameters, segment JSON); only the plugin
reads it. `labels` is display context (creator, wardrobe, world, camera position) that mock
engines burn into their output; real engines must not treat it as instructions (I10).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.common import ArtifactRef, ContractModel

__all__ = [
    "AlignRequest",
    "AlignResult",
    "AlignedWord",
    "AudioAnalysisRequest",
    "AudioEmotionResult",
    "AudioResult",
    "AvatarRequest",
    "AvatarResult",
    "BodyLandmarksResult",
    "CaptionBuildRequest",
    "CaptionResult",
    "CaptionTranslateRequest",
    "CaptionWord",
    "EffectRequest",
    "EmbeddingResult",
    "ExpressionEditRequest",
    "FaceDetectResult",
    "FaceLandmarksResult",
    "IdentityTrainRequest",
    "IdentityTrainResult",
    "ImageEditRequest",
    "ImageEmbedRequest",
    "ImageGenerateRequest",
    "ImageResult",
    "InterpolateRequest",
    "LLMMessage",
    "LLMRequest",
    "LLMResult",
    "LidRequest",
    "LidResult",
    "LipSyncRequest",
    "MediaAnalysisRequest",
    "MusicRequest",
    "OcrBox",
    "OcrFrame",
    "OcrRequest",
    "OcrResult",
    "ProsodyFeaturesResult",
    "QCMetricRequest",
    "QCMetricResult",
    "ResearchFetchRequest",
    "ResearchFetchResult",
    "ResearchIngestRequest",
    "ResearchIngestResult",
    "ResearchSearchRequest",
    "ResearchSearchResult",
    "SfxRequest",
    "SignRequest",
    "SignResult",
    "TTSRequest",
    "TTSResult",
    "TextEmbedRequest",
    "TrackEventOut",
    "TranscribeRequest",
    "TranscribeResult",
    "UpscaleRequest",
    "VerifyRequest",
    "VerifyResult",
    "VideoGenerateRequest",
    "VideoResult",
    "VisionRequest",
    "VisionResult",
    "VoiceConditioning",
    "VoiceConvertRequest",
    "VoiceDesignRequest",
    "VoiceDesignResult",
    "VoicePrepareRequest",
    "VoicePrepareResult",
    "VoiceReference",
    "WatermarkRequest",
    "WatermarkResult",
]


class _Request(ContractModel):
    labels: dict[str, str] = Field(default_factory=dict)
    engine: dict[str, Any] = Field(default_factory=dict, description="translator output; plugin-private")


# ---------------------------------------------------------------------- LLM, embeddings, research


class LLMMessage(ContractModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(ContractModel):
    messages: list[LLMMessage]
    json_schema: dict[str, Any]
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=4096, gt=0)
    stage: str = ""
    scenario_id: str | None = None


class LLMResult(ContractModel):
    output: dict[str, Any]
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0


class TextEmbedRequest(_Request):
    texts: list[str] = Field(min_length=1)
    language: str | None = None


class EmbeddingResult(ContractModel):
    vectors: list[list[float]]
    dim: int


class ResearchFetchRequest(_Request):
    url: str


class ResearchFetchResult(ContractModel):
    document: ArtifactRef
    title: str = ""
    content_type: str = ""


class ResearchIngestRequest(_Request):
    document: ArtifactRef
    chunk_chars: int = Field(default=1200, gt=0)


class ResearchIngestResult(ContractModel):
    chunks: list[str]


class ResearchSearchRequest(_Request):
    query: str
    k: int = Field(default=5, gt=0)


class ResearchSearchResult(ContractModel):
    hits: list[dict[str, Any]]


# ---------------------------------------------------------------------- image, video, avatar


class ImageResult(ContractModel):
    image: ArtifactRef
    width: int
    height: int


class ImageGenerateRequest(_Request):
    prompt: str
    negative: str = ""
    references: list[ArtifactRef] = Field(default_factory=list)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    steps: int | None = None
    loras: list[str] = Field(default_factory=list)


class ImageEditRequest(_Request):
    image: ArtifactRef
    prompt: str
    negative: str = ""
    references: list[ArtifactRef] = Field(default_factory=list)
    mask: ArtifactRef | None = None
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class VideoResult(ContractModel):
    video: ArtifactRef
    duration_s: float
    fps: float
    width: int
    height: int
    audio: ArtifactRef | None = None


class VideoGenerateRequest(_Request):
    prompt: str
    negative: str = ""
    first_frame: ArtifactRef | None = None
    last_frame: ArtifactRef | None = None
    references: list[ArtifactRef] = Field(default_factory=list)
    source_video: ArtifactRef | None = None
    mask: ArtifactRef | None = None
    duration_s: float = Field(gt=0)
    fps: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class AvatarRequest(_Request):
    """`avatar.a2v`: animate the keyframe with the audio. `behavior` carries the compiled
    directives with times relative to the audio; the translator renders them into `engine`."""

    keyframe: ArtifactRef
    audio: ArtifactRef
    behavior: BehaviorDirectives | None = None
    prompt: str = Field(default="", description="model-agnostic camera/scene hints (camera profile prompt_hints)")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    continuation_frames: ArtifactRef | None = None
    chunk_index: int = Field(default=1, ge=1)


class AvatarResult(VideoResult):
    behavior_track: ArtifactRef | None = Field(
        default=None, description="ground truth of what the engine performed (mock engines only, §16.7)"
    )


class LipSyncRequest(_Request):
    video: ArtifactRef
    audio: ArtifactRef
    face_region: tuple[float, float, float, float] | None = None


class ExpressionEditRequest(_Request):
    video: ArtifactRef
    landmark_tracks: ArtifactRef | None = None
    operations: list[dict[str, Any]] = Field(default_factory=list)


class UpscaleRequest(_Request):
    video: ArtifactRef
    target_width: int = Field(gt=0)
    target_height: int = Field(gt=0)


class InterpolateRequest(_Request):
    video: ArtifactRef
    target_fps: float = Field(gt=0)


class IdentityTrainRequest(_Request):
    appearance_version_id: str
    dataset: list[ArtifactRef] = Field(min_length=1)
    base_model: str


class IdentityTrainResult(ContractModel):
    lora: ArtifactRef
    steps: int = 0


# ---------------------------------------------------------------------- voice, ASR, audio


class VoiceReference(ContractModel):
    audio: ArtifactRef
    transcript: str
    language: str


class VoiceConditioning(ContractModel):
    conditioning: ArtifactRef | None = Field(default=None, description="output of voice.clone_prepare (voice.prepare)")
    description: str = ""
    lexicon: dict[str, str] = Field(default_factory=dict)


class TTSRequest(_Request):
    """`voice.tts`. `behavior` carries the compiled ProsodyPlan (one `prosody` entry for this
    segment); the voice plugin's translator renders it into `engine`."""

    text: str = Field(min_length=1)
    words: list[str] = Field(default_factory=list, description="the canonical tokenizer's words of `text` (§11)")
    language: str
    voice: VoiceConditioning = Field(default_factory=VoiceConditioning)
    behavior: BehaviorDirectives | None = None
    sample_rate: int = 48_000
    wpm: float = Field(default=150.0, gt=0)
    spoken: list[str] = Field(
        default_factory=list,
        description="the language normalizer's spoken form of each canonical word (numbers, dates, abbreviations, "
        "lexicon) with its punctuation, same length as `words`; empty = speak `text` (§21)",
    )


class AlignedWord(ContractModel):
    index: int = Field(ge=0)
    word: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)


class TTSResult(ContractModel):
    audio: ArtifactRef
    duration_s: float
    sample_rate: int
    word_timings: list[AlignedWord] | None = None


class VoiceDesignRequest(_Request):
    description: str
    language: str
    sample_text: str
    count: int = Field(default=4, ge=1, le=16)


class VoiceDesignResult(ContractModel):
    candidates: list[ArtifactRef]


class VoicePrepareRequest(_Request):
    references: list[VoiceReference] = Field(default_factory=list)
    description: str = ""


class VoicePrepareResult(ContractModel):
    conditioning: ArtifactRef


class VoiceConvertRequest(_Request):
    audio: ArtifactRef
    target: VoiceConditioning


class AudioResult(ContractModel):
    audio: ArtifactRef
    duration_s: float
    sample_rate: int


class TranscribeRequest(_Request):
    audio: ArtifactRef
    language: str | None = None
    hint_text: str | None = Field(default=None, description="known text, used only by mock engines")


class TranscribeResult(ContractModel):
    text: str
    words: list[AlignedWord] = Field(default_factory=list)
    language: str


class AlignRequest(_Request):
    audio: ArtifactRef
    text: str
    words: list[str] = Field(default_factory=list, description="the canonical tokenizer's words; indices match anchors")
    language: str
    spoken_words: list[str] = Field(
        default_factory=list, description="comparison words of the normalized script (what an ASR should hear)"
    )
    spoken_sources: list[int] = Field(
        default_factory=list, description="for each spoken word, the index of the canonical word it comes from"
    )


class AlignResult(ContractModel):
    words: list[AlignedWord]
    precision: Literal["fine", "coarse"] = "fine"


class LidRequest(_Request):
    audio: ArtifactRef


class LidResult(ContractModel):
    language: str
    confidence: float = Field(ge=0, le=1)


class MusicRequest(_Request):
    description: str
    duration_s: float = Field(gt=0)
    bpm: int | None = None
    instrumental: bool = True


class SfxRequest(_Request):
    description: str
    duration_s: float = Field(gt=0)


# ---------------------------------------------------------------------- analysis


class MediaAnalysisRequest(_Request):
    """`face.*`, `body.landmarks`, `image.embed` (single medium). `sidecars` lets mock analyzers
    read ground-truth tracks written by mock engines."""

    media: ArtifactRef
    sample_hz: float = Field(default=5.0, gt=0)
    sidecars: list[ArtifactRef] = Field(default_factory=list)
    character_key: str | None = None


class TrackEventOut(ContractModel):
    type: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    value: float | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    item_ref: str | None = None


class FaceDetectResult(ContractModel):
    frames: list[dict[str, Any]] = Field(default_factory=list, description="[{t_s, boxes: [[x, y, w, h, score]]}]")


class FaceLandmarksResult(ContractModel):
    sample_hz: float
    series: dict[str, list[float]] = Field(default_factory=dict)
    events: list[TrackEventOut] = Field(default_factory=list)
    face_detected_ratio: float = Field(default=0.0, ge=0, le=1)


class BodyLandmarksResult(ContractModel):
    sample_hz: float
    series: dict[str, list[float]] = Field(default_factory=dict)
    events: list[TrackEventOut] = Field(default_factory=list)
    body_detected_ratio: float = Field(default=0.0, ge=0, le=1)


class AudioAnalysisRequest(_Request):
    audio: ArtifactRef
    word_timings: list[AlignedWord] = Field(default_factory=list)


class ProsodyFeaturesResult(ContractModel):
    sample_hz: float
    series: dict[str, list[float]] = Field(default_factory=dict)
    speech_rate_wpm: float | None = None
    pauses: list[TrackEventOut] = Field(default_factory=list)


class AudioEmotionResult(ContractModel):
    classes: dict[str, float]


class ImageEmbedRequest(_Request):
    images: list[ArtifactRef] = Field(min_length=1)
    masks: list[ArtifactRef] = Field(default_factory=list)


class VisionRequest(_Request):
    media: ArtifactRef
    question: str
    json_schema: dict[str, Any] = Field(default_factory=dict)
    sampling_fps: float = Field(default=2.0, gt=0)
    window_s: tuple[float, float] | None = None


class VisionResult(ContractModel):
    answer: dict[str, Any]
    confidence: float = Field(default=0.0, ge=0, le=1)


class OcrRequest(_Request):
    media: ArtifactRef
    sampling_fps: float = Field(default=1.0, gt=0)


class OcrBox(ContractModel):
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0, le=1)


class OcrFrame(ContractModel):
    t_s: float = Field(ge=0)
    boxes: list[OcrBox] = Field(default_factory=list)


class OcrResult(ContractModel):
    frames: list[OcrFrame] = Field(default_factory=list)


class QCMetricRequest(_Request):
    media: ArtifactRef
    reference: ArtifactRef | None = None
    text: str | None = None


class QCMetricResult(ContractModel):
    metric: str
    score: float
    metrics: dict[str, float] = Field(default_factory=dict)
    passed: bool | None = None
    advisory: bool = False


# ---------------------------------------------------------------------- captions, effects, provenance


class CaptionWord(ContractModel):
    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    emphasis: bool = False


class CaptionBuildRequest(_Request):
    words: list[CaptionWord]
    language: str
    rtl: bool = False
    style: dict[str, Any] = Field(description="a config/caption_styles entry")
    safe_zone: dict[str, float] = Field(default_factory=dict, description="top, bottom, left, right fractions")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    max_words_per_line: int = Field(default=3, gt=0)
    highlight: Literal["active_word", "phrase", "none"] = "active_word"
    placement: str = "platform_safe_zone"


class CaptionResult(ContractModel):
    ass: ArtifactRef
    srt: ArtifactRef | None = None
    vtt: ArtifactRef | None = None


class CaptionTranslateRequest(_Request):
    captions: ArtifactRef
    source_language: str
    target_language: str


class EffectRequest(_Request):
    effect: dict[str, Any]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_s: float = Field(gt=0)
    fps: float = Field(gt=0)
    text: str = ""


class WatermarkRequest(_Request):
    media: ArtifactRef
    payload_id: str


class WatermarkResult(ContractModel):
    media: ArtifactRef
    payload_id: str
    mode: Literal["real", "mock_dev"]


class SignRequest(_Request):
    media: ArtifactRef
    manifest: dict[str, Any]


class SignResult(ContractModel):
    media: ArtifactRef
    manifest: ArtifactRef
    mode: Literal["real", "mock_dev"]


class VerifyRequest(_Request):
    """`provenance.verify` (not routed): check one provenance layer of a delivered file — the C2PA
    manifest (signature, hashes, trust) or an invisible watermark (detection, payload)."""

    media: ArtifactRef
    layer: Literal["c2pa", "watermark_video", "watermark_audio"]
    payload_id: str | None = None


class VerifyResult(ContractModel):
    layer: str
    present: bool
    mode: Literal["real", "mock_dev"]
    state: str | None = None
    success: list[str] = Field(default_factory=list, description="validator success codes (C2PA)")
    failures: list[str] = Field(default_factory=list, description="validator failure codes (C2PA)")
    payload_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
