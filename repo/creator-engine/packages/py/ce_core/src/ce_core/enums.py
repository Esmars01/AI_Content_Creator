"""Closed enumerations that are part of the schema itself (§10.4 and the sections cited).

Vocabulary-driven values (emotions, strategies, intent goals, event types, lock groups…) are
NOT enums in code: they live in `config/vocab/` and are validated against the declared
`vocab_version` (I13, rule 7). Only structural, rarely-changing sets are enums here.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "VERSION_STATE_TRANSITIONS",
    "AnnotationType",
    "ArtifactKind",
    "CameraPositionStatus",
    "CastRole",
    "ConflictState",
    "CoverageLevel",
    "CreatorKind",
    "DerivedFromKind",
    "ElementMutability",
    "ElementSource",
    "InputMode",
    "JobKind",
    "LanguageSupport",
    "Maturity",
    "MemorySourceType",
    "MemoryStatus",
    "ObservationVerdict",
    "Outcome",
    "PluginStatus",
    "Priority",
    "ProfileSource",
    "PromotionBasis",
    "QualityTier",
    "RealizationMethod",
    "RecordStatus",
    "ReliabilityClass",
    "RoadmapBucket",
    "RuntimeFamily",
    "ShotLayer",
    "ShotType",
    "TemporalPrecision",
    "TimeOfDay",
    "Validation",
    "VersionFlag",
    "VersionOrigin",
    "VersionState",
    "Weather",
]


class PluginStatus(StrEnum):
    """Plugin/model `status` (§10.4). Routing uses production normally, sandbox only in sandbox runs."""

    PRODUCTION = "production"
    SANDBOX = "sandbox"
    EXPERIMENTAL = "experimental"
    RESEARCH_ONLY = "research_only"
    DISABLED = "disabled"


class Validation(StrEnum):
    """Plugin/model `validation` evidence (§10.4)."""

    UNTESTED_ON_GPU = "untested_on_gpu"
    SMOKE_PASSED = "smoke_passed"
    BENCH_PASSED = "bench_passed"
    FAILED = "failed"


class PromotionBasis(StrEnum):
    SMOKE = "smoke"
    BENCH = "bench"


class Maturity(StrEnum):
    """Design maturity of modes, camera profiles and features (§10.4)."""

    PRODUCTION = "production"
    BETA = "beta"
    EXPERIMENTAL = "experimental"


class LanguageSupport(StrEnum):
    PRODUCTION = "production"
    BETA = "beta"
    UNSUPPORTED = "unsupported"


class RoadmapBucket(StrEnum):
    MVP = "MVP"
    V1 = "V1"
    V2 = "V2"
    EXPERIMENTAL = "Experimental"


class RecordStatus(StrEnum):
    """Lifecycle of every versioned identity record (§10.2)."""

    DRAFT = "draft"
    APPROVED = "approved"
    ARCHIVED = "archived"


class VersionState(StrEnum):
    """Video version state machine (§12.8)."""

    PLANNING = "planning"
    PLANNED = "planned"
    PREVIZ_RUNNING = "previz_running"
    PREVIZ_READY = "previz_ready"
    APPROVED = "approved"
    GENERATING = "generating"
    READY = "ready"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


VERSION_STATE_TRANSITIONS: dict[VersionState, frozenset[VersionState]] = {
    VersionState.PLANNING: frozenset({VersionState.PLANNED, VersionState.FAILED, VersionState.CANCELLED}),
    VersionState.PLANNED: frozenset({VersionState.PREVIZ_RUNNING, VersionState.FAILED, VersionState.CANCELLED}),
    VersionState.PREVIZ_RUNNING: frozenset({VersionState.PREVIZ_READY, VersionState.FAILED, VersionState.CANCELLED}),
    VersionState.PREVIZ_READY: frozenset({VersionState.APPROVED, VersionState.PREVIZ_RUNNING, VersionState.CANCELLED}),
    VersionState.APPROVED: frozenset({VersionState.GENERATING, VersionState.CANCELLED}),
    VersionState.GENERATING: frozenset(
        {
            VersionState.READY,
            VersionState.PARTIAL,
            VersionState.NEEDS_REVIEW,
            VersionState.FAILED,
            VersionState.CANCELLED,
        }
    ),
    # `:resume` sends partial, failed and cancelled versions back to generating (§12.8).
    VersionState.PARTIAL: frozenset({VersionState.GENERATING}),
    VersionState.FAILED: frozenset({VersionState.GENERATING}),
    VersionState.CANCELLED: frozenset({VersionState.GENERATING}),
    VersionState.READY: frozenset(),
    VersionState.NEEDS_REVIEW: frozenset(),
}
"""Allowed state changes. A failed or cancelled version that never passed approval cannot resume
generation in practice; the API checks that it reached `approved` before (§30 `:resume`)."""


class VersionOrigin(StrEnum):
    PLAN = "plan"
    REPLAN = "replan"
    EDIT = "edit"
    REGENERATE = "regenerate"
    REROUTE = "reroute"
    RESTORE = "restore"
    BRANCH = "branch"
    LOCK_CHANGE = "lock_change"
    TAKE_SELECT = "take_select"
    DUPLICATE = "duplicate"
    VARIANT = "variant"
    REMIX = "remix"


class VersionFlag(StrEnum):
    COVERAGE_CHANGED = "coverage_changed"
    NEEDS_WORLD_APPROVAL = "needs_world_approval"
    APPROXIMATIONS_STALE = "approximations_stale"


class ArtifactKind(StrEnum):
    """Closed artifact kinds (§12.2)."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    CAPTIONS = "captions"
    ALIGNMENT = "alignment"
    CBS = "cbs"
    COMPILED_BEHAVIOR = "compiled_behavior"
    KEYFRAME_STATE = "keyframe_state"
    OBSERVED_BEHAVIOR = "observed_behavior"
    COVERAGE_REPORT = "coverage_report"
    PLAN_REPORT = "plan_report"
    SCREEN_ANALYSIS = "screen_analysis"
    WORLD_FINGERPRINTS = "world_fingerprints"
    VOICE_CONDITIONING = "voice_conditioning"
    LOGS = "logs"
    OTHER = "other"


class ShotType(StrEnum):
    """Closed shot types (§11). `two_shot` is rejected while `multi_character_enabled=false`."""

    TALKING_HEAD = "talking_head"
    BROLL = "broll"
    INSERT = "insert"
    SCREEN = "screen"
    PRODUCT = "product"
    TITLE_CARD = "title_card"
    REACTION_CLIP = "reaction_clip"
    SILENT_HOLD = "silent_hold"
    TWO_SHOT = "two_shot"


class ShotLayer(StrEnum):
    BASE = "base"
    OVERLAY = "overlay"


class CoverageLevel(StrEnum):
    """Predicted execution of a requested behavior item (§10.3, §15.7)."""

    HONORED = "HONORED"
    APPROXIMATED = "APPROXIMATED"
    UNSUPPORTED = "UNSUPPORTED"


class ObservationVerdict(StrEnum):
    """What the analyzers saw (§16.3)."""

    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    NOT_OBSERVED = "NOT_OBSERVED"
    CONTRADICTED = "CONTRADICTED"
    NOT_MEASURABLE = "NOT_MEASURABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Outcome(StrEnum):
    """The 12 viewer-level outcomes (§16.4). Only `*_CONFIRMED` counts as delivered (I9)."""

    HONORED_CONFIRMED = "HONORED_CONFIRMED"
    HONORED_PARTIAL = "HONORED_PARTIAL"
    HONORED_NOT_OBSERVED = "HONORED_NOT_OBSERVED"
    HONORED_CONTRADICTED = "HONORED_CONTRADICTED"
    APPROXIMATED_CONFIRMED = "APPROXIMATED_CONFIRMED"
    APPROXIMATED_PARTIAL = "APPROXIMATED_PARTIAL"
    APPROXIMATED_NOT_OBSERVED = "APPROXIMATED_NOT_OBSERVED"
    APPROXIMATED_CONTRADICTED = "APPROXIMATED_CONTRADICTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNSUPPORTED_OBSERVED = "UNSUPPORTED_OBSERVED"
    NOT_MEASURABLE = "NOT_MEASURABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RealizationMethod(StrEnum):
    """Behavior compiler realization methods (§15.7)."""

    NATIVE_PARAMETRIC = "native_parametric"
    NATIVE_SEGMENT = "native_segment"
    TEXT_PROMPT_SEGMENT = "text_prompt_segment"
    TEXT_PROMPT_GLOBAL = "text_prompt_global"
    SHOT_SPLIT = "shot_split"
    PROSODY_TRANSFER = "prosody_transfer"
    AUDIO_NONVERBAL = "audio_nonverbal"
    EDITORIAL_CUTAWAY = "editorial_cutaway"
    EDITORIAL_PUNCH = "editorial_punch"
    CAPTION_EMPHASIS = "caption_emphasis"
    MUSIC_CUE = "music_cue"
    SFX_CUE = "sfx_cue"
    KEYFRAME_CONDITIONING = "keyframe_conditioning"
    POSE_GUIDED = "pose_guided"
    POST_EXPRESSION = "post_expression"
    OMIT = "omit"


class JobKind(StrEnum):
    """Job kinds; each maps to exactly one workflow (§9)."""

    PLAN = "plan"
    PREVIZ = "previz"
    GENERATE = "generate"
    REGENERATE = "regenerate"
    EDIT_PROPOSE = "edit_propose"
    EDIT_APPLY = "edit_apply"
    RENDER = "render"
    CRITIQUE = "critique"
    PACKAGE = "package"
    IDENTITY_PACK = "identity_pack"
    WORLD_PLATES = "world_plates"
    WARDROBE_REFS = "wardrobe_refs"
    CREATOR_TEST = "creator_test"
    VOICE_DESIGN = "voice_design"
    VOICE_TEST = "voice_test"
    MEMORY_UPDATE = "memory_update"
    CONSISTENCY = "consistency"
    RESEARCH_INGEST = "research_ingest"
    SCREEN_ANALYSIS = "screen_analysis"
    ASSET_VALIDATION = "asset_validation"
    BENCHMARK = "benchmark"
    CALIBRATION = "calibration"
    RETENTION = "retention"
    DELETION = "deletion"
    LORA_TRAIN = "lora_train"
    AUTONOMOUS_SUGGEST = "autonomous_suggest"


class RuntimeFamily(StrEnum):
    """One image per runtime family (ADR 0007)."""

    IMAGE = "image"
    WAN = "wan"
    TTS = "tts"
    ASR = "asr"
    AUDIO = "audio"
    LIPSYNC = "lipsync"
    VISION = "vision"
    POST = "post"
    VLLM = "vllm"
    CPU_MODEL = "cpu_model"
    CPU_INPROC = "cpu_inproc"


class AnnotationType(StrEnum):
    """Script annotation types (§11)."""

    PAUSE = "pause"
    EMPHASIS = "emphasis"
    NONVERBAL_AUDIO = "nonverbal_audio"
    DELIVERY = "delivery"
    PRONUNCIATION = "pronunciation"
    INSERTED_DISFLUENCY = "inserted_disfluency"


class ElementSource(StrEnum):
    """Who wrote a state, event, annotation or derived element (§15.3)."""

    DIRECTOR = "director"
    USER_TAG = "user_tag"
    USER_EDIT = "user_edit"
    INTENT_POLICY = "intent_policy"
    COMPILER_APPROXIMATION = "compiler_approximation"


class DerivedFromKind(StrEnum):
    """Traceability kinds (§11 `derived_from`)."""

    INTENT = "intent"
    ACTING = "acting"
    DNA = "dna"
    MEMORY = "memory"
    POLICY = "policy"
    COMPILER_APPROXIMATION = "compiler_approximation"
    USER = "user"


class Priority(StrEnum):
    MUST = "must"
    SHOULD = "should"
    NICE = "nice"


class InputMode(StrEnum):
    IDEA = "idea"
    STRUCTURED = "structured"
    EXACT_SCRIPT = "exact_script"
    BRAIN_DUMP = "brain_dump"


class QualityTier(StrEnum):
    DRAFT = "draft"
    FINAL = "final"


class CastRole(StrEnum):
    HOST = "host"
    GUEST = "guest"
    NARRATOR = "narrator"


class CreatorKind(StrEnum):
    """`creators.kind` (§17.3). Digital twins are behind `digital_twins_enabled=false` until V1."""

    SYNTHETIC = "synthetic"
    DIGITAL_TWIN = "digital_twin"


class MemoryStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    FORGOTTEN = "forgotten"
    SUPERSEDED = "superseded"


class ConflictState(StrEnum):
    NONE = "none"
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class MemorySourceType(StrEnum):
    AUTHORED = "authored"
    PLAN = "plan"
    EXPORT = "export"
    OBSERVATION = "observation"
    USER_EDIT = "user_edit"
    CRITIQUE = "critique"
    IMPORT = "import"


class ReliabilityClass(StrEnum):
    """Observation proxy reliability (§16.2)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TemporalPrecision(StrEnum):
    """How precisely in time a requested control must be realized (CBS `requested_controls`)."""

    WORD = "word"
    SEGMENT = "segment"
    SHOT = "shot"
    SCENE = "scene"


class TimeOfDay(StrEnum):
    """World `time_of_day` values (§19.1)."""

    MORNING = "morning"
    MIDDAY = "midday"
    LATE_AFTERNOON = "late_afternoon"
    GOLDEN_HOUR = "golden_hour"
    EVENING = "evening"
    NIGHT = "night"


class Weather(StrEnum):
    CLEAR = "clear"
    OVERCAST = "overcast"
    RAIN = "rain"


class ElementMutability(StrEnum):
    FIXED = "fixed"
    MOVABLE = "movable"
    STATEFUL = "stateful"


class CameraPositionStatus(StrEnum):
    PERMITTED = "permitted"
    FORBIDDEN = "forbidden"


class ProfileSource(StrEnum):
    """Where a measured behavior profile came from (§16.6)."""

    MOCK = "mock"
    BENCH = "bench"
    PRODUCTION = "production"
