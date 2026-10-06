"""Creator DNA, Appearance, Voice, Wardrobe, World DNA, Memory items and snapshots (§17–§19, §21)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ce_core.identity.creator import AppearanceDNA, CreatorDNA, VoiceDNA, validate_creator_dna
from ce_core.identity.memory import MemoryItem, dedup_key, validate_memory_item, value_hash
from ce_core.identity.world import WorldDNA, behavior_digest_fields, validate_world_dna
from ce_core.ids import new_id
from ce_core.vocab import Vocabulary, load_vocabulary
from ce_testing.fixtures import ALEX, alex_creator_dna, alex_memory_snapshot, alex_voice_dna, home_office_world
from pydantic import ValidationError

VOCAB_DIR = Path(__file__).resolve().parents[4] / "config" / "vocab"


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    return load_vocabulary(VOCAB_DIR)


# ---------------------------------------------------------------------- creator


def test_alex_dna_is_valid(vocab: Vocabulary) -> None:
    assert validate_creator_dna(alex_creator_dna(), vocab) == []


def test_creator_dna_vocab_and_range_rules(vocab: Vocabulary) -> None:
    data = alex_creator_dna().model_dump(mode="json")
    data["personality"]["humor_style"] = "slapstick"
    data["gesture"]["preferred_gestures"].append({"gesture": "nod", "frequency": "often"})
    issues = validate_creator_dna(CreatorDNA.model_validate(data), vocab)
    assert {i.code for i in issues} == {"unknown_vocab", "not_a_gesture"}
    data["behavior"]["emotion_ranges"]["calm"] = [0.8, 0.2]
    with pytest.raises(ValidationError, match=r"min 0\.8 > max 0\.2"):
        CreatorDNA.model_validate(data)


def test_dna_is_model_independent() -> None:
    """I1 schema lint (first slice): no engine parameter names in the DNA schema."""
    schema = str(CreatorDNA.model_json_schema()).lower()
    for engine_word in (
        "cfg_scale",
        "guidance",
        "lora",
        "prompt",
        "seed",
        "steps",
        "infinitetalk",
        "wan",
        "chatterbox",
    ):
        assert engine_word not in schema, engine_word


def test_appearance_must_present_as_adult() -> None:
    with pytest.raises(ValidationError):
        AppearanceDNA(age_appearance=17)
    assert AppearanceDNA(age_appearance=18).age_appearance == 18


def test_voice_rules() -> None:
    voice = alex_voice_dna()
    assert voice.wpm_for("en-US") == 150
    assert voice.wpm_for("de", default=140) == 140
    with pytest.raises(ValidationError, match="consent_id"):
        VoiceDNA(kind="cloned")
    with pytest.raises(ValidationError, match="exactly one"):
        VoiceDNA.model_validate({"lexicon": [{"term": "LLM"}]})


# ---------------------------------------------------------------------- world


def test_home_office_world_is_valid(vocab: Vocabulary) -> None:
    world = home_office_world()
    assert validate_world_dna(world, vocab) == []
    assert world.camera_position("cam_desk_front") is not None
    assert world.element("el_monitor") is not None and world.element("el_monitor").default_state == "on"  # type: ignore[union-attr]


def world_data() -> dict[str, Any]:
    return home_office_world().model_dump(mode="json")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["elements"][1].pop("default_state"), "default_state is required"),
        (lambda d: d["elements"][1].update(default_state="dim"), "not one of its states"),
        (lambda d: d["elements"].append(dict(d["elements"][0])), "duplicate world keys"),
        (
            lambda d: d["lighting"]["practicals"].append({"element": "el_lamp", "color_temp_k": 2700}),
            "practical light el_lamp",
        ),
        (lambda d: d["continuity"]["must_show_from"].update(cam_desk_front=["el_poster"]), "unknown element el_poster"),
        (lambda d: d["time_and_weather"].update(default_time_of_day="night"), "default_time_of_day"),
        (lambda d: [c.update(status="forbidden") for c in d["camera_positions"]], "at least one camera position"),
        (
            lambda d: d.update(references=[{"asset_id": str(ALEX.PLATE_FRONT_ASSET_ID), "role": "position:cam_roof"}]),
            "unknown camera position",
        ),
    ],
)
def test_world_dna_internal_rules(change: Any, message: str) -> None:
    data = world_data()
    change(data)
    with pytest.raises(ValidationError, match=message):
        WorldDNA.model_validate(data)


def test_world_dna_vocab_rules(vocab: Vocabulary) -> None:
    data = world_data()
    data["kind"] = "spaceship"
    data["zones"][0]["allowed_postures"].append("floating")
    issues = validate_world_dna(WorldDNA.model_validate(data), vocab)
    assert sorted(i.path or "" for i in issues) == ["/kind", "/zones[zone_desk_chair]/allowed_postures"]


def test_on_off_states_survive_yaml_style_input() -> None:
    assert home_office_world().element("el_neon").states == ["on", "off"]  # type: ignore[union-attr]


def test_behavior_digest_excludes_lighting_and_acoustics() -> None:
    world = home_office_world()
    data = world.model_dump(mode="json")
    data["lighting"]["key"]["color_temp_k"] = 3200
    data["acoustics"]["rt60_s"] = 0.9
    assert behavior_digest_fields(WorldDNA.model_validate(data)) == behavior_digest_fields(world)
    data["zones"][0]["allowed_postures"] = ["seated_upright"]
    assert behavior_digest_fields(WorldDNA.model_validate(data)) != behavior_digest_fields(world)


# ---------------------------------------------------------------------- memory


def memory_item(vocab: Vocabulary, kind: str, value: dict[str, Any], **extra: Any) -> MemoryItem:
    now = datetime(2026, 10, 3, tzinfo=UTC)
    typed = vocab.validate_memory_value(kind, value).model_dump(mode="json")
    base: dict[str, Any] = {
        "id": new_id(),
        "org_id": ALEX.ORG_ID,
        "creator_id": ALEX.CREATOR_ID,
        "category": kind.split(".")[0],
        "kind": kind,
        "key": dedup_key(vocab, kind, typed),
        "value_hash": value_hash(typed),
        "value": typed,
        "vocab_version": vocab.version,
        "source": {"type": "authored"},
        "first_seen_at": now,
        "last_seen_at": now,
        "updated_at": now,
    }
    base.update(extra)
    return MemoryItem.model_validate(base)


def test_memory_item_validates_against_its_kind(vocab: Vocabulary) -> None:
    item = memory_item(
        vocab, "speech_habit.recurring_phrase", {"text": "Here's  the THING", "max_per_video": 1, "contexts": []}
    )
    assert item.key == "here's the thing"
    assert validate_memory_item(item, vocab) == []
    tampered = item.model_copy(update={"value": {"text": "something else", "max_per_video": 1, "contexts": []}})
    assert {i.code for i in validate_memory_item(tampered, vocab)} == {"memory_key", "memory_value_hash"}
    wrong = item.model_copy(update={"value": {"text": "x", "max_per_video": "many"}})
    assert [i.code for i in validate_memory_item(wrong, vocab)] == ["memory_value"]


def test_persona_fact_key_ignores_object(vocab: Vocabulary) -> None:
    a = memory_item(vocab, "persona_fact", {"subject": "Alex", "predicate": "lives_in", "object": "Austin"})
    b = memory_item(vocab, "persona_fact", {"subject": "alex", "predicate": "lives_in", "object": "Berlin"})
    assert a.key == b.key and a.value_hash != b.value_hash  # same key, different value → a conflict (§18.7)


def test_memory_state_rules(vocab: Vocabulary) -> None:
    with pytest.raises(ValidationError, match="superseded_by_id"):
        memory_item(vocab, "gaze_habit.eye_contact_ratio", {"ratio": 0.7}, status="superseded")
    with pytest.raises(ValidationError, match="does not belong"):
        memory_item(vocab, "gaze_habit.eye_contact_ratio", {"ratio": 0.7}, category="avoidance")
    later = datetime(2026, 10, 2, tzinfo=UTC)
    with pytest.raises(ValidationError, match="before first_seen_at"):
        memory_item(vocab, "gaze_habit.eye_contact_ratio", {"ratio": 0.7}, last_seen_at=later - timedelta(days=1))
    active = memory_item(vocab, "gaze_habit.eye_contact_ratio", {"ratio": 0.7}, status="active")
    assert active.influences_planning
    assert not memory_item(
        vocab, "gaze_habit.eye_contact_ratio", {"ratio": 0.7}, status="forgotten"
    ).influences_planning


def test_snapshot_digest_is_content_only() -> None:
    snapshot = alex_memory_snapshot()
    moved = snapshot.model_copy(update={"id": new_id(), "created_at": datetime(2030, 1, 1, tzinfo=UTC)})
    assert snapshot.digest() == moved.digest()
    changed = snapshot.model_copy(update={"items": snapshot.items[:1]})
    assert snapshot.digest() != changed.digest()
