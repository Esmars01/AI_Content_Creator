"""The Creator Test script (§17.4): a fixed, about 20-second VideoSpec in the creator's default world.

Mini trajectory neutral → excited → hesitant → serious over four segments, a look-away while the
creator hesitates, a small laugh at the excited peak and one emphasis. The spec is the same for
every creator (only the cast, world binding, wardrobe and language change), so scorecards are
comparable across creators, versions and engines. `TEST_SCRIPT_VERSION` changes whenever the script
does; baselines record it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SEGMENTS", "TEST_SCRIPT_VERSION", "creator_test_spec"]

TEST_SCRIPT_VERSION = "creator-test-v1"

SEGMENTS = [
    ("seg_1", "Hi, I'm doing a quick test today, so let me just talk to you for a moment."),
    ("seg_2", "And honestly, this is the part I'm really excited about, because it changes everything!"),
    ("seg_3", "Although, wait, I'm not completely sure it works the same way for everyone."),
    ("seg_4", "So here is what matters: test it yourself before you trust it."),
]

STATES = [
    # key, segment, felt, displayed, internal state, social goal, audience goal, intent, strategies
    ("st_1", "seg_1", "neutral", "neutral", "quiet_confidence", "build_rapport", "make_viewer_nod_along",
     "explain_plainly", {"prosody": "controlled_even", "gaze": "hold_camera", "gesture": "still",
                         "posture": "seated_upright", "reaction": "none", "camera_awareness": "direct_address"}),
    ("st_2", "seg_2", "excited", "excited", "eager_to_share", "entertain", "spark_curiosity",
     "celebrate_result", {"prosody": "playful_rising", "gaze": "hold_camera", "gesture": "illustrative_strong",
                          "posture": "seated_lean_in", "reaction": "open", "camera_awareness": "direct_address"}),
    ("st_3", "seg_3", "hesitant", "hesitant", "uncertain_search", "win_trust", "make_viewer_feel_understood",
     "reflect", {"prosody": "hesitant_with_pauses", "gaze": "look_down_thinking", "gesture": "self_soothing",
                 "posture": "seated_upright", "reaction": "suppressed", "camera_awareness": "direct_address"}),
    ("st_4", "seg_4", "serious", "serious", "growing_conviction", "establish_authority", "make_viewer_act",
     "invite_action", {"prosody": "slow_measured", "gaze": "hold_camera", "gesture": "still",
                       "posture": "seated_lean_in", "reaction": "none", "camera_awareness": "direct_address"}),
]  # fmt: skip


def _words(segment: str, start: int, end: int, end_segment: str | None = None) -> dict[str, Any]:
    return {
        "kind": "words",
        "start": {"segment_key": segment, "word": start},
        "end": {"segment_key": end_segment or segment, "word": end},
    }


def _last_word(text: str) -> int:
    from ce_core.text.tokenize import tokenize

    return len(tokenize(text)) - 1


def creator_test_spec(
    *,
    video_id: str,
    version_id: str,
    creator_version_id: str,
    world: dict[str, Any],
    wardrobe_version_id: str | None,
    vocab_version: str,
    language: str = "en-US",
    seed_namespace: str,
    preset_id: str = "tiktok_1080x1920_30",
) -> dict[str, Any]:
    """The test video's VideoSpec (API form, before `submit_spec`)."""
    last = {key: _last_word(text) for key, text in SEGMENTS}
    states = []
    for key, segment, felt, displayed, internal, social, audience, intent, strategies in STATES:
        states.append(
            {
                "key": key,
                "character_key": "char_test",
                "source": "director",
                "span": _words(segment, 0, last[segment]),
                "internal_state": {"label": internal, "description": f"Creator Test: {displayed}"},
                "social_goal": social,
                "audience_goal": audience,
                "performance_intent": intent,
                "emotion": {
                    "felt": {"label": felt, "intensity": 0.6 if felt != "neutral" else 0.2},
                    "displayed": {"label": displayed, "intensity": 0.6 if displayed != "neutral" else 0.2},
                    "masking": False,
                },
                "confidence_delta": 0.0,
                "attention_target": "camera",
                "strategies": strategies,
                "transition_in": None
                if key == "st_1"
                else {
                    "trigger": {
                        "kind": "topic_shift",
                        "at": {"segment_key": segment, "word": 0},
                        "description": f"moves into {displayed}",
                    },
                    "style": "gradual",
                    "duration_words": 2,
                },
                "priority": "must",
            }
        )
    return {
        "schema_version": "1.0",
        "vocab_version": vocab_version,
        "tokenizer_version": "1",
        "video_id": video_id,
        "version_id": version_id,
        "parent_version_id": None,
        "meta": {
            "title": "Creator Test",
            "mode": "talking_head_explainer",
            "language": language,
            "platform_targets": ["tiktok"],
            "primary_aspect": "9:16",
            "target_duration_s": 20,
            "quality_tier": "draft",
            "template_ids": [],
            "strategy_pack": None,
        },
        "brief": {
            "input_mode": "exact_script",
            "raw_input": " ".join(text for _, text in SEGMENTS),
            "audience": "the creator's owner",
            "angle": f"{TEST_SCRIPT_VERSION}: a fixed trajectory for comparable scorecards",
            "assumptions": [],
            "hook_candidates": [],
            "selected_hook_key": None,
            "sources_policy": "closed_book",
            "constraints": [],
        },
        "intent": {
            "video": {
                "narrative_goal": "challenge_common_belief",
                "audience_effect": "insight",
                "persuasion_goal": "reframe_mental_model",
                "information_goal": "define_concept",
                "emotional_arc": ["neutral", "excited", "hesitant", "serious"],
                "attention_goal": "sustain",
                "cta_goal": "follow_for_series",
            }
        },
        "memory": {"snapshots": []},
        "cast": [
            {
                "key": "char_test",
                "role": "host",
                "creator_version_id": creator_version_id,
                "overrides": {"appearance_version_id": None, "voice_version_id": None},
            }
        ],
        "products": [],
        "script": {
            "segments": [
                {
                    "key": key,
                    "speaker_key": "char_test",
                    "text": text,
                    "language": None,
                    "annotations": (
                        [
                            {
                                "key": "an_1",
                                "type": "emphasis",
                                "tag": "emphasize",
                                "span": _words(key, 7, 7),
                                "source": "director",
                            }
                        ]
                        if key == "seg_4"
                        else []
                    ),
                    "claim_keys": [],
                }
                for key, text in SEGMENTS
            ]
        },
        "scenes": [
            {
                "key": "scn_test",
                "purpose": "hook",
                "order": 1,
                "segment_keys": [key for key, _ in SEGMENTS],
                "intent": {
                    "narrative_goal": "challenge_common_belief",
                    "emotional_goal": "create_doubt_then_reveal",
                    "audience_effect": "curiosity",
                    "persuasion_goal": "none",
                    "information_goal": "state_misconception",
                    "attention_goal": "delay_payoff",
                    "reveal_strategy": "tease_then_reveal",
                    "tension_level": 0.5,
                    "curiosity_level": 0.6,
                    "performance_strategy": "start_confident_then_lower_energy",
                    "notes": "Creator Test: neutral → excited → hesitant → serious.",
                },
                "world": world,
                "cast": [
                    {
                        "character_key": "char_test",
                        "wardrobe_version_id": wardrobe_version_id,
                        "placement": None,
                        "default_posture": None,
                    }
                ],
                "acting": {
                    "situation": {
                        "kind": "explaining_concept",
                        "description": "A short test talk that moves through four emotional states.",
                        "audience_stance": "neutral",
                        "stimulus": None,
                    },
                    "states": states,
                    "events": [
                        {
                            "key": "ev_laugh",
                            "character_key": "char_test",
                            "type": "small_laugh",
                            "at": {"segment_key": "seg_2", "word": 8},
                            "duration_ms": 900,
                            "direction": None,
                            "intensity": 0.4,
                            "purpose": "genuine",
                            "priority": "should",
                            "source": "director",
                        },
                        {
                            "key": "ev_look",
                            "character_key": "char_test",
                            "type": "look_away",
                            "at": {"segment_key": "seg_3", "word": 2},
                            "duration_ms": 700,
                            "direction": "down_left",
                            "intensity": 0.5,
                            "purpose": "thinking",
                            "priority": "should",
                            "source": "director",
                        },
                    ],
                },
                "shots": [
                    {
                        "key": "sht_test",
                        "type": "talking_head",
                        "layer": "base",
                        "span": _words("seg_1", 0, last["seg_4"], end_segment="seg_4"),
                        "character_key": "char_test",
                        "camera": {
                            "profile_id": "phone_front_selfie",
                            "framing": "medium_close_up",
                            "angle": "eye_level",
                            "moves": [],
                        },
                        "visual": {
                            "keyframe": {"strategy": "composite", "asset_id": None},
                            "prompt_extra": "",
                            "negative": "",
                        },
                        "takes": {"count": 1, "selected_take_key": None},
                    }
                ],
            }
        ],
        "audio": {
            "music": {"cues": []},
            "sfx": [],
            "acoustics": {"source": "world", "mic_profile": None},
            "loudness": {"integrated_lufs": -14, "true_peak_dbtp": -1},
        },
        "captions": {
            "enabled": True,
            "style_id": "bold_pop_highlight",
            "language": language.split("-")[0],
            "max_words_per_line": 3,
            "highlight": "active_word",
            "placement": "platform_safe_zone",
            "translations": [],
        },
        "brand": {"brand_kit_id": None, "logo_overlay": False},
        "render": {
            "outputs": [{"preset_id": preset_id, "aspect": "9:16"}],
            "reframe": {"strategy": "subject_aware", "regenerate_if_crop_loss_above": 0.25},
        },
        "provenance": {"visible_label": "auto", "consent_ids": []},
        "assets": [],
        "effects": [],
        "generation": {"seed_namespace": seed_namespace, "seed_overrides": {}, "engine_hints": {}},
        "locks": [],
    }
