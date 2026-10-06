"""Schemas for every configuration file in `config/` (§8, §35).

Each YAML file type has a Pydantic model; unknown keys are rejected so a typo never silently
falls back to a default. Vocabulary tokens are checked in `ce_config.loader` against the
loaded vocabulary, because these schemas must not hard-code vocabulary (I13, rule 7).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from ce_core.enums import LanguageSupport, Maturity, PluginStatus, RoadmapBucket, RuntimeFamily, ShotType, Validation
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, PositiveFloat, PositiveInt

__all__ = [
    "AppConfig",
    "BehaviorConfig",
    "BehaviorQC",
    "Blocklists",
    "CameraProfile",
    "CaptionStyle",
    "ConsistencyQC",
    "GpuPools",
    "IntentPolicies",
    "Languages",
    "MemoryConfig",
    "MicProfile",
    "Mode",
    "ModelRegistry",
    "OperatorProfile",
    "Platform",
    "Room",
    "RoutingProfile",
    "StrategyPack",
    "TestimonialPolicy",
    "TierQC",
    "WorldQC",
]

Unit = Annotated[float, Field(ge=0.0, le=1.0)]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Aspect = Literal["9:16", "16:9", "1:1", "4:5"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------- default.yaml / env/*.yaml


class PasswordHashConfig(Strict):
    """argon2id parameters (RFC 9106). Production keeps at least the OWASP minimum (19 MiB, t=2)."""

    time_cost: Annotated[int, Field(ge=1)] = 3
    memory_cost_kib: Annotated[int, Field(ge=1024)] = 65_536
    parallelism: Annotated[int, Field(ge=1)] = 4


class SecurityConfig(Strict):
    cookie_secure: bool = True
    session_ttl_s: PositiveInt = 1_209_600
    api_key_prefix: str = "ce_"
    password_min_length: Annotated[int, Field(ge=8)] = 12
    password_hash: PasswordHashConfig = Field(default_factory=PasswordHashConfig)
    login_rate_limit_per_minute: PositiveInt = 10
    invitation_ttl_s: PositiveInt = 604_800
    session_cookie: str = "ce_session"
    csrf_cookie: str = "ce_csrf"


class ApiConfig(Strict):
    default_page_size: PositiveInt = 50
    max_page_size: PositiveInt = 200
    sse_keepalive_s: PositiveInt = 15
    sse_replay_max: PositiveInt = 1000
    # A stream is closed after this long; the browser reconnects with Last-Event-ID, which re-runs
    # authentication, so a revoked session or removed member stops receiving events.
    sse_max_stream_s: PositiveInt = 900


class ProvenanceConfig(Strict):
    mode: Literal["real", "mock_dev"] = "real"
    visible_label_default: Literal["auto", "on", "off"] = "auto"


class FeatureFlags(Strict):
    multi_character_enabled: bool = False
    digital_twins_enabled: bool = False
    autonomous_suggest_enabled: bool = False
    share_links_enabled: bool = False


class SpecConfig(Strict):
    duration_tolerance: Unit = 0.25
    default_wpm: PositiveFloat = 150.0
    require_memory_snapshots: bool = True


class UploadConfig(Strict):
    max_bytes: PositiveInt = 2 * 1024**3
    allowed_media_types: list[str] = Field(default_factory=list)
    ffprobe_timeout_s: PositiveInt = 30
    max_filename_length: PositiveInt = 255


class StorageConfig(Strict):
    presign_ttl_s: PositiveInt = 900


class EventsConfig(Strict):
    stream_maxlen: PositiveInt = 10_000


class ProxyPreset(Strict):
    height: PositiveInt
    fps: PositiveInt
    video_codec: Literal["h264"]
    crf: Annotated[int, Field(ge=0, le=51)]
    audio_bitrate_kbps: PositiveInt


class TimelineConfig(Strict):
    """Dialogue layout on the timeline (§27, `ce_render.timeline`)."""

    lead_s: NonNegativeFloat = 0.2
    segment_gap_s: NonNegativeFloat = 0.25
    tail_s: NonNegativeFloat = 0.6
    shot_pad_in_s: NonNegativeFloat = 0.2
    shot_pad_out_s: NonNegativeFloat = 0.3


class DuckingConfig(Strict):
    speech_pad_s: NonNegativeFloat = 0.1
    attack_s: NonNegativeFloat = 0.08
    release_s: NonNegativeFloat = 0.3


class ScreenConfig(Strict):
    """Screen recordings (§27): `ScreenAnalysisWorkflow` and screen-shot planning."""

    scene_threshold: float = Field(default=10.0, gt=0, le=100, description="FFmpeg scdet score (0–100) of a cut")
    keyframe_settle_s: NonNegativeFloat = 0.4
    keyframe_every_s: PositiveFloat = 8.0
    max_keyframes: PositiveInt = 60
    max_side: PositiveInt = 1280
    dead_time_min_s: PositiveFloat = 3.0
    dead_time_speed: float = Field(default=4.0, gt=1.0, le=8.0)
    zoom_min_s: PositiveFloat = 1.2
    zoom_max_width: float = Field(default=0.9, gt=0, le=1, description="windows wider than this stay unzoomed")
    vlm_sampling_fps: PositiveFloat = 1.0
    bubble_size: float = Field(default=0.28, gt=0, le=0.5)


class BrandLogoConfig(Strict):
    """Where `render.final` places a brand kit's logo when `brand.logo_overlay` is on (Phase 12)."""

    corner: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = "top_right"
    width_ratio: float = Field(default=0.14, gt=0, le=0.5, description="logo width / frame width")
    margin_ratio: float = Field(default=0.04, ge=0, le=0.2, description="margin / frame short side")
    opacity: float = Field(default=0.9, gt=0, le=1)


class RenderConfig(Strict):
    proxy: ProxyPreset
    brand_logo: BrandLogoConfig = Field(default_factory=BrandLogoConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    timeline: TimelineConfig = Field(default_factory=TimelineConfig)
    ducking: DuckingConfig = Field(default_factory=DuckingConfig)
    encode_preset: str = "veryfast"
    true_peak_codec_headroom_db: NonNegativeFloat = Field(
        default=1.5,
        description="the mix targets the spec's true peak minus this headroom: lossy encoding (AAC) raises "
        "inter-sample peaks, and the delivered file must still meet the target (§27)",
    )
    true_peak_retry_bitrates_kbps: list[PositiveInt] = Field(
        default_factory=lambda: [256, 320],
        description="when the encoded delivery still exceeds the true peak, its audio is re-encoded from the "
        "mix at these bitrates in turn, then attenuated as a last resort (Phase 14)",
    )


class PlacementConfig(Strict):
    """Placement scoring weights (§25)."""

    priority: float = 1.0
    age_per_minute: float = 0.5
    resident: float = 20.0
    cached: float = 5.0
    sticky: float = 10.0
    load_penalty_per_gb: float = 0.1
    cost_penalty_per_usd_hour: float = 1.0
    # fair share between organizations (Phase 14, docs/LOAD_TEST.md): each organization's best
    # `per_org_candidates` tasks compete, and an organization loses `fair_share` points per percent
    # of the live leases it holds (at most 50 points: less than the priority range, so priority
    # still decides between unequal tasks). 0 and 0 = one global window (before Phase 14).
    fair_share: NonNegativeFloat = 0.5
    per_org_candidates: Annotated[int, Field(ge=0, le=100)] = 10


class FleetConfig(Strict):
    """The fleet manager (§25, Phase 9): provisioning timeouts, spend projection and holds."""

    provision_timeout_s: PositiveFloat = Field(
        default=900.0, description="a provisioned worker that has not registered by then is terminated"
    )
    spend_horizon_h: PositiveFloat = Field(
        default=1.0, description="projected spend = spent today + the running fleet's burn rate over this horizon"
    )
    low_priority_max: Annotated[int, Field(ge=0, le=100)] = Field(
        default=25, description="queued tasks at or below this priority are held while the daily budget is exceeded"
    )
    alert_cooldown_s: PositiveFloat = Field(
        default=3600.0, description="one budget alert per scope and reason per window"
    )
    worker_env: dict[str, str] = Field(
        default_factory=dict,
        description="extra environment for provisioned workers (no secrets: those come from the provider)",
    )


class SchedulerConfig(Strict):
    """GPU scheduler (§25): leases, heartbeats, retries, loops."""

    heartbeat_s: PositiveFloat = 5.0
    lease_s: PositiveFloat = 30.0
    long_poll_s: NonNegativeFloat = 20.0
    poll_interval_s: PositiveFloat = 0.5
    max_infra_retries: Annotated[int, Field(ge=0, le=10)] = 3
    reaper_interval_s: PositiveFloat = 2.0
    temporal_heartbeat_s: PositiveFloat = 10.0
    fleet_interval_s: PositiveFloat = 15.0
    upload_slots: Annotated[int, Field(ge=1, le=32)] = 6
    stickiness_s: NonNegativeFloat = 120.0
    worker_stale_s: PositiveFloat = 120.0
    # Database-derived operations metrics (`ce_db.ops_metrics`, Phase 14): refresh period and window
    ops_metrics_interval_s: PositiveFloat = 60.0
    ops_metrics_window_s: Annotated[float, Field(ge=60.0, le=7 * 86400.0)] = 3600.0
    placement: PlacementConfig = Field(default_factory=PlacementConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)


class BuildConfig(Strict):
    """Build execution (§12): parallelism, task priority, per-kind timeouts."""

    max_parallel_nodes: Annotated[int, Field(ge=1, le=64)] = 6
    gpu_task_priority: Annotated[int, Field(ge=0, le=100)] = 50
    model_node_timeout_s: PositiveFloat = 7200.0
    cpu_node_timeout_s: PositiveFloat = 600.0
    render_node_timeout_s: PositiveFloat = 1800.0


class ResearchFetchConfig(Strict):
    """The SSRF-guarded fetcher (§33): only http(s) on these ports, resolved addresses checked
    before connecting, with size, time and redirect limits."""

    timeout_s: PositiveFloat = 10.0
    max_bytes: PositiveInt = 2 * 1024**2
    max_redirects: Annotated[int, Field(ge=0, le=10)] = 3
    allowed_ports: list[Annotated[int, Field(ge=1, le=65535)]] = Field(default_factory=lambda: [80, 443])
    allowed_content_types: list[str] = Field(
        default_factory=lambda: ["text/html", "application/xhtml+xml", "text/plain", "text/markdown"]
    )
    user_agent: str = "creator-engine-research/0.1"


class ResearchIngestConfig(Strict):
    """Persistent research sources (Phase 12, `ResearchIngestWorkflow`): fetch limits for documents
    (PDFs included), extraction limits, fact chunking and retrieval weights."""

    fetch: ResearchFetchConfig = Field(
        default_factory=lambda: ResearchFetchConfig(
            max_bytes=20 * 1024**2,
            timeout_s=30.0,
            allowed_content_types=[
                "text/html",
                "application/xhtml+xml",
                "text/plain",
                "text/markdown",
                "application/pdf",
            ],
        )
    )
    max_upload_bytes: PositiveInt = 50 * 1024**2
    max_chars: PositiveInt = 2_000_000
    max_pdf_pages: PositiveInt = 500
    max_archive_bytes: PositiveInt = 50 * 1024**2
    fact_words: Annotated[int, Field(ge=20, le=1000)] = 80
    fact_overlap_words: Annotated[int, Field(ge=0, le=200)] = 15
    max_facts: PositiveInt = 5000
    embed_batch: Annotated[int, Field(ge=1, le=256)] = 32
    vector_weight: Annotated[float, Field(ge=0.0, le=4.0)] = 1.0
    min_cosine: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.2
    min_claim_support: Unit = 0.5  # share of a claim's content terms its evidence must contain


class ResearchConfig(Strict):
    """Director stage 3 (§13): in-memory research over pasted text and fetched URLs (Phase 4) and
    persistent project sources (Phase 12)."""

    ingest: ResearchIngestConfig = Field(default_factory=ResearchIngestConfig)
    fetch: ResearchFetchConfig = Field(default_factory=ResearchFetchConfig)
    max_urls: Annotated[int, Field(ge=0, le=20)] = 5
    max_pasted_chars: PositiveInt = 200_000
    chunk_words: Annotated[int, Field(ge=20, le=2000)] = 120
    chunk_overlap_words: Annotated[int, Field(ge=0, le=500)] = 20
    top_k: Annotated[int, Field(ge=1, le=50)] = 8


class ProsodyStrategyDefaults(Strict):
    """Abstract ProsodyDirective values of a prosody strategy; energy is relative to the DNA baseline."""

    rate: Annotated[float, Field(gt=0.3, lt=3.0)]
    energy_delta: Annotated[float, Field(ge=-1.0, le=1.0)]
    pitch_variation: Unit


class PlannerConfidence(Strict):
    """Model-independent planner confidence of a requested control (§15.6)."""

    base: dict[Literal["emotion", "prosody", "strategy", "event", "annotation"], Unit]
    priority_delta: dict[Literal["must", "should", "nice"], Annotated[float, Field(ge=-0.5, le=0.5)]]
    user_source_bonus: Annotated[float, Field(ge=0.0, le=0.5)] = 0.1
    supported_bonus: Annotated[float, Field(ge=0.0, le=0.5)] = 0.1


class JudgeConfig(Strict):
    """Verdict rules shared by every proxy (§16.3); thresholds per proxy are in observation_proxies.yaml."""

    partial_fraction: Unit = 0.6  # a measure ≥ this fraction of its threshold (or duration) is PARTIAL
    contradiction_ratio: Unit = 0.5  # the opposite behavior over at least this share of the window
    min_face_ratio: Unit = 0.5  # below this share of frames with a face, visual items are NOT_MEASURABLE
    overlay_hidden_ratio: Unit = 0.5  # an overlay covering this share of a window hides the face from the viewer
    relative_tolerance: Annotated[float, Field(ge=0.0, le=0.2)] = 0.03  # measurement band on relative thresholds


class BehaviorConfig(Strict):
    """The behavior engine (`ce_behavior`, §10.6, §15.6–§16): resolution, compilation, judgement."""

    memory_shift_max: Annotated[float, Field(ge=0.0, le=0.5)] = 0.15
    strategy_priority_cap: Literal["must", "should", "nice"] = "should"
    low_salience: dict[str, list[str]] = Field(default_factory=dict)
    planner_confidence: PlannerConfidence
    attention_element_kinds: list[str] = Field(default_factory=lambda: ["screen", "window"])
    prosody_strategies: dict[str, ProsodyStrategyDefaults] = Field(min_length=1)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)


class AppSection(Strict):
    name: str


class BenchmarkConfig(Strict):
    """The benchmark runner and bench-based promotion (§24, Phase 11)."""

    eval_set: str = "eval/cases.yaml"
    seeds: Annotated[int, Field(ge=1, le=8)] = 2
    min_ratings_per_pair: PositiveInt = 1
    min_rated_share: Unit = 1.0  # share of pairs that need their ratings before a verdict
    min_check_pass_rate: Unit = 1.0  # automatic checks (`checks` in the eval cases)
    min_win_rate: Unit = 0.4  # candidate wins + half the ties, over rated pairs, vs the production default
    require_bench_for_promotion: bool = True


class QCAppConfig(Strict):
    """Jobs around the QC gate (Phase 11, §20, §24, §26)."""

    consistency_after_build: bool = True  # ConsistencyWorkflow after a version reaches ready / needs_review
    critique_sampling_fps: Annotated[float, Field(gt=0, le=4)] = 2.0
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)


class EmbeddingsConfig(Strict):
    """`embed.text` use (Phase 12): batching; the dimension is `EMBEDDING_DIM` (settings)."""

    batch_size: Annotated[int, Field(ge=1, le=256)] = 32
    max_chars: PositiveInt = 2000  # longer texts are cut before embedding


class PackagingDesignLimits(Strict):
    """Our own packaging limits for platforms whose limits are not verified (`rules.*` null, D16).
    They are design defaults, never presented as platform facts."""

    title_max_chars: PositiveInt = 100
    description_max_chars: PositiveInt = 2200
    hashtags_max: NonNegativeInt = 5
    hashtag_max_chars: PositiveInt = 30
    cta_max_chars: PositiveInt = 80


class PackagingConfig(Strict):
    """Director stage 12 (§13): per-platform packaging and thumbnails (Phase 12)."""

    design_limits: PackagingDesignLimits = Field(default_factory=PackagingDesignLimits)
    thumbnail_candidates: Annotated[int, Field(ge=1, le=8)] = 3
    thumbnail_text_max_chars: PositiveInt = 40
    max_repairs: Annotated[int, Field(ge=0, le=4)] = 2


class ExportsConfig(Strict):
    """Exports (§30, §32): a packaged render download."""

    require_approved_packaging: bool = True
    require_approved_translations: bool = True  # only approved caption translations are exported


class CaptionTranslationConfig(Strict):
    """`captions.translate` (§27, Phase 12): the `captions_llm` adapter's batching and checks."""

    lines_per_request: Annotated[int, Field(ge=1, le=200)] = 40
    max_line_chars: PositiveInt = 200


class StudioConfig(Strict):
    """Studio jobs (Phase 10, `ce_exec.studio_jobs`): image sizes, the identity-pack expansions,
    plate candidates and the Creator Test."""

    face_size: PositiveInt = 768
    candidates_default: Annotated[int, Field(ge=1, le=16)] = 12
    angles: list[str] = Field(
        default_factory=lambda: ["front", "three_quarter_left", "three_quarter_right", "profile_left", "profile_right"]
    )
    expressions: list[str] = Field(
        default_factory=lambda: ["neutral", "smile", "laugh", "surprised", "serious", "thinking"]
    )
    wardrobe_views: list[str] = Field(default_factory=lambda: ["front", "three_quarter", "full_body"])
    plate_width: PositiveInt = 1080
    plate_height: PositiveInt = 1920
    plate_candidates: Annotated[int, Field(ge=1, le=8)] = 2
    min_vlm_age_estimate: PositiveFloat = Field(
        default=25.0, description="§17.3: an apparent-age estimate below this blocks approval pending review"
    )
    voice_test_text: str = "Okay, here is the thing nobody tells you about this. It actually works."
    creator_test_project: str = Field(default="Creator tests", description="the project that holds test videos")


class AppConfig(Strict):
    """The merged `default.yaml` + `env/<APP_ENV>.yaml`."""

    app: AppSection
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    provenance: ProvenanceConfig = Field(default_factory=ProvenanceConfig)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    spec: SpecConfig = Field(default_factory=SpecConfig)
    uploads: UploadConfig = Field(default_factory=UploadConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    render: RenderConfig
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    behavior: BehaviorConfig
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)
    qc: QCAppConfig = Field(default_factory=lambda: QCAppConfig())
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    packaging: PackagingConfig = Field(default_factory=PackagingConfig)
    exports: ExportsConfig = Field(default_factory=ExportsConfig)
    captions: CaptionTranslationConfig = Field(default_factory=CaptionTranslationConfig)


class EnvOverlay(Strict):
    """`env/<APP_ENV>.yaml`: any subset of AppConfig sections."""

    security: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, Any] = Field(default_factory=dict)
    spec: dict[str, Any] = Field(default_factory=dict)
    uploads: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    events: dict[str, Any] = Field(default_factory=dict)
    render: dict[str, Any] = Field(default_factory=dict)
    scheduler: dict[str, Any] = Field(default_factory=dict)
    build: dict[str, Any] = Field(default_factory=dict)
    behavior: dict[str, Any] = Field(default_factory=dict)
    research: dict[str, Any] = Field(default_factory=dict)
    studio: dict[str, Any] = Field(default_factory=dict)
    embeddings: dict[str, Any] = Field(default_factory=dict)
    packaging: dict[str, Any] = Field(default_factory=dict)
    exports: dict[str, Any] = Field(default_factory=dict)
    captions: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------- languages, memory, intent


class LanguageEntry(Strict):
    label: str
    support: LanguageSupport
    normalizer: str
    aligner_preference: list[str] = Field(min_length=1)
    script: Literal["Latn", "Cyrl", "Arab"]
    rtl: bool = False


class Languages(Strict):
    languages: dict[Annotated[str, Field(pattern=r"^[a-z]{2,3}$")], LanguageEntry]


class RetrievalConfig(Strict):
    budgets: dict[str, NonNegativeInt]
    recency_half_life_days: dict[str, PositiveFloat]
    confidence_weight: Unit
    conflict_precedence: list[Literal["pinned", "authored", "confidence", "recency"]]


class PromotionConfig(Strict):
    min_evidence_count: PositiveInt
    min_confidence: Unit
    repeated_edit_threshold: PositiveInt
    critique_threshold: PositiveInt = 1  # accepted critique proposals → a proposed preference
    allow_mock_evidence: bool = False  # habits measured on mock engines are never promoted unless set
    habit_quantum: Annotated[float, Field(gt=0.0, le=0.5)] = 0.05
    conflict_tolerance: Unit = 0.15  # measured habit vs an authored value before it is a conflict


class ObservationHabit(Strict):
    """A measured feature of a video (ce_qc.consistency features) that is evidence for a habit."""

    feature: str
    kind: str  # a memory kind (`category.kind`)
    field: str  # the unit field of that kind's value
    scale: PositiveFloat = 1.0  # measured / scale, clamped to 0..1


class MemoryEmbeddingConfig(Strict):
    """Embedding use in memory (Phase 12): ranking weight and the hook repetition threshold."""

    retrieval_weight: Annotated[float, Field(ge=0.0, le=2.0)] = 0.5
    hook_max_cosine: Unit = 0.9


class RepetitionWindow(Strict):
    window_videos: PositiveInt
    max_similarity: Unit | None = None
    max_overlap: Unit | None = None
    near_duplicate: Unit | None = None
    ngram: PositiveInt | None = None


class RepetitionConfig(Strict):
    hooks: RepetitionWindow
    phrases: RepetitionWindow
    emotional_arcs: RepetitionWindow
    visual_patterns: RepetitionWindow
    behavior_signatures: RepetitionWindow


class MemoryConfig(Strict):
    retrieval: RetrievalConfig
    promotion: PromotionConfig
    repetition: RepetitionConfig
    embeddings: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    observation_habits: list[ObservationHabit] = Field(default_factory=list)
    update_after_ready: bool = True  # MemoryUpdateWorkflow after a version reaches ready


INTENT_FIELDS = (
    "narrative_goal",
    "emotional_goal",
    "audience_effect",
    "persuasion_goal",
    "information_goal",
    "attention_goal",
    "reveal_strategy",
    "performance_strategy",
    "cta_goal",
)
CHANNELS = ("voice", "acting", "camera", "broll", "editing", "captions", "music", "sfx")
POLICY_POSITIONS = ("first", "middle", "last", "only")  # a scene's position in the video


class IntentRule(Strict):
    id: str
    group: str
    match: dict[str, str] = Field(min_length=1)
    proposals: dict[
        Literal["voice", "acting", "camera", "broll", "editing", "captions", "music", "sfx"], dict[str, Any]
    ]


class IntentPolicies(Strict):
    version: PositiveInt
    precedence: list[str]
    rules: list[IntentRule]


# ---------------------------------------------------------------------- routing, camera, audio


class GenerationBudget(Strict):
    max_height: PositiveInt
    fps: PositiveInt


class RoutingWeights(Strict):
    quality: Unit
    cost: Unit
    latency: Unit
    measured_behavior: Unit


class RoutingProfile(Strict):
    id: str
    label: str
    quality_tier: Literal["draft", "final"]
    allowed_statuses: list[PluginStatus]
    weights: RoutingWeights
    generation: GenerationBudget
    final_preset_height: PositiveInt
    upscale_required: bool
    interpolate: Literal["video.interpolate", "frame_rate_conversion"]
    fallback_chain_depth: Annotated[int, Field(ge=0, le=5)]


class Sensor(Strict):
    fps: PositiveInt
    noise_luma: Unit
    noise_chroma: Unit
    rolling_shutter: Unit


class Lens(Strict):
    focal_equiv_mm: PositiveFloat
    distortion_k1: float
    vignette: Unit
    dof: Literal["deep", "medium", "shallow"]


class Framing(Strict):
    default: str
    headroom: Unit
    eye_line: Unit
    overscan: Annotated[float, Field(ge=0.0, le=0.5)]


class Motion(Strict):
    type: Literal["static", "tripod", "handheld", "gimbal", "walking", "vehicle"]
    amplitude_px: NonNegativeFloat
    freq_hz: tuple[NonNegativeFloat, NonNegativeFloat]
    rotation_deg: NonNegativeFloat
    breathing_sway: bool
    stabilization: Unit


class Focus(Strict):
    autofocus_hunts_per_min: NonNegativeFloat


class Exposure(Strict):
    auto_exposure_drift: Unit


class ColorSpec(Strict):
    lut: str
    saturation: PositiveFloat


class Compression(Strict):
    codec: Literal["h264", "h265", "vp9", "av1"]
    bitrate_kbps: PositiveInt


class CameraAudio(Strict):
    mic_profile: str
    distance_m: PositiveFloat


class CameraProfile(Strict):
    id: str
    label: str
    maturity: Maturity
    prompt_hints: list[str]
    subject_distance_m: PositiveFloat
    sensor: Sensor
    lens: Lens
    framing: Framing
    motion: Motion
    focus: Focus
    exposure: Exposure
    color: ColorSpec
    compression: Compression
    audio: CameraAudio


class EqBand(Strict):
    type: Literal["highpass", "lowpass", "peak", "shelf_low", "shelf_high"]
    freq_hz: PositiveFloat
    gain_db: float | None = None
    q: PositiveFloat | None = None


class MicCompression(Strict):
    threshold_db: float
    ratio: PositiveFloat


class MicProfile(Strict):
    id: str
    label: str
    eq: list[EqBand]
    noise_floor_db: Annotated[float, Field(le=0)]
    distance_m: PositiveFloat
    compression: MicCompression


class Room(Strict):
    id: str
    label: str
    rt60_s: PositiveFloat
    impulse_response: str
    default_ambient: str
    wet_mix: Unit


# ---------------------------------------------------------------------- modes, packs, captions, platforms


class DurationRange(Strict):
    min: PositiveFloat
    max: PositiveFloat
    default: PositiveFloat


class EditGrammar(Strict):
    max_shot_s: PositiveFloat
    cutaway_every_s: NonNegativeFloat
    punch_in_on_emphasis: bool
    beat_snap: bool


class Mode(Strict):
    id: str
    label: str
    maturity: Maturity
    roadmap: RoadmapBucket
    description: str
    allowed_shot_types: list[ShotType] = Field(min_length=1)
    default_camera_profiles: list[str] = Field(min_length=1)
    default_world_kinds: list[str]
    duration_s: DurationRange
    aspects: list[Aspect]
    edit_grammar: EditGrammar
    acting_density: Literal["low", "medium", "high"]
    editorial_methods: list[Literal["editorial_cutaway", "editorial_punch", "caption_emphasis", "music_cue", "sfx_cue"]]
    strategy_packs: list[str]
    requires: list[Literal["product", "screen_asset", "reaction_clip"]]
    prompt_fragment: str | None = None


class StrategyPack(Strict):
    id: str
    label: str
    structure: list[str] = Field(min_length=1)
    signature_format: bool
    hook_count: Annotated[int, Field(ge=1, le=10)]
    guidance: str


class TemplatePurpose(Strict):
    """Template-Director defaults for one scene purpose: scene intent and one acting state."""

    narrative_goal: str
    emotional_goal: str
    audience_effect: str
    information_goal: str
    attention_goal: str
    emotion: str
    intensity: Unit
    internal_state: str
    social_goal: str
    audience_goal: str
    performance_intent: str
    prosody: str
    gaze: str
    gesture: str
    reaction: str
    camera_awareness: str


class TemplateDirector(Strict):
    mode: str
    situation_kind: str
    audience_stance: str
    video_intent: dict[str, str]
    purposes: dict[str, TemplatePurpose]


class HookRange(Strict):
    min: Annotated[int, Field(ge=1, le=10)]
    max: Annotated[int, Field(ge=1, le=10)]


class DirectorMusic(Strict):
    default_mood: str
    tension_mood: str
    resolve_mood: str
    duck_db: Annotated[float, Field(le=0)]


class DirectorConfig(Strict):
    """`config/director.yaml` (§13, §37)."""

    version: PositiveInt
    max_repairs: Annotated[int, Field(ge=0, le=5)]
    temperature: Annotated[float, Field(ge=0, le=2)]
    max_tokens: PositiveInt
    hooks: HookRange
    script_words_tolerance: Unit
    max_pacing_delta: Annotated[float, Field(ge=0, le=0.5)]
    punch_scale: Annotated[float, Field(gt=0.5, lt=3.0)]
    reveal_hold_words: Annotated[int, Field(ge=0, le=10)]
    cta_texts: dict[str, str]
    music: DirectorMusic
    template: TemplateDirector


class CaptionStyle(Strict):
    id: str
    label: str
    font_family: str
    font_fallbacks: list[str] = Field(min_length=1)
    font_size_px: PositiveInt
    primary_color: HexColor
    stroke_color: HexColor
    stroke_width_px: NonNegativeInt
    highlight_color: HexColor
    highlight: Literal["active_word", "phrase", "none"]
    max_words_per_line: PositiveInt
    animation: Literal["none", "pop", "box", "fade"]
    rtl_highlight: Literal["line", "none"]


class SafeZone(Strict):
    top: Unit
    bottom: Unit
    left: Unit
    right: Unit


class PlatformRules(Strict):
    """Platform limits. Null until verified against official documentation (rule 14)."""

    aspects: list[Aspect]
    max_duration_s: PositiveFloat | None = None
    title_max_chars: PositiveInt | None = None
    description_max_chars: PositiveInt | None = None
    hashtags_max: NonNegativeInt | None = None
    caption_safe_zone: SafeZone


class RenderPreset(Strict):
    id: str
    aspect: Aspect
    width: PositiveInt
    height: PositiveInt
    fps: PositiveInt
    video_codec: Literal["h264", "h265", "vp9", "av1"]
    crf: Annotated[int, Field(ge=0, le=51)]
    max_bitrate_kbps: PositiveInt
    audio_codec: Literal["aac", "opus"]
    audio_bitrate_kbps: PositiveInt
    loudness_override: float | None = None


class ChecklistItem(Strict):
    """One disclosure-checklist item of an export (§32 "export checklist per platform")."""

    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    label: str
    required: bool = True


class PlatformPackaging(Strict):
    """Packaging presentation per platform (our design choices; limits live in `rules`)."""

    thumbnail_width: PositiveInt = 1080
    thumbnail_height: PositiveInt = 1920
    hashtag_prefix: str = "#"
    description_hashtags: bool = True  # hashtags appended to the description on export


class Platform(Strict):
    id: str
    label: str
    verified_at: date | None
    sources: list[str] = Field(default_factory=list)
    rules: PlatformRules
    render_presets: list[RenderPreset] = Field(min_length=1)
    packaging: PlatformPackaging = Field(default_factory=PlatformPackaging)
    export_checklist: list[ChecklistItem] = Field(default_factory=list)


# ---------------------------------------------------------------------- QC


class LipsyncCheck(Strict):
    advisory: bool
    by_adapter: dict[str, dict[str, float]]
    av_offset_max_frames: NonNegativeInt


class SpeechCheck(Strict):
    max_wer: dict[str, Unit]
    max_cer: dict[str, Unit] = Field(
        default_factory=lambda: {"default": 0.03},
        description="exact-script loop: a segment passes when WER or CER is within its threshold (§21)",
    )
    by_adapter: dict[str, dict[str, float]]


class MixCheck(Strict):
    integrated_lufs: float
    lufs_tolerance: PositiveFloat
    max_true_peak_dbtp: Annotated[float, Field(le=0)]
    music_under_speech_db: PositiveFloat


class MetricCheck(Strict):
    """A metric check whose thresholds are keyed by metric adapter id (§26): scales differ between
    models, so an adapter without thresholds never gates (its scores are reported as advisory)."""

    advisory: bool = False
    by_adapter: dict[str, dict[str, float]] = Field(default_factory=dict)


class VlmJudgeCheck(Strict):
    critical_defects_fail: bool
    sampling_fps: Annotated[float, Field(gt=0, le=4)] = 2.0
    min_confidence: Unit = 0.5


class TierChecks(Strict):
    lipsync: LipsyncCheck
    identity: dict[str, float]
    speech: SpeechCheck
    visual_quality: MetricCheck = Field(default_factory=MetricCheck)
    mix: MixCheck
    captions: dict[str, bool]
    text_in_broll: dict[str, int]
    vlm_judge: VlmJudgeCheck
    pacing: dict[str, bool]


class QCBudgets(Strict):
    retries_per_node: NonNegativeInt
    retries_per_version: NonNegativeInt
    usd_per_version: NonNegativeFloat


class TierQC(Strict):
    tier: Literal["draft", "final"]
    checks: TierChecks
    budgets: QCBudgets
    ladder: list[Literal["attempt_seed", "fallback_route", "cheaper_fix", "needs_review"]]


class MustPolicy(Strict):
    retry_on: list[str]
    retry_on_unexpected_editorial: list[str]
    warn_on: list[str]
    after_retry: list[Literal["fallback_route_if_measured_better", "needs_review"]]


class ProxyCalibrationConfig(Strict):
    """F1 → reliability class of a calibrated proxy (§16.2); fewer windows than `min_n` stay `low`."""

    high: Unit = 0.85
    medium: Unit = 0.65
    min_n: PositiveInt = 20


class BehaviorQC(Strict):
    version: PositiveInt
    must: MustPolicy
    should: dict[str, str]
    nice: dict[str, str]
    never_fail: list[str]
    low_reliability_proxies: Literal["warn_only"]
    editorial_not_executed: Literal["render_defect"]
    take_ranking_weight: Unit
    unreliable_control_success_rate: Unit
    calibration: ProxyCalibrationConfig = Field(default_factory=lambda: ProxyCalibrationConfig())


class WorldCheck(Strict):
    method: str
    scope: Literal["shot", "video", "creator"]
    reliability: Literal["high", "medium", "low"]
    gate: bool
    window_videos: PositiveInt | None = None


class WorldQC(Strict):
    version: PositiveInt
    checks: dict[str, WorldCheck]
    weights: dict[str, Unit]
    thresholds_by_world_kind: dict[str, dict[str, Unit]]


class ConsistencyMetric(Strict):
    method: str
    gate: bool
    min_similarity: Unit | None = None
    min_score: Unit | None = None
    max_z: PositiveFloat | None = None


class ConsistencyQC(Strict):
    version: PositiveInt
    baseline_window_videos: PositiveInt
    metrics: dict[str, ConsistencyMetric]
    human_rating_sample_rate: Unit


# ---------------------------------------------------------------------- policy, GPU, models


class OperatorProfile(Strict):
    jurisdiction: str
    revenue_band: Literal["lt_1m", "1m_10m", "gte_10m"]
    commercial_use: bool
    distributes_images: bool
    regions_served: list[str]


class Blocklists(Strict):
    version: PositiveInt
    topics: list[str]
    terms: list[str]
    protected_persons: list[str]


class ClassifierConfig(Strict):
    provider: Literal["llm"]
    min_confidence: Unit


class TestimonialPolicy(Strict):
    version: PositiveInt
    require_disclosure_for_dramatization: bool
    require_consent_for_real_quotes: bool
    disclosure_text: str
    classifier: ClassifierConfig
    applies_to_modes: list[str] = Field(default_factory=list)
    dramatization_modes: list[str] = Field(default_factory=list)
    experiential_patterns: dict[str, str] = Field(default_factory=dict)  # rule id → regex (case-insensitive)


class GpuPool(Strict):
    id: str
    gpu_classes: list[str] = Field(min_length=1)
    providers: list[str] = Field(min_length=1)
    families: list[RuntimeFamily] = Field(min_length=1)
    min: NonNegativeInt
    max: NonNegativeInt
    idle_timeout_s: PositiveInt
    target_latency_s: PositiveInt
    spot_ok: bool
    regions: list[str]
    enabled: bool
    autoscale: bool = Field(
        default=True,
        description="the fleet manager provisions and stops workers; false when workers are started outside it "
        "(compose `worker-cpu`, self-managed hosts)",
    )
    idle_action: Literal["terminate", "stop"] = Field(
        default="terminate",
        description="what scale-down does to an idle worker: terminate (no further charges) or stop (keeps its disk)",
    )
    min_driver_version: str | None = Field(
        default=None, description="offers with an older NVIDIA driver are skipped (CUDA 12.8 images need >= 570, §36)"
    )


class GpuClass(Strict):
    vram_gb: NonNegativeFloat = Field(description="0 for CPU-only classes (the `cpu_local` pool of real CPU engines)")


class GpuPools(Strict):
    classes: dict[str, GpuClass] = Field(default_factory=dict)
    pools: list[GpuPool]


class GpuVariant(Strict):
    family: RuntimeFamily
    variant: str = Field(min_length=1)


class GpuVariants(Strict):
    """`config/gpu/variants.yaml` (Phase 9): which family image variant serves each GPU adapter
    (ADR 0052). Generated by `scripts/gen_worker_dockerfiles.py`; the fleet manager provisions the
    variant that serves the adapters with the largest backlog."""

    variants: dict[str, GpuVariant] = Field(default_factory=dict)


LicenseCondition = Literal[
    "revenue_cap_usd",
    "mau_cap",
    "territories_excluded",
    "attribution_text",
    "maas_restricted",
    "non_commercial",
    "copyleft",
    "use_restrictions",
]


class LicenseBlock(Strict):
    """§24 license block. `verified_at`/`verified_by` record who read the LICENSE at the pinned revision."""

    name: str
    url: str
    commercial_use: bool
    conditions: list[dict[LicenseCondition, Any]] = Field(default_factory=list)
    verified_at: date
    verified_by: str
    text_sha256: str | None = None
    evidence: str = ""


class ModelDependency(Strict):
    ref: str
    license: LicenseBlock
    role: str = ""


class ModelSource(Strict):
    type: Literal["huggingface", "s3", "url", "builtin"]
    repo: str | None = None
    revision: str
    uri: str | None = None


class ModelEntry(Strict):
    key: str
    adapter_id: str
    capability: str
    family: RuntimeFamily
    source: ModelSource
    license: LicenseBlock
    dependencies: list[ModelDependency] = Field(default_factory=list)
    status: PluginStatus
    validation: Validation


class ModelRegistry(Strict):
    models: list[ModelEntry]
