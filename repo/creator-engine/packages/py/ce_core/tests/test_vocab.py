"""The closed vocabularies (§15.2): shipped files lint clean, and each lint rule fires."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from ce_core.vocab import Vocabulary, VocabularyError, load_vocabulary
from ce_core.yamlio import load_yaml_file
from pydantic import ValidationError

VOCAB_DIR = Path(__file__).resolve().parents[4] / "config" / "vocab"


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    return load_vocabulary(VOCAB_DIR)


def mutated(tmp_path: Path, name: str, change: Callable[[Any], None]) -> Vocabulary:
    target = tmp_path / "vocab"
    shutil.copytree(VOCAB_DIR, target)
    data = load_yaml_file(target / name)
    change(data)
    (target / name).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_vocabulary(target)


def codes(v: Vocabulary) -> set[str]:
    return {issue.code for issue in v.lint()}


def test_shipped_vocabulary_lints_clean(vocab: Vocabulary) -> None:
    assert vocab.version == "2026.10.1"
    assert vocab.lint() == []


def test_spec_minimum_tokens_are_present(vocab: Vocabulary) -> None:
    # §15.2 shipped emotion minimum
    for label in [
        "neutral",
        "confident",
        "amused",
        "curious",
        "excited",
        "serious",
        "skeptical",
        "uncertain",
        "hesitant",
        "confused",
        "surprised",
        "embarrassed",
        "frustrated",
        "angry",
        "sad",
        "calm",
        "warm",
        "sarcastic",
        "determined",
        "relieved",
        "concerned",
        "playful",
    ]:
        assert vocab.has("emotion", label), label
    # §15.2 dimension list (requestable + engine properties)
    assert set(vocab.tokens("dimension")) == {
        "emotion_visual",
        "emotion_vocal",
        "facial_expression",
        "gaze",
        "blink",
        "head_motion",
        "gesture",
        "posture",
        "camera_awareness",
        "reaction",
        "listening",
        "nonverbal_audio",
        "delivery",
        "prosody_rate",
        "prosody_pitch",
        "prosody_energy",
        "prosody_emphasis",
        "prosody_pause",
        "accent",
    }
    assert vocab.engine_properties == {
        "segment_control",
        "prosody_coupling",
        "continuity",
        "object_interaction",
        "multi_person",
        "temporal_control",
    }
    # §12.7 lock groups and regenerate components
    assert set(vocab.lock_groups) == {
        "script",
        "voice",
        "appearance",
        "wardrobe",
        "world",
        "acting",
        "intent",
        "camera",
        "music",
        "sfx",
        "broll",
        "captions",
        "product",
    }
    assert set(vocab.regenerate_components) == {
        "voice",
        "keyframe",
        "avatar_video",
        "background",
        "world_plate",
        "broll",
        "camera_post",
        "acting",
        "music",
        "sfx",
        "captions",
        "lipsync",
    }
    # §14 intent minimum
    assert vocab.has("intent.reveal_strategy", "tease_then_reveal")
    assert vocab.has("intent.cta_goal", "follow_for_series")


def test_every_event_maps_to_one_requestable_dimension(vocab: Vocabulary) -> None:
    for name, event in vocab.events.items():
        assert event.dimension in vocab.dimensions, name


def test_on_off_states_stay_strings(vocab: Vocabulary) -> None:
    assert vocab.has("element_state", "on") and vocab.has("element_state", "off")


def test_check_reports_unknown_tokens(vocab: Vocabulary) -> None:
    assert vocab.check("emotion", "confident") == []
    assert vocab.check("emotion", None) == []
    [issue] = vocab.check("emotion", "jubilant", path="/scenes[scn_a]/acting/states[st_1]/emotion/displayed/label")
    assert issue.code == "unknown_vocab"
    assert issue.path is not None and issue.path.endswith("label")
    with pytest.raises(KeyError):
        vocab.has("no_such_category", "x")


def test_descriptions_have_aliases_and_variants(vocab: Vocabulary) -> None:
    confident = vocab.describe("emotion", "confident")
    assert confident is not None and "assured" in confident.aliases and len(confident.variants) >= 2
    assert vocab.describe("strategy.gaze", "hold_camera") is not None


def test_memory_values_are_typed_by_kind(vocab: Vocabulary) -> None:
    value = vocab.validate_memory_value("gaze_habit.thinking_glance", {"direction": "down_left", "typical_ms": 600})
    assert value.model_dump() == {"direction": "down_left", "typical_ms": 600}
    fact = vocab.validate_memory_value("persona_fact", {"subject": "Alex", "predicate": "lives_in", "object": "Austin"})
    assert fact.model_dump()["object"] == "Austin"
    with pytest.raises(ValidationError):
        vocab.validate_memory_value(
            "reaction_habit.laughter", {"expression": "giggle", "intensity": 0.3, "frequency": "often"}
        )
    with pytest.raises(ValidationError):
        vocab.validate_memory_value("social_behavior.warmth", {"level": 1.5})
    with pytest.raises(ValidationError):
        vocab.validate_memory_value("social_behavior.warmth", {"level": 0.5, "extra": 1})
    with pytest.raises(ValueError, match="unknown memory kind"):
        vocab.validate_memory_value("nonsense", {})
    preference = vocab.validate_memory_value("preference", {"dimension": "facial_expression", "delta": -0.2})
    assert preference.model_dump()["label"] is None


def test_digest_changes_with_content(tmp_path: Path, vocab: Vocabulary) -> None:
    changed = mutated(tmp_path, "intent.yaml", lambda d: d["cta_goal"].append("subscribe"))
    assert changed.digest != vocab.digest


# ---------------------------------------------------------------------- each lint rule fires


def test_lint_version_mismatch(tmp_path: Path) -> None:
    v = mutated(tmp_path, "intent.yaml", lambda d: d.update(vocab_version="2026.11.0"))
    assert "version_mismatch" in codes(v)


def test_lint_strategy_event_overlap(tmp_path: Path) -> None:
    def change(d: Any) -> None:
        d["event_types"]["hold_camera"] = {"dimension": "gaze", "default_duration_ms": 500, "params": []}

    assert "strategy_event_overlap" in codes(mutated(tmp_path, "behavior_events.yaml", change))


def test_lint_missing_description(tmp_path: Path) -> None:
    v = mutated(tmp_path, "intent.yaml", lambda d: d["cta_goal"].append("subscribe"))
    [issue] = [i for i in v.lint() if i.code == "missing_description"]
    assert "subscribe" in issue.message


def test_lint_event_must_map_to_requestable_dimension(tmp_path: Path) -> None:
    def change(d: Any) -> None:
        d["event_types"]["nod"]["dimension"] = "segment_control"

    assert "event_dimension" in codes(mutated(tmp_path, "behavior_events.yaml", change))


def test_lint_method_preference_must_end_with_omit(tmp_path: Path) -> None:
    def change(d: Any) -> None:
        d["requestable"]["gaze"]["method_preference"] = ["native_parametric"]

    assert "method_preference" in codes(mutated(tmp_path, "behavior_dimensions.yaml", change))


def test_lint_bad_lock_pattern_and_node_kind(tmp_path: Path) -> None:
    def change(d: Any) -> None:
        d["lock_groups"]["music"]["patterns"] = ["/audio/music[0]"]
        d["regenerate_components"]["sfx"]["node_kinds"] = ["audio.sfx_v2"]

    found = codes(mutated(tmp_path, "edit_vocabulary.yaml", change))
    assert {"lock_pattern", "node_kind"} <= found


def test_lint_unknown_memory_field_type(tmp_path: Path) -> None:
    def change(d: Any) -> None:
        d["categories"]["gaze_habit"]["eye_contact_ratio"]["fields"]["ratio"] = "vocab:nonexistent"

    assert "memory_field_type" in codes(mutated(tmp_path, "memory_kinds.yaml", change))


def test_malformed_file_is_rejected_with_its_name(tmp_path: Path) -> None:
    with pytest.raises(VocabularyError, match=r"emotions\.yaml"):
        mutated(tmp_path, "emotions.yaml", lambda d: d["labels"]["calm"].update(valence=3))
    with pytest.raises(VocabularyError, match="missing vocabulary file"):
        load_vocabulary(tmp_path / "nowhere")
