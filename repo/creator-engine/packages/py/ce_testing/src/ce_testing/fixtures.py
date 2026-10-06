"""Canonical fixtures: the creator "Alex", the world "Alex's home office" and the §11 example spec.

These mirror the examples in docs/MASTER_BUILD_PROMPT.md (§11 VideoSpec, §15.6 CBS, §19.1
World DNA) and are the data `ce seed dev` writes (§40 Phase 1). The spec example in the
prompt is abbreviated to one scene; here its target duration is 6 s so the one scene fits,
and its emotional arc lists only the emotions that scene performs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ce_core.canonical import content_digest
from ce_core.enums import RecordStatus
from ce_core.identity.creator import AppearanceDNA, CreatorDNA, VoiceDNA, WardrobeSpec
from ce_core.identity.memory import MemorySnapshot, SnapshotItem
from ce_core.identity.world import WorldDNA
from ce_core.spec.validate import (
    CreatorVersionInfo,
    InMemoryReferences,
    OwnedVersionInfo,
    SnapshotInfo,
    VoiceVersionInfo,
    WorldVersionInfo,
)
from ce_core.spec.videospec import VideoSpec

__all__ = [
    "ALEX",
    "VOCAB_VERSION",
    "alex_appearance_dna",
    "alex_creator_dna",
    "alex_memory_snapshot",
    "alex_voice_dna",
    "example_references",
    "example_spec",
    "example_spec_dict",
    "grey_hoodie",
    "home_office_world",
    "route_digest_placeholder",
    "words",
]

VOCAB_VERSION = "2026.10.1"
FIXED_TIME = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _id(suffix: str) -> UUID:
    return UUID(f"0192f0a0-0000-7000-8000-{suffix.rjust(12, '0')}")


class ALEX:
    """Stable ids shared by the spec example, the CBS example and the dev seed."""

    ORG_ID = _id("f0")
    USER_ID = _id("f1")
    PROJECT_ID = _id("f2")
    VIDEO_ID = _id("1")
    VERSION_ID = _id("2")
    CREATOR_ID = _id("10")
    CREATOR_VERSION_ID = _id("11")
    APPEARANCE_ID = _id("1a")
    APPEARANCE_VERSION_ID = _id("12")
    VOICE_ID = _id("1b")
    VOICE_VERSION_ID = _id("13")
    WORLD_ID = _id("1f")
    WORLD_VERSION_ID = _id("20")
    WARDROBE_ID = _id("2f")
    WARDROBE_VERSION_ID = _id("30")
    SNAPSHOT_ID = _id("a1")
    MEMORY_GAZE_ID = _id("b1")
    MEMORY_LAUGH_ID = _id("b2")
    MEMORY_PHRASE_ID = _id("b3")
    MEMORY_FACT_ID = _id("b4")
    CANONICAL_FACE_ASSET_ID = _id("c1")
    VOICE_REFERENCE_ASSET_ID = _id("c2")
    PLATE_FRONT_ASSET_ID = _id("c3")
    PLATE_SIDE_ASSET_ID = _id("c4")
    # Edit fixtures (Phase 6): an accent change, an outfit change and another room (§2 edits).
    VOICE_UK_VERSION_ID = _id("14")
    WARDROBE_NAVY_ID = _id("3f")
    WARDROBE_NAVY_VERSION_ID = _id("31")
    OFFICE_WORLD_ID = _id("4f")
    OFFICE_WORLD_VERSION_ID = _id("40")
    PLATE_OFFICE_FRONT_ASSET_ID = _id("c5")
    PLATE_OFFICE_WIDE_ASSET_ID = _id("c6")


def route_digest_placeholder() -> str:
    """The planned route's digest of the §11 example (`sha256:…` in the prompt): the identity of
    the `mock_avatar_global` route the example's cutaway was planned for, so a default mock build
    does not flag it stale (a re-route to another engine does, I1)."""
    return content_digest(
        {
            "adapter_id": "mock_avatar_global",
            "model_id": "mock-avatar-global",
            "revision": "1",
            "translator_version": "0.1.0",
        }
    )


def alex_creator_dna() -> CreatorDNA:
    return CreatorDNA.model_validate(
        {
            "vocab_version": VOCAB_VERSION,
            "identity": {
                "display_name": "Alex",
                "bio": "Explains AI and work tools to curious non-technical professionals.",
                "canon": [
                    {
                        "key": "home_city",
                        "subject": "Alex",
                        "predicate": "lives_in",
                        "object": "Austin",
                        "pinned": True,
                    },
                    {"key": "occupation", "subject": "Alex", "predicate": "works_as", "object": "product designer"},
                ],
            },
            "personality": {
                "traits": {"openness": 0.8, "conscientiousness": 0.6, "extraversion": 0.65, "agreeableness": 0.7},
                "values": ["clarity", "curiosity"],
                "humor_style": "dry",
                "directness": 0.7,
                "warmth": 0.6,
                "sarcasm": 0.2,
                "seriousness": 0.5,
                "conversational_style": "friendly_peer",
            },
            "speech": {
                "vocabulary_level": "conversational",
                "signature_phrases": [{"text": "here's the thing", "max_per_video": 1}],
                "sentence_structure": "short_punchy",
                "filler_tendency": {"fillers": ["honestly"], "rate": 0.05},
                "speech_rate_preference": "average",
            },
            "behavior": {
                "baseline": {"energy": 0.65, "confidence": 0.7, "warmth": 0.6, "expressivity": 0.55},
                "emotion_ranges": {"confident": [0.3, 0.8], "serious": [0.2, 0.8], "amused": [0.1, 0.6]},
                "escalation_style": "gradual",
                "deescalation_style": "gradual",
                "masking_tendency": 0.4,
                "reaction_habits": {
                    "laughter": {"expression": "small_smile", "intensity": 0.3, "frequency": "often"},
                    "skepticism": {"expression": "eyebrow_raise", "intensity": 0.4, "frequency": "sometimes"},
                },
            },
            "gesture": {
                "preferred_gestures": [{"gesture": "open_palms", "frequency": "often"}],
                "gesture_density": 0.45,
                "preferred_hand": "right",
                "head_movement_amplitude": 0.4,
                "default_posture": "seated_upright",
            },
            "gaze": {
                "eye_contact_ratio": 0.75,
                "look_away_directions": ["down_left"],
                "look_away_ms": 600,
                "thinking_gaze": "down_left",
            },
            "camera": {
                "framing_preference": "medium_close_up",
                "camera_distance": "medium",
                "selfie_behavior": "propped_phone",
                "movement_preference": "subtle_handheld",
                "preferred_camera_profiles": ["phone_front_selfie", "desk_mirrorless"],
            },
            "fashion": {"style_descriptors": ["casual", "muted colors"], "avoid": ["suits"]},
            "world": {"world_kind_preferences": ["home_office"], "avoid": []},
            "editing": {
                "cut_cadence": "medium",
                "caption_style_preference": "bold_pop_highlight",
                "music_taste": ["light electronic"],
            },
            "avoidances": {
                "gestures": ["finger_point_at_camera"],
                "emotions": [{"label": "angry", "max_intensity": 0.5}],
                "phrases": ["guys"],
            },
        }
    )


def alex_appearance_dna() -> AppearanceDNA:
    return AppearanceDNA(
        face_shape="oval",
        skin="light olive",
        hair="short dark brown, slightly messy",
        eyes="brown",
        distinctive_features=["light stubble"],
        body_type="average",
        grooming_style="casual, neat",
        age_appearance=31,
    )


def alex_voice_dna() -> VoiceDNA:
    return VoiceDNA.model_validate(
        {
            "kind": "designed",
            "description": "warm, mid-pitched male voice, relaxed American accent, clear diction",
            "references": [
                {
                    "language": "en-US",
                    "asset_id": str(ALEX.VOICE_REFERENCE_ASSET_ID),
                    "transcript": "Everyone thinks AI agents are just smarter chatbots.",
                }
            ],
            "wpm": {"en": 150},
            "lexicon": [{"term": "LLM", "respelling": "el el em"}],
            "default_prosody": {
                "accent": "general_american",
                "pitch_semitones": 0,
                "speed": 1.0,
                "energy": 0.6,
                "expressivity": 0.55,
            },
        }
    )


def alex_voice_uk_dna() -> VoiceDNA:
    """A fixture voice version of Alex with a British accent ("change the accent"; voice design
    arrives with Phase 10, §40 Phase 6 DoD)."""
    base = alex_voice_dna().model_dump(mode="json")
    base["description"] = "warm, mid-pitched male voice, relaxed southern British accent, clear diction"
    base["default_prosody"] = {**base["default_prosody"], "accent": "southern_british"}
    return VoiceDNA.model_validate(base)


def navy_sweater() -> WardrobeSpec:
    return WardrobeSpec(
        name="navy sweater", description="a fitted navy crewneck sweater over a white collar", style_tags=["smart"]
    )


def grey_hoodie() -> WardrobeSpec:
    return WardrobeSpec(
        name="grey hoodie", description="a plain mid-grey zip hoodie over a white t-shirt", style_tags=["casual"]
    )


def home_office_world() -> WorldDNA:
    """The §19.1 example, completed with the fields the model requires."""
    return WorldDNA.model_validate(
        {
            "vocab_version": VOCAB_VERSION,
            "name": "Alex's home office",
            "kind": "home_office",
            "style_tags": ["warm", "lived-in"],
            "palette": ["#3b3a36", "#d9cbb3", "#ff8a3d"],
            "geometry": {
                "dimensions_m": [3.5, 3.0, 2.6],
                "layout": "desk against the right wall, window on the left, bookshelf behind the chair",
            },
            "zones": [
                {
                    "key": "zone_desk_chair",
                    "label": "desk chair",
                    "position": [0.62, 0.55, 0.0],
                    "allowed_postures": ["seated_upright", "seated_relaxed", "seated_lean_in"],
                }
            ],
            "elements": [
                {
                    "key": "el_desk",
                    "kind": "furniture",
                    "label": "oak desk",
                    "position": [0.7, 0.55, 0.0],
                    "signature": True,
                },
                {
                    "key": "el_monitor",
                    "kind": "screen",
                    "label": "27-inch monitor",
                    "position": [0.78, 0.55, 0.75],
                    "mutability": "stateful",
                    "states": ["on", "off"],
                    "default_state": "on",
                },
                {
                    "key": "el_shelf",
                    "kind": "furniture",
                    "label": "bookshelf with plants",
                    "position": [0.5, 0.95, 0.0],
                    "signature": True,
                },
                {
                    "key": "el_neon",
                    "kind": "light",
                    "label": "small orange neon sign",
                    "position": [0.35, 0.97, 1.6],
                    "signature": True,
                    "mutability": "stateful",
                    "states": ["on", "off"],
                    "default_state": "on",
                },
                {
                    "key": "el_mug",
                    "kind": "prop",
                    "label": "white coffee mug",
                    "position": [0.72, 0.5, 0.76],
                    "mutability": "movable",
                },
                {"key": "el_window", "kind": "window", "label": "window", "position": [0.02, 0.5, 1.2]},
            ],
            "background_layouts": {
                "cam_desk_front": {
                    "visible_elements": ["el_shelf", "el_neon", "el_window"],
                    "composition": "shelf centered behind the head",
                }
            },
            "lighting": {
                "key": {"azimuth_deg": -60, "elevation_deg": 20, "intensity": 0.8, "color_temp_k": 5200},
                "fill_ratio": 0.4,
                "practicals": [{"element": "el_neon", "color_temp_k": 2200}],
                "tolerance": {"color_temp_k": 400, "luminance": 0.12},
            },
            "time_and_weather": {
                "default_time_of_day": "late_afternoon",
                "allowed_times": ["morning", "late_afternoon", "evening"],
                "default_weather": "clear",
                "allowed_weather": ["clear", "overcast"],
            },
            "acoustics": {
                "room_profile": "small_office",
                "rt60_s": 0.35,
                "ambient": ["room_tone_light_hvac"],
                "noise_floor_db": -62,
            },
            "camera_positions": [
                {
                    "key": "cam_desk_front",
                    "height_m": 1.2,
                    "distance_m": 0.6,
                    "lens_equiv_mm": 24,
                    "default_framing": "medium_close_up",
                    "allowed_camera_profiles": ["phone_front_selfie", "webcam", "laptop_camera", "desk_mirrorless"],
                    "status": "permitted",
                },
                {
                    "key": "cam_side_wide",
                    "height_m": 1.4,
                    "distance_m": 2.2,
                    "lens_equiv_mm": 28,
                    "default_framing": "medium_wide",
                    "allowed_camera_profiles": ["desk_mirrorless", "dslr"],
                    "status": "permitted",
                },
            ],
            "continuity": {"must_show_from": {"cam_desk_front": ["el_shelf", "el_neon"]}},
        }
    )


def modern_office_world() -> WorldDNA:
    """A second approved world: a bright modern office ("change the room to a modern office")."""
    return WorldDNA.model_validate(
        {
            "vocab_version": VOCAB_VERSION,
            "name": "Modern office",
            "kind": "office",
            "style_tags": ["modern", "bright", "minimal"],
            "palette": ["#f4f4f2", "#2b2d42", "#8d99ae"],
            "geometry": {"dimensions_m": [5.0, 4.0, 2.8], "layout": "standing desk by a glass wall, plants behind"},
            "zones": [
                {
                    "key": "zone_desk_chair",
                    "label": "desk chair",
                    "position": [0.5, 0.5, 0.0],
                    "allowed_postures": ["seated_upright", "seated_relaxed", "seated_lean_in", "standing_upright"],
                }
            ],
            "elements": [
                {
                    "key": "el_desk",
                    "kind": "furniture",
                    "label": "white standing desk",
                    "position": [0.55, 0.5, 0.0],
                    "signature": True,
                },
                {
                    "key": "el_glass_wall",
                    "kind": "fixture",
                    "label": "frosted glass wall",
                    "position": [0.5, 0.98, 0.0],
                    "signature": True,
                },
                {"key": "el_plant", "kind": "plant", "label": "tall fiddle-leaf fig", "position": [0.2, 0.9, 0.0]},
                {
                    "key": "el_lamp",
                    "kind": "light",
                    "label": "pendant lamp",
                    "position": [0.5, 0.6, 2.2],
                    "mutability": "stateful",
                    "states": ["on", "off"],
                    "default_state": "on",
                },
            ],
            "background_layouts": {
                "cam_desk_front": {
                    "visible_elements": ["el_glass_wall", "el_plant", "el_lamp"],
                    "composition": "glass wall behind, plant on the left",
                }
            },
            "lighting": {
                "key": {"azimuth_deg": 30, "elevation_deg": 35, "intensity": 0.85, "color_temp_k": 5600},
                "fill_ratio": 0.6,
                "practicals": [{"element": "el_lamp", "color_temp_k": 3200}],
                "tolerance": {"color_temp_k": 400, "luminance": 0.12},
            },
            "time_and_weather": {
                "default_time_of_day": "midday",
                "allowed_times": ["morning", "midday", "late_afternoon"],
                "default_weather": "clear",
                "allowed_weather": ["clear", "overcast"],
            },
            "acoustics": {
                "room_profile": "small_office",
                "rt60_s": 0.5,
                "ambient": ["room_tone_light_hvac"],
                "noise_floor_db": -58,
            },
            "camera_positions": [
                {
                    "key": "cam_desk_front",
                    "height_m": 1.3,
                    "distance_m": 0.8,
                    "lens_equiv_mm": 26,
                    "default_framing": "medium_close_up",
                    "allowed_camera_profiles": ["phone_front_selfie", "webcam", "laptop_camera", "desk_mirrorless"],
                    "status": "permitted",
                },
                {
                    "key": "cam_wide",
                    "height_m": 1.5,
                    "distance_m": 3.0,
                    "lens_equiv_mm": 28,
                    "default_framing": "medium_wide",
                    "allowed_camera_profiles": ["desk_mirrorless", "dslr"],
                    "status": "permitted",
                },
            ],
            "continuity": {"must_show_from": {"cam_desk_front": ["el_glass_wall"]}},
        }
    )


def alex_memory_snapshot() -> MemorySnapshot:
    return MemorySnapshot(
        id=ALEX.SNAPSHOT_ID,
        creator_id=ALEX.CREATOR_ID,
        creator_version_id=ALEX.CREATOR_VERSION_ID,
        character_key="char_alex",
        items=[
            SnapshotItem(
                item_id=ALEX.MEMORY_GAZE_ID,
                kind="gaze_habit.thinking_glance",
                value={"direction": "down_left", "typical_ms": 600},
                text="Glances down-left for about 600 ms while thinking.",
                confidence=0.8,
            ),
            SnapshotItem(
                item_id=ALEX.MEMORY_LAUGH_ID,
                kind="reaction_habit.laughter",
                value={"expression": "small_smile", "intensity": 0.3, "frequency": "often"},
                text="Usually laughs with a small smile.",
                confidence=0.7,
            ),
        ],
        retrieval_params={"budget": {"gaze_habit": 3, "reaction_habit": 3}, "planner": "fixture"},
        created_at=FIXED_TIME,
    )


def words(segment_key: str, first: int, last: int) -> dict[str, Any]:
    """A WordSpan within one segment, as plain data."""
    return {
        "kind": "words",
        "start": {"segment_key": segment_key, "word": first},
        "end": {"segment_key": segment_key, "word": last},
    }


def example_spec_dict() -> dict[str, Any]:
    """The §11 example as plain data (made complete and valid; see the module docstring)."""
    return {
        "schema_version": "1.0",
        "vocab_version": VOCAB_VERSION,
        "tokenizer_version": "1",
        "video_id": str(ALEX.VIDEO_ID),
        "version_id": str(ALEX.VERSION_ID),
        "parent_version_id": None,
        "meta": {
            "title": "Why most people misunderstand AI agents",
            "mode": "talking_head_explainer",
            "language": "en-US",
            "platform_targets": ["tiktok", "youtube_shorts"],
            "primary_aspect": "9:16",
            "target_duration_s": 6,
            "quality_tier": "draft",
            "template_ids": [],
            "strategy_pack": "myth_vs_reality",
        },
        "brief": {
            "input_mode": "idea",
            "raw_input": "Create a 30-second TikTok explaining why most people misunderstand AI agents.",
            "audience": "curious non-technical professionals",
            "angle": "agents are workflows with judgment, not smarter chatbots",
            "assumptions": ["No sources supplied; claims kept general and non-statistical."],
            "hook_candidates": [
                {"key": "hk_1", "text": "Everyone thinks AI agents are just smarter chatbots."},
                {"key": "hk_2", "text": "You've been sold the wrong idea about AI agents."},
            ],
            "selected_hook_key": "hk_1",
            "sources_policy": "open",
            "constraints": [],
        },
        "intent": {
            "video": {
                "narrative_goal": "challenge_common_belief",
                "audience_effect": "insight",
                "persuasion_goal": "reframe_mental_model",
                "information_goal": "define_concept",
                "emotional_arc": ["confident", "serious"],
                "attention_goal": "sustain",
                "cta_goal": "follow_for_series",
            }
        },
        "memory": {"snapshots": [{"character_key": "char_alex", "snapshot_id": str(ALEX.SNAPSHOT_ID)}]},
        "cast": [
            {
                "key": "char_alex",
                "role": "host",
                "creator_version_id": str(ALEX.CREATOR_VERSION_ID),
                "overrides": {"appearance_version_id": None, "voice_version_id": None},
            }
        ],
        "products": [],
        "script": {
            "segments": [
                {
                    "key": "seg_1",
                    "speaker_key": "char_alex",
                    "text": "Everyone thinks AI agents are just smarter chatbots.",
                    "language": None,
                    "annotations": [
                        {
                            "key": "an_1",
                            "type": "emphasis",
                            "tag": "emphasize",
                            "span": words("seg_1", 6, 6),
                            "source": "director",
                        }
                    ],
                    "claim_keys": [],
                },
                {
                    "key": "seg_2",
                    "speaker_key": "char_alex",
                    "text": "But here's the thing... they're not.",
                    "language": None,
                    "annotations": [
                        {
                            "key": "an_2",
                            "type": "pause",
                            "tag": "short_pause",
                            "span": words("seg_2", 3, 3),
                            "source": "intent_policy",
                        },
                        {
                            "key": "an_3",
                            "type": "emphasis",
                            "tag": "emphasize",
                            "span": words("seg_2", 5, 5),
                            "source": "intent_policy",
                        },
                    ],
                    "claim_keys": [],
                },
            ]
        },
        "scenes": [
            {
                "key": "scn_hook",
                "purpose": "hook",
                "order": 1,
                "segment_keys": ["seg_1", "seg_2"],
                "intent": {
                    "narrative_goal": "challenge_common_belief",
                    "emotional_goal": "create_doubt_then_reveal",
                    "audience_effect": "curiosity",
                    "persuasion_goal": "none",
                    "information_goal": "state_misconception",
                    "attention_goal": "delay_payoff",
                    "reveal_strategy": "tease_then_reveal",
                    "tension_level": 0.6,
                    "curiosity_level": 0.8,
                    "performance_strategy": "start_confident_then_lower_energy",
                    "notes": "Say the common belief as if agreeing, then undercut it.",
                },
                "world": {
                    "world_version_id": str(ALEX.WORLD_VERSION_ID),
                    "camera_position_key": "cam_desk_front",
                    "time_of_day": "late_afternoon",
                    "weather": "clear",
                    "overrides": {
                        "element_states": {"el_monitor": "on"},
                        "hide_elements": [],
                        "add_elements": [],
                        "move_elements": [],
                        "lighting": None,
                        "acoustics": None,
                    },
                    "continuity_ref": {"kind": "world_plate", "camera_position_key": "cam_desk_front"},
                },
                "cast": [
                    {
                        "character_key": "char_alex",
                        "wardrobe_version_id": str(ALEX.WARDROBE_VERSION_ID),
                        "placement": "zone_desk_chair",
                        "default_posture": "seated_upright",
                    }
                ],
                "acting": {
                    "situation": {
                        "kind": "contradicting_the_audience",
                        "description": (
                            "Alex states the popular belief as if agreeing, then realizes the viewer probably "
                            "believes it too and decides to challenge it."
                        ),
                        "audience_stance": "agrees_with_misconception",
                        "stimulus": None,
                    },
                    "states": [
                        {
                            "key": "st_1",
                            "character_key": "char_alex",
                            "source": "director",
                            "span": words("seg_1", 0, 7),
                            "internal_state": {
                                "label": "amused_certainty",
                                "description": "Knows the belief is wrong and finds it a little funny.",
                            },
                            "social_goal": "build_rapport",
                            "audience_goal": "make_viewer_nod_along",
                            "performance_intent": "mock_agreement",
                            "emotion": {
                                "felt": {"label": "amused", "intensity": 0.4},
                                "displayed": {"label": "confident", "intensity": 0.6},
                                "masking": True,
                            },
                            "confidence_delta": 0.0,
                            "attention_target": "camera",
                            "strategies": {
                                "prosody": "assertive_light",
                                "gaze": "hold_camera",
                                "gesture": "illustrative_light",
                                "posture": "seated_upright",
                                "reaction": "suppressed",
                                "camera_awareness": "direct_address",
                            },
                            "transition_in": None,
                            "priority": "should",
                        },
                        {
                            "key": "st_2",
                            "character_key": "char_alex",
                            "source": "director",
                            "span": {
                                "kind": "words",
                                "start": {"segment_key": "seg_2", "word": 0},
                                "end": {"segment_key": "seg_2", "word": 5},
                            },
                            "internal_state": {
                                "label": "deliberate_reveal",
                                "description": "Drops the act; wants the viewer to feel the turn.",
                            },
                            "social_goal": "establish_authority",
                            "audience_goal": "create_doubt",
                            "performance_intent": "undercut_belief",
                            "emotion": {
                                "felt": {"label": "serious", "intensity": 0.6},
                                "displayed": {"label": "serious", "intensity": 0.6},
                                "masking": False,
                            },
                            "confidence_delta": 0.15,
                            "attention_target": "camera",
                            "strategies": {
                                "prosody": "slow_measured",
                                "gaze": "glance_away_and_return",
                                "gesture": "still",
                                "posture": "seated_lean_in",
                                "reaction": "none",
                                "camera_awareness": "direct_address",
                            },
                            "transition_in": {
                                "trigger": {
                                    "kind": "realization",
                                    "at": {"segment_key": "seg_2", "word": 0},
                                    "description": "Realizes the viewer agrees with the misconception.",
                                },
                                "style": "sudden",
                                "duration_words": 1,
                            },
                            "priority": "must",
                        },
                    ],
                    "events": [
                        {
                            "key": "ev_1",
                            "character_key": "char_alex",
                            "type": "look_away",
                            "at": {"segment_key": "seg_2", "word": 2},
                            "duration_ms": 700,
                            "direction": "down_left",
                            "intensity": 0.5,
                            "purpose": "thinking",
                            "priority": "should",
                            "source": "director",
                        },
                        {
                            "key": "ev_2",
                            "character_key": "char_alex",
                            "type": "small_smile",
                            "at": {"segment_key": "seg_2", "word": 5},
                            "duration_ms": 600,
                            "direction": None,
                            "intensity": 0.3,
                            "purpose": "knowing",
                            "priority": "nice",
                            "source": "director",
                        },
                    ],
                },
                "shots": [
                    {
                        "key": "sht_1",
                        "type": "talking_head",
                        "layer": "base",
                        "span": {
                            "kind": "words",
                            "start": {"segment_key": "seg_1", "word": 0},
                            "end": {"segment_key": "seg_2", "word": 5},
                        },
                        "character_key": "char_alex",
                        "camera": {
                            "profile_id": "phone_front_selfie",
                            "framing": "medium_close_up",
                            "angle": "eye_level",
                            "moves": [
                                {
                                    "key": "mv_1",
                                    "type": "punch_in",
                                    "at": {"segment_key": "seg_2", "word": 0},
                                    "scale": 1.12,
                                    "transition": "cut",
                                    "derived_from": [
                                        {"kind": "intent", "ref": "/scenes[scn_hook]/intent/reveal_strategy"}
                                    ],
                                }
                            ],
                        },
                        "visual": {
                            "keyframe": {"strategy": "composite", "asset_id": None},
                            "prompt_extra": "",
                            "negative": "",
                        },
                        "takes": {"count": 2, "selected_take_key": None},
                    },
                    {
                        "key": "sht_2",
                        "type": "broll",
                        "layer": "overlay",
                        "span": words("seg_2", 2, 3),
                        "character_key": None,
                        "camera": {"profile_id": "desk_mirrorless", "framing": "insert", "angle": "high", "moves": []},
                        "broll": {
                            "source": "generate",
                            "asset_id": None,
                            "prompt": (
                                "close-up of a chat window on a laptop screen, shallow depth of field, "
                                "soft office light"
                            ),
                            "world_bound": True,
                            "allow_text_in_frame": False,
                        },
                        "takes": {"count": 1, "selected_take_key": None},
                        "derived_from": [
                            {
                                "kind": "compiler_approximation",
                                "ref": "/scenes[scn_hook]/acting/events[ev_1]",
                                "route_digest": route_digest_placeholder(),
                            }
                        ],
                    },
                ],
            }
        ],
        "audio": {
            "music": {
                "cues": [
                    {
                        "key": "mc_1",
                        "span": {"kind": "scene", "scene_key": "scn_hook"},
                        "mode": "generate",
                        "mood": "curious, light electronic, no vocals",
                        "bpm": 96,
                        "asset_id": None,
                        "duck_db": -18,
                    }
                ]
            },
            "sfx": [
                {
                    "key": "sfx_1",
                    "at": {"kind": "shot", "shot_key": "sht_2", "offset_ms": 0},
                    "description": "soft whoosh",
                    "gain_db": -10,
                }
            ],
            "acoustics": {"source": "world", "mic_profile": None},
            "loudness": {"integrated_lufs": -14, "true_peak_dbtp": -1},
        },
        "captions": {
            "enabled": True,
            "style_id": "bold_pop_highlight",
            "language": "en",
            "max_words_per_line": 3,
            "highlight": "active_word",
            "placement": "platform_safe_zone",
            "translations": [],
        },
        "brand": {"brand_kit_id": None, "logo_overlay": False},
        "render": {
            "outputs": [{"preset_id": "tiktok_1080x1920_30", "aspect": "9:16"}],
            "reframe": {"strategy": "subject_aware", "regenerate_if_crop_loss_above": 0.25},
        },
        "provenance": {"visible_label": "auto", "consent_ids": []},
        "assets": [],
        "effects": [],
        "generation": {"seed_namespace": str(ALEX.VIDEO_ID), "seed_overrides": {}, "engine_hints": {}},
        "locks": [{"group": "voice", "scope": {"character_keys": ["char_alex"]}, "set_by": "user"}],
    }


def two_scene_spec_dict() -> dict[str, Any]:
    """The example split into two scenes (Phase 6 dirty-set tests): `scn_hook` speaks seg_1 from
    `cam_desk_front` (a position with a background layout), `scn_reveal` speaks seg_2 from
    `cam_side_wide` (no layout: every element is visible) with the B-roll overlay, the punch-in and
    both events. The music cue covers the hook; the whoosh sits on the overlay."""
    import copy

    data = example_spec_dict()
    hook = data["scenes"][0]
    reveal = copy.deepcopy(hook)
    st_1, st_2 = hook["acting"]["states"]
    talking = hook["shots"][0]
    broll = hook["shots"][1]
    hook["segment_keys"] = ["seg_1"]
    hook["acting"]["states"] = [st_1]
    hook["acting"]["events"] = []
    hook["shots"] = [
        {**copy.deepcopy(talking), "span": words("seg_1", 0, 7), "camera": {**talking["camera"], "moves": []}}
    ]
    reveal.update(
        key="scn_reveal",
        purpose="turn",
        order=2,
        segment_keys=["seg_2"],
    )
    reveal["intent"] = {**reveal["intent"], "reveal_strategy": "immediate_answer", "attention_goal": "release"}
    reveal["world"] = {
        **reveal["world"],
        "camera_position_key": "cam_side_wide",
        "continuity_ref": {"kind": "world_plate", "camera_position_key": "cam_side_wide"},
    }
    reveal["acting"]["states"] = [st_2]
    reveal["shots"] = [
        {
            **copy.deepcopy(talking),
            "key": "sht_4",
            "span": words("seg_2", 0, 5),
            "camera": {
                "profile_id": "desk_mirrorless",
                "framing": "medium",
                "angle": "eye_level",
                "moves": [{**talking["camera"]["moves"][0], "derived_from": []}],
            },
            "takes": {"count": 1, "selected_take_key": None},
        },
        {
            **broll,
            "derived_from": [
                {**d, "ref": d["ref"].replace("/scenes[scn_hook]/", "/scenes[scn_reveal]/")}
                for d in broll["derived_from"]
            ],
        },
    ]
    data["scenes"] = [hook, reveal]
    data["meta"]["target_duration_s"] = 7
    return data


def example_spec() -> VideoSpec:
    return VideoSpec.model_validate(example_spec_dict())


def example_references() -> InMemoryReferences:
    """The approved records the example spec references, with their DNA."""
    approved = RecordStatus.APPROVED
    return InMemoryReferences(
        creator_versions={
            ALEX.CREATOR_VERSION_ID: CreatorVersionInfo(
                ALEX.CREATOR_VERSION_ID, ALEX.CREATOR_ID, approved, alex_creator_dna()
            )
        },
        defaults={ALEX.CREATOR_VERSION_ID: (ALEX.APPEARANCE_VERSION_ID, ALEX.VOICE_VERSION_ID)},
        appearance_versions={
            ALEX.APPEARANCE_VERSION_ID: OwnedVersionInfo(ALEX.APPEARANCE_VERSION_ID, approved, ALEX.CREATOR_ID)
        },
        voice_versions={
            ALEX.VOICE_VERSION_ID: VoiceVersionInfo(ALEX.VOICE_VERSION_ID, approved, ALEX.CREATOR_ID, alex_voice_dna()),
            ALEX.VOICE_UK_VERSION_ID: VoiceVersionInfo(
                ALEX.VOICE_UK_VERSION_ID, approved, ALEX.CREATOR_ID, alex_voice_uk_dna()
            ),
        },
        wardrobe_versions={
            ALEX.WARDROBE_VERSION_ID: OwnedVersionInfo(ALEX.WARDROBE_VERSION_ID, approved, ALEX.CREATOR_ID),
            ALEX.WARDROBE_NAVY_VERSION_ID: OwnedVersionInfo(ALEX.WARDROBE_NAVY_VERSION_ID, approved, ALEX.CREATOR_ID),
        },
        world_versions={
            ALEX.WORLD_VERSION_ID: WorldVersionInfo(ALEX.WORLD_VERSION_ID, approved, home_office_world()),
            ALEX.OFFICE_WORLD_VERSION_ID: WorldVersionInfo(
                ALEX.OFFICE_WORLD_VERSION_ID, approved, modern_office_world()
            ),
        },
        snapshots={ALEX.SNAPSHOT_ID: SnapshotInfo(ALEX.SNAPSHOT_ID, ALEX.CREATOR_VERSION_ID)},
    )
